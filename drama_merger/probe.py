"""
媒体探测：解析每个片段的技术参数，并做"一致性分析"。

一致性分析是整个工具的决策中枢：
- 若所有片段编码参数完全一致 -> 可用 concat demuxer 无损拼接（秒级，不损画质）
- 若不一致 -> 必须先归一化（重编码到统一参数），或走 concat filter 一次性重编码

这一步做得好，能避免 90% 的"拼接后花屏/音画不同步/播放器不认"问题。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .ffmpeg_env import FfmpegEnv, _run

# 容器/流格式识别
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".m4v", ".3gp", ".rmvb", ".vob",
}


@dataclass
class StreamInfo:
    index: int
    codec_type: str = ""          # video / audio / subtitle
    codec_name: str = ""          # h264 / hevc / aac / mp3 ...
    profile: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    r_frame_rate: str = ""
    avg_frame_rate: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    bit_rate: int = 0
    duration: float = 0.0
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""

    @property
    def fps(self) -> float:
        for raw in (self.avg_frame_rate, self.r_frame_rate):
            if raw and raw not in ("0/0",):
                try:
                    return float(Fraction(raw))
                except Exception:  # noqa: BLE001
                    continue
        return 0.0

    def video_signature(self) -> tuple:
        """用于判断两个视频流能否无损串接的关键参数。"""
        return (self.codec_name, self.width, self.height, self.pix_fmt, self.profile)

    def audio_signature(self) -> tuple:
        return (self.codec_name, self.sample_rate, self.channels, self.channel_layout)


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    size: int = 0
    format_name: str = ""
    bit_rate: int = 0
    video: StreamInfo | None = None
    audios: list[StreamInfo] = field(default_factory=list)
    subtitles: list[StreamInfo] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    probe_error: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return len(self.audios) > 0

    @property
    def width(self) -> int:
        return self.video.width if self.video else 0

    @property
    def height(self) -> int:
        return self.video.height if self.video else 0

    @property
    def fps(self) -> float:
        return self.video.fps if self.video else 0.0

    def summary(self) -> str:
        if self.probe_error:
            return f"{self.name}  [探测失败: {self.probe_error}]"
        v = self.video
        vtxt = (
            f"{v.codec_name} {v.width}x{v.height} {v.fps:g}fps {v.pix_fmt}"
            if v else "无视频流"
        )
        atxt = (
            f"{self.audios[0].codec_name} {self.audios[0].sample_rate}Hz "
            f"{self.audios[0].channels}ch"
            if self.audios else "无音轨"
        )
        return f"{self.name}  [{self.duration:.2f}s]  视频:{vtxt}  音频:{atxt}"


def _parse_fraction(value: str) -> float:
    try:
        return float(Fraction(value))
    except Exception:  # noqa: BLE001
        return 0.0


def probe_file(path: Path, env: FfmpegEnv, timeout: int = 60) -> MediaInfo:
    """用 ffprobe 探测单个文件。ffprobe 缺失时退化用 ffmpeg -i 解析。"""
    info = MediaInfo(path=path)
    try:
        info.size = path.stat().st_size
    except Exception:  # noqa: BLE001
        info.size = 0

    ffprobe = env.ffprobe
    data: dict | None = None

    if ffprobe:
        cmd = [
            ffprobe, "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        code, out, err = _run(cmd, timeout=timeout)
        if code == 0 and out.strip():
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                data = None
        else:
            info.probe_error = _condense(err)

    if data is None:
        # 退化路径：解析 ffmpeg -i 的 stderr
        data = _probe_via_ffmpeg(path, env, info)

    if data is None:
        if not info.probe_error:
            info.probe_error = "无法解析媒体信息"
        return info

    info.raw = data
    fmt = data.get("format", {}) or {}
    info.format_name = fmt.get("format_name", "")
    info.duration = _parse_fraction(str(fmt.get("duration", "0")))
    info.bit_rate = int(fmt.get("bit_rate", 0) or 0)

    for st in data.get("streams", []) or []:
        codec_type = st.get("codec_type", "")
        si = StreamInfo(
            index=int(st.get("index", 0)),
            codec_type=codec_type,
            codec_name=st.get("codec_name", ""),
            profile=st.get("profile", ""),
            width=int(st.get("width", 0) or 0),
            height=int(st.get("height", 0) or 0),
            pix_fmt=st.get("pix_fmt", ""),
            r_frame_rate=st.get("r_frame_rate", ""),
            avg_frame_rate=st.get("avg_frame_rate", ""),
            sample_rate=int(st.get("sample_rate", 0) or 0),
            channels=int(st.get("channels", 0) or 0),
            channel_layout=st.get("channel_layout", ""),
            bit_rate=int(st.get("bit_rate", 0) or 0),
            color_space=st.get("color_space", ""),
            color_transfer=st.get("color_transfer", ""),
            color_primaries=st.get("color_primaries", ""),
        )
        # 流级时长缺失时用容器时长兜底
        si.duration = _parse_fraction(str(st.get("duration", "0"))) or info.duration

        if codec_type == "video":
            # 跳过封面图（mjpeg/png 且只有一帧的情况）
            if si.codec_name in ("mjpeg", "png", "bmp") and st.get("disposition", {}).get("attached_pic"):
                continue
            if info.video is None:
                info.video = si
        elif codec_type == "audio":
            info.audios.append(si)
        elif codec_type == "subtitle":
            info.subtitles.append(si)

    if info.duration <= 0 and info.video and info.video.duration:
        info.duration = info.video.duration

    return info


def _condense(err: str, limit: int = 200) -> str:
    """把 ffprobe 的多行报错压成一行。"""
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    if not lines:
        return "未知错误"
    msg = lines[-1]
    return msg[:limit]


def _probe_via_ffmpeg(path: Path, env: FfmpegEnv, info: MediaInfo) -> dict | None:
    """没有 ffprobe 时，从 ffmpeg -i 的 stderr 里榨取信息。"""
    code, out, err = _run([env.ffmpeg, "-hide_banner", "-i", str(path)], timeout=60)
    text = out + err

    streams: list[dict] = []
    fmt: dict = {}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        fmt["duration"] = str(h * 3600 + mi * 60 + s)
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        fmt["bit_rate"] = str(int(m.group(1)) * 1000)
    m = re.search(r"Input #0,\s*([^,]+),", text)
    if m:
        fmt["format_name"] = m.group(1).strip()

    idx = 0
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"Stream #\d+:\d+.*?:\s*Video:\s*([a-zA-Z0-9_]+)", line)
        if m:
            st: dict = {"index": idx, "codec_type": "video", "codec_name": m.group(1)}
            mr = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if mr:
                st["width"], st["height"] = int(mr.group(1)), int(mr.group(2))
            mp = re.search(r",\s*(yuv[a-z0-9]+|rgb[a-z0-9]*|gray|nv12)\b", line)
            if mp:
                st["pix_fmt"] = mp.group(1)
            mf = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if mf:
                st["avg_frame_rate"] = f"{mf.group(1)}/1"
            mp2 = re.search(r"Video:\s*[a-zA-Z0-9_]+[^,]*,\s*([^,(]+?)(?:\s*\(|,)", line)
            if mp2:
                prof = mp2.group(1).strip()
                if prof and not prof[0].isdigit():
                    st["profile"] = prof
            streams.append(st)
            idx += 1
            continue
        m = re.match(r"Stream #\d+:\d+.*?:\s*Audio:\s*([a-zA-Z0-9_]+)", line)
        if m:
            st = {"index": idx, "codec_type": "audio", "codec_name": m.group(1)}
            ms = re.search(r"(\d{4,6})\s*Hz", line)
            if ms:
                st["sample_rate"] = int(ms.group(1))
            mc = re.search(r"Hz,\s*([a-z]+(?:\d\.\d)?)", line)
            if mc:
                st["channel_layout"] = mc.group(1)
                st["channels"] = {"mono": 1, "stereo": 2, "quad": 4}.get(mc.group(1), 2)
            streams.append(st)
            idx += 1
            continue
        m = re.match(r"Stream #\d+:\d+.*?:\s*Subtitle:\s*([a-zA-Z0-9_]+)", line)
        if m:
            streams.append({"index": idx, "codec_type": "subtitle", "codec_name": m.group(1)})
            idx += 1

    if not streams:
        return None
    return {"format": fmt, "streams": streams}


def collect_video_files(directory: Path, recursive: bool = False,
                        exts: set[str] | None = None) -> list[Path]:
    """收集目录下的视频文件，按文件名自然排序（第2集排在 第10集 前）。"""
    exts = exts or VIDEO_EXTS
    pattern = "**/*" if recursive else "*"
    files = [
        p for p in directory.glob(pattern)
        if p.is_file() and p.suffix.lower() in exts
    ]
    return sorted(files, key=lambda p: _natural_key(p.name))


def _natural_key(name: str) -> tuple:
    """自然排序键：ep6 < ep10，而非字典序 ep10 < ep6。"""
    parts = re.split(r"(\d+)", name)
    key: list = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part), ""))
        else:
            key.append((0, 0, part.lower()))
    return tuple(key)


# ---------------------------------------------------------------------------
# 一致性分析
# ---------------------------------------------------------------------------

@dataclass
class Inconsistency:
    """一处参数不一致。"""

    field: str          # 描述性字段名，如 "分辨率"
    values: dict        # {片段名: 值}
    severity: str       # "blocker" 必须处理 / "warning" 建议处理

    def describe(self) -> str:
        uniq = sorted({str(v) for v in self.values.values()})
        detail = "、".join(uniq) if len(uniq) <= 4 else f"{len(uniq)} 种取值"
        return f"{self.field} 不一致（{detail}）"


@dataclass
class ConsistencyReport:
    items: list[MediaInfo]
    inconsistencies: list[Inconsistency] = field(default_factory=list)

    @property
    def blockers(self) -> list[Inconsistency]:
        return [i for i in self.inconsistencies if i.severity == "blocker"]

    @property
    def warnings(self) -> list[Inconsistency]:
        return [i for i in self.inconsistencies if i.severity == "warning"]

    @property
    def is_uniform(self) -> bool:
        """所有片段关键参数完全一致 -> 可无损拼接。"""
        return not self.inconsistencies

    @property
    def can_concat_lossless(self) -> bool:
        """只要没有 blocker 就可以走"归一化后无损拼接"，更快。"""
        return not self.blockers

    # ------------------------------------------------------------------
    # 拼接签名分析
    #
    # 核心思路：无损 concat 要求参与拼接的片段参数完全一致。实际素材中
    # 常见的是"绝大多数集一致，个别集偏离"（例如某集被换成 4K/HEVC）。
    # 这时只需重编码那些偏离的集，其余原样复制即可 —— 比全部重编码快得多，
    # 且画质损失面最小。
    # ------------------------------------------------------------------

    @staticmethod
    def concat_signature(item: MediaInfo) -> tuple | None:
        """决定两个片段能否被 -c copy 直接串接的参数组合。

        任一必需参数缺失则返回 None（该片段必须被归一化）。
        """
        v = item.video
        if v is None or not v.width or not v.height:
            return None
        if item.has_audio:
            a = item.audios[0]
            audio_sig = (a.codec_name, a.sample_rate, a.channels)
        else:
            audio_sig = ("__none__", 0, 0)
        return (
            v.codec_name, v.width, v.height, v.pix_fmt,
            v.profile or "", round(v.fps, 3),
            audio_sig,
        )

    def signature_groups(self) -> dict[tuple, list[MediaInfo]]:
        """按拼接签名分组，按组内片段数降序返回。签名缺失的归入特殊键。"""
        groups: dict[tuple, list[MediaInfo]] = {}
        for it in self.items:
            sig = self.concat_signature(it)
            key = sig if sig is not None else ("__unprobeable__",)
            groups.setdefault(key, []).append(it)
        # 按组内数量降序
        return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))

    def majority_group(self) -> tuple[tuple | None, list[MediaInfo]]:
        """返回数量最多的签名组 (签名, 片段列表)。"""
        groups = self.signature_groups()
        if not groups:
            return None, []
        sig, items = next(iter(groups.items()))
        if sig and sig[0] == "__unprobeable__":
            return None, items
        return sig, items

    def deviating_items(self) -> list[MediaInfo]:
        """返回不属于多数派的片段（这些需要重编码）。"""
        _, maj = self.majority_group()
        maj_ids = {id(i) for i in maj}
        return [i for i in self.items if id(i) not in maj_ids]

    def minimal_reencode_plan(self) -> tuple[list[MediaInfo], list[MediaInfo]]:
        """返回 (需重编码的片段, 可原样复制的片段)。

        仅当多数派占比 >= 50% 且多数派非空时，才值得走"最小重编码"。
        否则全部归一化更稳妥。
        """
        if not self.items:
            return [], []
        sig, maj = self.majority_group()
        if sig is None or len(maj) * 2 < len(self.items):
            return list(self.items), []
        maj_ids = {id(i) for i in maj}
        dev = [i for i in self.items if id(i) not in maj_ids]
        return dev, maj

    def target_video_params(self) -> dict:
        """推导归一化的目标视频参数。

        优先取自"多数派"片段（数量最多的拼接签名组），这样重编码后的片段
        能与多数派片段无缝 -c copy 拼接。若无明确多数派，则取出现最多的分辨率
        与最高帧率（保证不降质）。
        """
        _, maj = self.majority_group()
        pool = [i.video for i in (maj or self.items) if i.video]
        if not pool:
            return {"width": 1080, "height": 1920, "fps": 30.0}

        from collections import Counter

        res_counter = Counter((v.width, v.height) for v in pool)
        width, height = res_counter.most_common(1)[0][0]
        # 帧率同样取多数派的众数，而非全局最大值 ——
        # 否则重编码片段帧率会与多数派不同，导致无法无缝拼接
        fps_counter = Counter(round(v.fps, 3) for v in pool if v.fps)
        fps = fps_counter.most_common(1)[0][0] if fps_counter else 30.0
        return {"width": width, "height": height, "fps": fps}

    def target_audio_params(self) -> dict:
        """推导归一化的目标音频参数，同样优先取自多数派。"""
        from collections import Counter

        _, maj = self.majority_group()
        pool = [a for i in (maj or self.items) for a in i.audios]
        if not pool:
            audios = [a for i in self.items for a in i.audios]
            if not audios:
                return {"sample_rate": 48000, "channels": 2}
            pool = audios

        sr = Counter(a.sample_rate for a in pool if a.sample_rate).most_common(1)
        sample_rate = sr[0][0] if sr else 48000
        ch = Counter(a.channels for a in pool if a.channels).most_common(1)
        channels = ch[0][0] if ch else 2
        return {"sample_rate": sample_rate, "channels": channels}

    def target_profile(self) -> str:
        """探测多数派视频流的 profile（小写，供 -profile:v 使用）。

        走最小重编码时，重编码片段必须与未重编码的多数派 profile 一致，
        否则拼接会失败。真实素材常见 Main，若输出 high 就不匹配。
        """
        _, maj = self.majority_group()
        pool = [i.video for i in (maj or self.items) if i.video]
        profiles = [v.profile for v in pool if v.profile]
        if not profiles:
            return ""
        from collections import Counter

        top = Counter(p.lower() for p in profiles).most_common(1)[0][0]
        # 归一化映射：ffmpeg 接受 baseline/main/high/high10 等
        mapping = {
            "constrained baseline": "main",   # 兼容 broadcast 场景更稳
            "baseline": "main",
            "main": "main",
            "high": "high",
            "high 10": "high10",
        }
        return mapping.get(top, "")

    def target_timescale(self) -> int:
        """探测多数派视频流的 time_base 分母，作为 -video_track_timescale。

        实测：原始素材常为 1/30、1/60（等于帧率），而 ffmpeg 默认输出
        1/90000 或 1/15360。timescale 不一致会在拼接时产生
        "Non-monotonic DTS" 警告，影响时间轴精度。
        """
        _, maj = self.majority_group()
        pool = [i for i in (maj or self.items) if i.video]
        vals = []
        for it in pool:
            tb = (it.raw.get("streams") or [{}])
            for st in it.raw.get("streams", []):
                if st.get("codec_type") == "video" and st.get("time_base"):
                    tb = st["time_base"]
                    try:
                        den = int(str(tb).split("/")[1])
                        if den > 0:
                            vals.append(den)
                    except (ValueError, IndexError):
                        pass
                    break
        if not vals:
            return 0
        from collections import Counter

        return Counter(vals).most_common(1)[0][0]


def analyze_consistency(items: list[MediaInfo]) -> ConsistencyReport:
    """分析一组片段的一致性，判定可用的拼接策略。"""
    report = ConsistencyReport(items=items)

    valid = [i for i in items if i.video is not None]
    if not valid:
        report.inconsistencies.append(
            Inconsistency("有效视频流", {i.name: "缺失" for i in items}, "blocker")
        )
        return report

    # --- 视频参数一致性与严重度 ---
    def check(field_name: str, getter, severity: str) -> None:
        values = {i.name: getter(i) for i in valid}
        if len(set(map(str, values.values()))) > 1:
            report.inconsistencies.append(Inconsistency(field_name, values, severity))

    # 分辨率/像素格式不同 -> concat 后必然花屏，blocker
    check("分辨率", lambda i: f"{i.width}x{i.height}", "blocker")
    check("像素格式", lambda i: i.video.pix_fmt, "blocker")
    # 编码格式/profile 不同 -> 播放器可能后段崩溃，blocker
    check("视频编码", lambda i: i.video.codec_name, "blocker")
    check("编码档次(profile)", lambda i: i.video.profile or "未标注", "blocker")
    # 帧率不同 -> 可拼接但时间轴会漂，warning
    check("帧率", lambda i: f"{i.fps:g}", "warning")

    # --- 音频参数 ---
    has_audio_flags = {i.name: i.has_audio for i in valid}
    if len(set(has_audio_flags.values())) > 1:
        report.inconsistencies.append(
            Inconsistency("音轨有无", {k: ("有" if v else "无") for k, v in has_audio_flags.items()},
                          "blocker")
        )
    else:
        with_audio = [i for i in valid if i.has_audio]
        if with_audio:
            def aget(getter):
                return {i.name: getter(i.audios[0]) for i in with_audio}

            a_codecs = aget(lambda a: a.codec_name)
            if len(set(a_codecs.values())) > 1:
                report.inconsistencies.append(Inconsistency(
                    "音频编码", a_codecs, "blocker"))
            a_sr = aget(lambda a: a.sample_rate)
            if len(set(a_sr.values())) > 1:
                report.inconsistencies.append(Inconsistency(
                    "音频采样率", a_sr, "warning"))
            a_ch = aget(lambda a: a.channels)
            if len(set(a_ch.values())) > 1:
                report.inconsistencies.append(Inconsistency(
                    "音频声道数", a_ch, "warning"))

    # --- 色彩元数据（不一致会导致颜色跳动）---
    color_vals = {i.name: (i.video.color_space, i.video.color_primaries) for i in valid}
    if len(set(map(str, color_vals.values()))) > 1 and any(v[0] for v in color_vals.values()):
        report.inconsistencies.append(Inconsistency(
            "色彩空间元数据", {k: f"{v[0] or '未标注'}" for k, v in color_vals.items()},
            "warning"))

    return report
