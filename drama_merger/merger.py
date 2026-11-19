"""
合并策略引擎 —— 这是整个工具的大脑。

三档策略，按"代价从低到高"依次降级：

  策略 A  LOSSLESS_CONCAT   参数完全一致且无需裁剪/淡变 -> concat demuxer + -c copy
                            速度：秒级；画质：零损失（不重新编码）
                            实测：18 秒素材 0.3~0.8 秒完成（23~57x 实时）
  策略 B  NORMALIZE_CONCAT  参数有差异 -> 先统一转码到目标参数，再无损拼接
                            速度：1 次全量编码；画质：一次损失
                            这是非一致场景下的默认策略，因为归一化几乎能解决
                            所有常见差异（分辨率/帧率/像素格式/采样率/音轨缺失）
  策略 C  FILTER_CONCAT     兜底 -> concat 滤镜一次性重编码（跳过归一化预处理）
                            速度：最慢，但能应对归一化本身出错的疑难片段

策略选择逻辑：
  完全一致 & 无裁剪淡变  -> A
  其他情况              -> B
  用户强制 / B 失败      -> C
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .compress import CompressPreset
from .ffmpeg_env import FfmpegEnv, _run
from .probe import ConsistencyReport, MediaInfo

IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


class Strategy(str, Enum):
    LOSSLESS_CONCAT = "A"       # 无损拼接
    MINIMAL_REENCODE = "D"      # 最小重编码：只重编码偏离片段，其余原样复制
    NORMALIZE_CONCAT = "B"      # 全部归一化后拼接
    FILTER_CONCAT = "C"         # 滤镜重编码（兜底）
    ENCODE_CONCAT = "E"         # 直连编码合并（合并即压缩，一次成型）

    @property
    def label(self) -> str:
        return {
            "A": "策略A · 无损拼接（不重编码，秒级完成，画质零损失）",
            "D": "策略D · 最小重编码（只重编码偏离的少数片段，其余零损失）",
            "B": "策略B · 归一化拼接（全部统一参数后拼接，仅转码一次）",
            "C": "策略C · 滤镜重编码（兼容性最强，速度最慢）",
            "E": "策略E · 合并即压缩（拼接同时编码，一次成型、不产生中间大文件）",
        }[self.value]


class Encoder(str, Enum):
    AUTO = "auto"
    NVENC = "nvenc"     # H.264 硬件编码
    X264 = "x264"       # H.264 软件编码


@dataclass
class MergeOptions:
    """合并选项。"""

    output: Path
    encoder: Encoder = Encoder.AUTO
    crf: int = 20
    preset: str = "veryfast"          # libx264 preset
    nvenc_preset: str = "p4"          # nvenc preset
    audio_bitrate: str = "192k"
    # 片头片尾裁剪（秒）
    trim_head: float = 0.0
    trim_tail: float = 0.0
    fade_in: float = 0.0              # 整片开头淡入秒数（0 = 不处理）
    fade_out: float = 0.0
    force_strategy: "Strategy | None" = None
    keep_temp: bool = False
    on_progress: object = None        # callable(phase: str, ratio: float)
    # 合并即压缩：指定档位则合并时直接编码输出，不产生中间大文件
    compress: "CompressPreset | None" = None

    @property
    def needs_reencode(self) -> bool:
        """是否需要重编码（裁剪或淡变都无法在 -c copy 下完成）。"""
        return bool(self.trim_head or self.trim_tail or self.fade_in or self.fade_out
                    or self.compress is not None)

    @property
    def needs_filter_processing(self) -> bool:
        """是否需要"逐片段/整片加工"（裁剪、淡变）。

        与 needs_reencode 的区别：压缩本身虽也需编码，但不妨碍使用
        concat demuxer 直连（策略E）；而裁剪/淡变必须逐片段或整片处理，
        无法用 concat demuxer 表达。选择策略时看的是这个属性。
        """
        return bool(self.trim_head or self.trim_tail or self.fade_in or self.fade_out)


@dataclass
class MergeResult:
    output: Path
    strategy: Strategy
    duration: float = 0.0
    size: int = 0
    elapsed: float = 0.0
    encoder_used: str = ""
    log: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 进度解析
# ---------------------------------------------------------------------------

class ProgressReader:
    """解析 ffmpeg -progress 输出，回调进度。"""

    def __init__(self, total_duration: float, phase: str, callback=None):
        self.total = max(total_duration, 0.001)
        self.phase = phase
        self.callback = callback
        self._last = -1.0        # 上一次上报的秒数，用于去重

    def feed(self, line: str) -> None:
        if not line:
            return
        key, _, val = line.partition("=")
        key = key.strip()

        # ffmpeg 收尾时会发 progress=end。用 -copyts 拼接时，末尾的
        # out_time 常略小于名义总时长（时序对齐误差），导致进度停在
        # 95%~99% 就到不了 100%。收到 end 时补一次 100%。
        if key == "progress" and val.strip() == "end":
            if self.callback:
                self.callback(self.phase, 1.0)
            return

        if key not in ("out_time_us", "out_time_ms"):
            return
        raw = val.strip()
        if not raw or raw.startswith("-"):
            return
        try:
            num = int(raw)
        except ValueError:
            return
        # 注意：ffmpeg 每次进度会同时发 out_time_us 与 out_time_ms（值相同，
        # 单位都是微秒）。若两者都上报，回调会触发两次 —— 之前表现为
        # 进度条每帧打印两遍。这里按秒数去重，只上报一次。
        secs = num / 1_000_000.0
        if secs == self._last:
            return
        self._last = secs
        if self.callback:
            self.callback(self.phase, min(secs / self.total, 1.0))


def _run_ffmpeg_with_progress(
    cmd: list[str],
    total_duration: float,
    phase: str,
    options: MergeOptions,
    log_lines: list[str],
) -> int:
    """执行 ffmpeg 并实时解析进度。"""
    progress_cb = getattr(options, "on_progress", None)
    reader = ProgressReader(total_duration, phase, progress_cb)

    cmd = list(cmd)
    try:
        idx = cmd.index("-y")
        cmd[idx:idx] = ["-progress", "pipe:1", "-nostats"]
    except ValueError:
        cmd.extend(["-progress", "pipe:1", "-nostats"])

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        log_lines.append(f"无法启动 ffmpeg: {exc}")
        return -1

    # ★ 必须用两个独立线程分别读取 stdout 和 stderr。
    #
    # 这里踩过一个会让长剧集必然卡死的坑：如果先循环读完 stdout 再读 stderr，
    # 当 ffmpeg 往 stderr 输出大量警告时（拼接多集时常见的
    # "Non-monotonic DTS" 每条一次），stderr 的管道缓冲区（Windows 约 64KB）
    # 很快被填满，ffmpeg 随即阻塞在写 stderr 上，不再往 stdout 写进度；
    # 而主线程正阻塞在等待 stdout，双方互等 —— 死锁，表现为程序无输出假死。
    # 合并 20+ 集时 stderr 警告量足以触发。
    stderr_tail: list[str] = []
    stderr_lock = threading.Lock()

    def pump_stdout() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                reader.feed(line.strip())
        except Exception:  # noqa: BLE001
            pass

    def pump_stderr() -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                s = line.strip()
                if not s:
                    continue
                with stderr_lock:
                    stderr_tail.append(s)
                    if len(stderr_tail) > 400:
                        del stderr_tail[:200]
        except Exception:  # noqa: BLE001
            pass

    t_out = threading.Thread(target=pump_stdout, daemon=True)
    t_err = threading.Thread(target=pump_stderr, daemon=True)
    t_out.start()
    t_err.start()

    proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    with stderr_lock:
        log_lines.extend(stderr_tail[-30:])
    return proc.returncode if proc.returncode is not None else -1


# ---------------------------------------------------------------------------
# 命令构建
# ---------------------------------------------------------------------------

def _escape_concat_path(path: Path) -> str:
    """concat demuxer 的 list 文件里，路径需转义单引号。"""
    p = str(path).replace("\\", "/")
    return p.replace("'", r"'\''")


def build_concat_list(files: list[Path], list_path: Path) -> None:
    """生成 concat demuxer 所需的 list 文件（UTF-8 无 BOM）。"""
    lines = [f"file '{_escape_concat_path(f.resolve())}'" for f in files]
    with open(list_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _fmt_fps(fps: float) -> str:
    """30.0 -> '30'；23.976 -> '23.976'（避免 fps=30.0 这类可能不被识别的写法）"""
    if abs(fps - round(fps)) < 1e-6:
        return str(int(round(fps)))
    return f"{fps:.6g}"


def _compress_encoder_args(preset: CompressPreset) -> list[str]:
    """按压缩档位生成视频编码参数（合并即压缩用）。

    为什么压缩场景一律用软编（libx264/libx265）而非 NVENC：
    实测同 CRF 下 NVENC 输出体积约为 x264 的 2 倍（1080p60 样本：
    x264 20.5MB vs NVENC 45.5MB）。NVENC 追求吞吐量，编码效率本就弱，
    对"瘦身"这个目标南辕北辙。
    """
    args = [
        "-c:v", preset.vcodec_name,
        "-preset", preset.speed,
        "-crf", str(preset.crf),
        "-pix_fmt", "yuv420p",
    ]
    if preset.codec == "hevc":
        # 兼容性最好的组合：Main profile（非 Main10）+ hvc1 标签
        #   - Main profile：老设备可播（Main10/10bit 很多设备不支持）
        #   - hvc1 标签：Apple / QuickTime 系能正确识别
        args += [
            "-profile:v", "main",
            "-tag:v", "hvc1",
            "-x265-params", "log-level=error",
        ]
    return args


def _compress_filter_suffix(preset: CompressPreset) -> list[str]:
    """按压缩档位生成缩放/降帧滤镜（附加在归一化链之后）。"""
    parts: list[str] = []
    if preset.max_height:
        # 等比缩放到高度上限，宽度按比例并保证偶数（H.264/H.265 硬性要求）
        parts.append(f"scale=-2:'min({preset.max_height},ih)'")
    if preset.fps_cap:
        parts.append(f"fps={preset.fps_cap}")
    return parts


def _video_encoder_args(options: MergeOptions, env: FfmpegEnv,
                        resolved: list[str], profile: str = "") -> list[str]:
    """生成视频编码参数。

    profile 参数很关键：走"最小重编码"策略时，重编码片段必须与未重编码的
    多数派片段 profile 一致，否则 -c copy 拼接会失败或产生异常时间戳。
    真实短剧素材常见的是 Main profile，若强行输出 high 就会不一致。
    传空字符串则交由编码器自动选择。
    """
    use_nvenc = False
    if options.encoder == Encoder.NVENC:
        if not env.nvenc_available:
            resolved.append("请求 NVENC 但实测不可用，已回退到 libx264")
        else:
            use_nvenc = True
    elif options.encoder == Encoder.AUTO and env.nvenc_available:
        use_nvenc = True

    prof_args = ["-profile:v", profile] if profile else []

    if use_nvenc:
        # 实测 p4 + cq 是质量/速度平衡点；
        # 不显式指定 level，交由编码器按分辨率自动选择（竖屏大分辨率易超 4.1）
        return [
            "-c:v", "h264_nvenc",
            "-preset", options.nvenc_preset,
            "-rc", "vbr",
            "-cq", str(options.crf),
            "-b:v", "0",
            "-pix_fmt", "yuv420p",
            *prof_args,
        ]
    return [
        "-c:v", "libx264",
        "-preset", options.preset,
        "-crf", str(options.crf),
        "-pix_fmt", "yuv420p",
        *prof_args,
    ]


def _video_filter_chain(w: int, h: int, fps: float) -> str:
    """统一的视频归一化滤镜链：缩放补边 -> 统一帧率 -> 统一像素格式。"""
    return ",".join([
        f"scale={w}:{h}:force_original_aspect_ratio=decrease",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={_fmt_fps(fps)}",
        "format=yuv420p",
    ])


def _audio_filter_chain(sr: int, ch: int, reset_pts: bool = True) -> str:
    """统一的音频归一化滤镜链。

    末尾的 asetpts 很关键：AAC 编码器有固定的 priming samples（本机实测约 3352 个
    @44100Hz ≈ 76ms），会让音频流的 start_time 与视频流不一致
    （实测音频 0.076s vs 视频 0.100s），从而引入细微音画错位。
    重置音频 PTS 到 0 可消除这个偏移。
    """
    parts = [
        f"aresample={sr}",
        f"aformat=channel_layouts={'stereo' if ch >= 2 else 'mono'}",
    ]
    if reset_pts:
        parts.append("asetpts=PTS-STARTPTS")
    return ",".join(parts)


def build_normalize_cmd(
    src: Path,
    dst: Path,
    target_video: dict,
    target_audio: dict,
    options: MergeOptions,
    env: FfmpegEnv,
    resolved: list[str],
    info: MediaInfo,
    profile: str = "",
    timescale: int | None = None,
) -> list[str]:
    """构建单片段归一化命令。

    要点：
    - 无音轨片段用 lavfi 补静音，并用显式 -map 确保取对音轨
      （不加 -map 时 ffmpeg 自动选择，多输入场景可能选错）
    - 裁剪用输入 seek（-ss 在 -i 前）+ 输出时长（-t）组合
    - profile / timescale 由调用方按"多数派"传入：走最小重编码策略时，
      重编码片段必须与未重编码的片段保持一致，否则 -c copy 拼接会失败
    """
    w, h = target_video["width"], target_video["height"]
    fps = target_video["fps"]
    sr = target_audio["sample_rate"]
    ch = target_audio["channels"]
    ch_layout = "stereo" if ch >= 2 else "mono"

    cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "warning"]

    # 输入 seek：放在 -i 前，速度快且重编码时仍能精确到帧
    if options.trim_head > 0:
        cmd.extend(["-ss", f"{options.trim_head:.3f}"])
    cmd.extend(["-i", str(src)])

    # 计算裁剪后时长
    out_dur: float | None = None
    if options.trim_head > 0 or options.trim_tail > 0:
        avail = info.duration - options.trim_head - options.trim_tail
        if avail > 0.05:
            out_dur = avail

    if not info.has_audio:
        # 补一路静音，长度与视频一致，保证拼接时音轨对齐
        sil_dur = out_dur if out_dur else max(info.duration, 0.1)
        cmd.extend([
            "-f", "lavfi",
            "-i", f"anullsrc=r={sr}:cl={ch_layout}:d={sil_dur:.3f}",
        ])

    # 归一化滤镜链；若同时指定了压缩档位，再叠加其缩放/降帧
    vf_chain = _video_filter_chain(w, h, fps)
    if options.compress is not None and options.compress.max_height and h > options.compress.max_height:
        th = options.compress.max_height
        tw = int(round(w * th / h))
        tw += tw % 2
        vf_chain += f",scale={tw}:{th}"
    if options.compress is not None and options.compress.fps_cap and fps > options.compress.fps_cap:
        vf_chain += f",fps={options.compress.fps_cap}"
    cmd.extend(["-vf", vf_chain])
    cmd.extend(["-af", _audio_filter_chain(sr, ch)])

    # 显式映射，避免多输入时的自动选择歧义
    cmd.extend(["-map", "0:v:0"])
    cmd.extend(["-map", "0:a:0" if info.has_audio else "1:a:0"])

    # 归一化编码：
    # - 有压缩档位且无淡变：直接套用档位参数（合并即压缩，一次成型）
    # - 有淡变：归一化只做"参数对齐"，不压缩。画面压缩留到最后的淡变
    #   阶段一次完成，否则会变成"编码两次"的二次损失。
    # - 无压缩档位：常规选项（仅把偏离片段拉齐到多数派）
    use_compress = (options.compress is not None
                    and not (options.fade_in or options.fade_out))
    if use_compress:
        cmd.extend(_compress_encoder_args(options.compress))
        abr = f"{options.compress.audio_kbps}k"
    else:
        cmd.extend(_video_encoder_args(options, env, resolved, profile))
        abr = options.audio_bitrate
    cmd.extend(["-c:a", "aac", "-b:a", abr,
                "-ac", str(ch), "-ar", str(sr)])

    if out_dur is not None:
        cmd.extend(["-t", f"{out_dur:.3f}"])
    if not info.has_audio:
        cmd.append("-shortest")

    # video_track_timescale 同样要对齐多数派：不匹配的 timescale 会让
    # 拼接时的 DTS 计算出现非单调，产生大量警告甚至播放异常。
    if timescale:
        cmd.extend(["-video_track_timescale", str(timescale)])
    cmd.extend(["-y", str(dst)])
    # 注意：这里刻意不加 -avoid_negative_ts make_zero。
    # 实测（本机 NVENC 路径）：该参数与 NVENC 组合会让输出起始时间变成
    # video=0.100s / audio=0.076s（NVENC 约 3 帧的编码器延迟被固化），
    # 造成约 24ms 的音画起点差。去掉后起始时间干净归零（0.000000）。
    # 防负时间戳的职责交给最终拼接阶段（见 build_lossless_cmd）。
    return cmd


def build_filter_concat_cmd(
    files: list[Path],
    dst: Path,
    target_video: dict,
    target_audio: dict,
    options: MergeOptions,
    env: FfmpegEnv,
    resolved: list[str],
    infos: list[MediaInfo],
) -> list[str]:
    """构建 concat 滤镜一次性重编码命令（兜底策略）。

    无音轨片段通过追加独立的 lavfi 静音输入来补齐 ——
    不能用 anull 接视频流（anull 是音频滤镜，且会重复消费同一路视频流）。
    静音源必须指定时长，否则无限长的源会让 concat 永不结束。
    """
    w, h = target_video["width"], target_video["height"]
    fps = target_video["fps"]
    sr = target_audio["sample_rate"]
    ch = target_audio["channels"]
    ch_layout = "stereo" if ch >= 2 else "mono"
    n = len(files)

    cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "warning"]

    # 先声明所有真实文件输入
    for f in files:
        cmd.extend(["-i", str(f)])

    # 再为无音轨片段追加静音输入（带时长）
    silence_idx: dict[int, int] = {}
    next_idx = n
    for i, info in enumerate(infos):
        if not info.has_audio:
            silence_idx[i] = next_idx
            dur = max(info.duration - options.trim_head - options.trim_tail, 0.1)
            cmd.extend(["-f", "lavfi", "-i", f"anullsrc=r={sr}:cl={ch_layout}:d={dur:.3f}"])
            next_idx += 1

    parts: list[str] = []
    for i, info in enumerate(infos):
        vf = _video_filter_chain(w, h, fps)
        # concat 要求各段时间戳从 0 开始，故重置 PTS
        parts.append(f"[{i}:v]{vf},setpts=PTS-STARTPTS[v{i}]")
        if info.has_audio:
            parts.append(f"[{i}:a]{_audio_filter_chain(sr, ch)}[a{i}]")
        else:
            parts.append(f"[{silence_idx[i]}:a]{_audio_filter_chain(sr, ch, reset_pts=False)}[a{i}]")

    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]")

    cmd.extend(["-filter_complex", ";".join(parts), "-map", "[outv]", "-map", "[outa]"])
    cmd.extend(_video_encoder_args(options, env, resolved))
    cmd.extend(["-c:a", "aac", "-b:a", options.audio_bitrate,
                "-ac", str(ch), "-ar", str(sr)])

    if options.trim_head > 0:
        # 裁剪在滤镜链中较难精确表达，此处退化为整体不裁剪并提示
        resolved.append("策略C 下片头裁剪未生效，建议改用默认策略B")
    cmd.extend(["-y", str(dst)])
    return cmd


def build_encode_concat_cmd(
    list_file: Path,
    dst: Path,
    target_video: dict,
    target_audio: dict,
    options: MergeOptions,
    env: FfmpegEnv,
    resolved: list[str],
) -> list[str]:
    """构建"合并即压缩"命令（策略E）。

    与策略B 的区别：策略B 是"先逐片段编码、再无损拼接"，
    策略E 是"直接用 concat demuxer 输入 + 一次编码"，
    中间不落地任何大文件，也少一次封装/解封装开销。

    适用条件：各片段参数一致（否则 concat demuxer 会因参数不匹配出错），
    因此 choose_strategy 只在 is_uniform 时选它。

    注意这里不用 -copyts：-copyts 是为 "-c copy" 保留原始时间戳设计的，
    重编码场景下由解码器/滤镜链自行生成正确时间戳；配合 concat demuxer
    会把各段偏移正确累加，最终输出干净。
    """
    preset = options.compress
    assert preset is not None, "策略E 必须指定压缩档位"

    w, h = target_video["width"], target_video["height"]
    fps = target_video["fps"]
    sr = target_audio["sample_rate"]
    ch = target_audio["channels"]

    cmd = [
        env.ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
    ]

    # 滤镜：按档位降分辨率 / 降帧率（原片参数已在目标之内则不加滤镜）
    vf: list[str] = []
    if preset.max_height and h > preset.max_height:
        target_h = preset.max_height
        target_w = int(round(w * target_h / h))
        target_w += target_w % 2          # 编码器要求偶数
        vf.append(f"scale={target_w}:{target_h}")
        vf.append("setsar=1")
        resolved.append(f"分辨率 {w}x{h} -> {target_w}x{target_h}")
    if preset.fps_cap and fps > preset.fps_cap:
        vf.append(f"fps={preset.fps_cap}")
        resolved.append(f"帧率 {fps:g} -> {preset.fps_cap}")
    if vf:
        cmd.extend(["-vf", ",".join(vf)])

    cmd.extend(_compress_encoder_args(preset))
    cmd.extend([
        "-c:a", "aac", "-b:a", f"{preset.audio_kbps}k",
        "-ac", str(ch), "-ar", str(sr),
        "-movflags", "+faststart",
        "-y", str(dst),
    ])
    return cmd


def build_lossless_cmd(list_file: Path, dst: Path, env: FfmpegEnv) -> list[str]:
    """构建无损拼接命令。

    时间戳处理很讲究，以下是实测结论（ffmpeg 7.1，768x1344@24fps 三段各 6s）：

      concat demuxer + -c copy，原始片段 start_time=0.000
        -> 输出 video=0.023031s / audio=0.000000s，时长 18.023220
           即视频被"推后"约 23ms（约 0.55 帧），时长多出 23ms

      加 -copyts 后
        -> 输出 video/audio 均为 0.000000s，时长精确 18.000000，
           帧数 432（18x24 精确匹配），三段音频位置与画面抽帧均正确

    因此选用 -copyts。它保留各输入的时间戳从 0 开始的原始语义，
    既不引入起始偏移，也不改变媒体数据本身（仍是 -c copy 零损失）。
    不加 -fflags +genpts / -avoid_negative_ts：两者都会把 NVENC 或
    封装层的固有延迟固化为可见的起始偏移。
    """
    return [
        env.ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-copyts",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        "-y", str(dst),
    ]


def build_fade_cmd(src: Path, dst: Path, options: MergeOptions,
                   total_duration: float, env: FfmpegEnv,
                   resolved: list[str]) -> list[str]:
    """对已合并成片应用淡入淡出（需重编码一次）。"""
    vf: list[str] = []
    af: list[str] = []
    if options.fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={options.fade_in:.3f}")
        af.append(f"afade=t=in:st=0:d={options.fade_in:.3f}")
    if options.fade_out > 0:
        st = max(total_duration - options.fade_out, 0)
        vf.append(f"fade=t=out:st={st:.3f}:d={options.fade_out:.3f}")
        af.append(f"afade=t=out:st={st:.3f}:d={options.fade_out:.3f}")

    cmd = [
        env.ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-copyts",
        "-i", str(src),
    ]
    if vf:
        cmd.extend(["-vf", ",".join(vf)])
    if af:
        cmd.extend(["-af", ",".join(af)])
    # 有压缩档位时用档位参数（这是整片唯一的压缩环节）；否则用常规选项
    if options.compress is not None:
        cmd.extend(_compress_encoder_args(options.compress))
        abr = f"{options.compress.audio_kbps}k"
    else:
        cmd.extend(_video_encoder_args(options, env, resolved))
        abr = options.audio_bitrate
    cmd.extend(["-c:a", "aac", "-b:a", abr])
    cmd.extend(["-movflags", "+faststart", "-y", str(dst)])
    return cmd


# ---------------------------------------------------------------------------
# 计划与执行
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    label: str
    cmd: list[str]
    duration: float = 0.0


def choose_strategy(report: ConsistencyReport, options: MergeOptions) -> Strategy:
    """决定采用哪档策略。

    选择逻辑（代价从低到高）：
      1. 指定了压缩档位：
         - 各片段参数一致 -> E 直连编码合并（一次成型，不产生中间大文件）
         - 存在差异       -> B 归一化拼接（归一化时直接套压缩参数）
      2. 完全一致且无需裁剪/淡变        -> A 无损拼接
      3. 存在少数偏离片段（多数派 >= 50%）-> D 最小重编码
         实测价值：36 集里只有 1 集是 4K/HEVC 时，只编码那 1 集，
         其余 35 集零损失，比全部重编码快一个数量级
      4. 其余情况                       -> B 全部归一化（覆盖所有差异类型）
    """
    if options.force_strategy is not None:
        return options.force_strategy

    # 有压缩档位：优先走"合并即压缩"
    if options.compress is not None:
        # 裁剪/淡变必须逐片段（或整片）加工，concat demuxer 直连无法表达；
        # 此时改走归一化路径 —— 它在归一化时同样套用压缩参数，
        # 仍然不产生中间大文件（逐片段已是压缩后体积）。
        if report.is_uniform and not options.needs_filter_processing:
            return Strategy.ENCODE_CONCAT
        return Strategy.NORMALIZE_CONCAT

    if report.is_uniform and not options.needs_reencode:
        return Strategy.LOSSLESS_CONCAT
    if options.needs_reencode:
        # 裁剪/淡变必须重编码，但这不意味着要重编码"全部"片段；
        # 若存在多数派，仍可只重编码偏离片段（裁剪参数会施加到所有片段，
        # 因此这里退回 B，保证每集都被均匀处理）
        return Strategy.NORMALIZE_CONCAT
    # 检查是否值得走最小重编码
    dev, maj = report.minimal_reencode_plan()
    if dev and maj:
        return Strategy.MINIMAL_REENCODE
    return Strategy.NORMALIZE_CONCAT


def _build_minimal_steps(
    items: list[MediaInfo],
    report: ConsistencyReport,
    options: MergeOptions,
    env: FfmpegEnv,
    workdir: Path,
    tv: dict,
    ta: dict,
    total: float,
) -> list[PlanStep]:
    """构建策略D 的步骤：只归一化偏离片段，其余原样复制。

    关键点：必须保持原始顺序 —— 把偏离片段替换为归一化后的版本，
    其余片段保持原路径，最终按原顺序生成 concat list。
    """
    resolved: list[str] = []
    dev, maj = report.minimal_reencode_plan()
    dev_ids = {id(i) for i in dev}
    # 关键：重编码片段要对齐多数派的 profile 与 timescale，才能无缝拼接
    prof = report.target_profile()
    ts = report.target_timescale()

    steps: list[PlanStep] = []
    ordered: list[Path] = []
    dev_no = 0

    for idx, info in enumerate(items, 1):
        if id(info) in dev_ids:
            dev_no += 1
            dst = workdir / f"reenc_{dev_no:04d}.mp4"
            cmd = build_normalize_cmd(info.path, dst, tv, ta, options, env,
                                      resolved, info, profile=prof, timescale=ts)
            steps.append(PlanStep(
                f"重编码偏离片段 {dev_no}/{len(dev)}: {info.name}",
                cmd, info.duration,
            ))
            ordered.append(dst)
        else:
            # 多数派片段：原样复制，不重编码
            ordered.append(info.path)

    list_file = workdir / "concat_list.txt"
    build_concat_list(ordered, list_file)
    steps.append(PlanStep(
        f"无损拼接 {len(ordered)} 个片段（其中 {len(maj)} 个未重编码）",
        build_lossless_cmd(list_file, options.output, env),
        total,
    ))
    return steps


def plan_merge(
    items: list[MediaInfo],
    report: ConsistencyReport,
    options: MergeOptions,
    env: FfmpegEnv,
    workdir: Path,
) -> tuple[Strategy, list[PlanStep]]:
    """制定合并计划：决定策略并生成命令序列。"""
    resolved: list[str] = []
    strategy = choose_strategy(report, options)
    tv = report.target_video_params()
    ta = report.target_audio_params()
    total = sum(i.duration for i in items)

    # 归一化会裁掉片头片尾，成片总时长需相应扣减
    if options.trim_head or options.trim_tail:
        total = max(total - (options.trim_head + options.trim_tail) * len(items), 0.1)

    steps: list[PlanStep] = []

    if strategy == Strategy.ENCODE_CONCAT:
        list_file = workdir / "concat_list.txt"
        build_concat_list([i.path for i in items], list_file)
        steps.append(PlanStep(
            f"合并即压缩：拼接 {len(items)} 个片段并编码（{options.compress.short}）",
            build_encode_concat_cmd(list_file, options.output, tv, ta,
                                    options, env, resolved),
            total,
        ))
        return strategy, steps

    if strategy == Strategy.LOSSLESS_CONCAT:
        list_file = workdir / "concat_list.txt"
        build_concat_list([i.path for i in items], list_file)
        steps.append(PlanStep(
            f"无损拼接 {len(items)} 个片段（不重编码）",
            build_lossless_cmd(list_file, options.output, env),
            total,
        ))
        return strategy, steps

    if strategy == Strategy.MINIMAL_REENCODE:
        return strategy, _build_minimal_steps(
            items, report, options, env, workdir, tv, ta, total)

    if strategy == Strategy.NORMALIZE_CONCAT:
        # 阶段1：逐片段归一化
        normalized: list[Path] = []
        for idx, info in enumerate(items, 1):
            dst = workdir / f"norm_{idx:04d}.mp4"
            normalized.append(dst)
            cmd = build_normalize_cmd(info.path, dst, tv, ta, options, env, resolved, info)
            eff_dur = info.duration
            if options.trim_head or options.trim_tail:
                eff_dur = max(info.duration - options.trim_head - options.trim_tail, 0.1)
            steps.append(PlanStep(
                f"归一化 {idx}/{len(items)}: {info.name}", cmd, eff_dur,
            ))

        # 阶段2：拼接（可选淡变）
        list_file = workdir / "concat_list.txt"
        build_concat_list(normalized, list_file)
        if options.fade_in or options.fade_out:
            # 淡变必须整片处理，故先无损拼接再统一编码一次。
            # 归一化阶段已刻意不压缩（见 build_normalize_cmd），
            # 因此这里的编码是唯一一次压缩，不存在二次损失。
            joined = workdir / "joined.mp4"
            steps.append(PlanStep(
                f"无损拼接 {len(normalized)} 个归一化片段",
                build_lossless_cmd(list_file, joined, env), total,
            ))
            fade_label = "应用整片淡入/淡出"
            if options.compress is not None:
                fade_label += f"并编码（{options.compress.short}）"
            steps.append(PlanStep(
                fade_label,
                build_fade_cmd(joined, options.output, options, total, env, resolved), total,
            ))
        else:
            steps.append(PlanStep(
                f"无损拼接 {len(normalized)} 个归一化片段",
                build_lossless_cmd(list_file, options.output, env), total,
            ))
        return strategy, steps

    # 策略 C：滤镜一次性重编码
    steps.append(PlanStep(
        f"滤镜拼接重编码 {len(items)} 个片段",
        build_filter_concat_cmd([i.path for i in items], options.output,
                                tv, ta, options, env, resolved, items),
        total,
    ))
    return strategy, steps


def execute_merge(
    items: list[MediaInfo],
    report: ConsistencyReport,
    options: MergeOptions,
    env: FfmpegEnv,
) -> MergeResult:
    """执行合并。"""
    t0 = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="drama_merger_"))
    options.output.parent.mkdir(parents=True, exist_ok=True)
    combined_log: list[str] = []

    from .probe import probe_file  # 局部导入避免循环依赖

    def run_steps(strategy: Strategy, steps: list[PlanStep]) -> None:
        for step in steps:
            combined_log.append(f"$ {' '.join(str(c) for c in step.cmd)}")
            code = _run_ffmpeg_with_progress(step.cmd, step.duration, step.label,
                                             options, combined_log)
            if code != 0:
                tail = "\n".join(combined_log[-8:])
                raise RuntimeError(
                    f"步骤失败：{step.label}\n"
                    f"命令：{' '.join(str(c) for c in step.cmd[:10])} ...\n"
                    f"日志尾部：\n{tail}"
                )

    try:
        strategy, steps = plan_merge(items, report, options, env, workdir)

        # collected_warnings 需要从 plan 里传出来，这里重新收集一次
        warnings: list[str] = []

        try:
            run_steps(strategy, steps)
        except RuntimeError as exc:
            # 自动降级：E/D/B 失败时退到 C（滤镜重编码，兼容性最强）
            if strategy in (Strategy.ENCODE_CONCAT, Strategy.MINIMAL_REENCODE,
                            Strategy.NORMALIZE_CONCAT) \
                    and options.force_strategy is None:
                warnings.append(
                    f"{strategy.label.split('·')[0].strip()} 执行失败，"
                    f"已自动降级到策略C：{str(exc)[:200]}"
                )
                strategy = Strategy.FILTER_CONCAT
                combined_log.clear()
                _, csteps = plan_merge(items, report,
                                       _replace(options, force_strategy=Strategy.FILTER_CONCAT),
                                       env, workdir)
                run_steps(strategy, csteps)
            else:
                raise

        elapsed = time.time() - t0
        out_size = options.output.stat().st_size if options.output.is_file() else 0
        if out_size == 0:
            raise RuntimeError(f"输出文件为空，合并未成功：{options.output}")

        return MergeResult(
            output=options.output,
            strategy=strategy,
            duration=sum(i.duration for i in items),
            size=out_size,
            elapsed=elapsed,
            encoder_used=_describe_encoder(options, env, strategy),
            log=combined_log,
            warnings=warnings,
        )
    finally:
        if not options.keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"[保留临时目录] {workdir}")


def _replace(options: MergeOptions, **changes) -> MergeOptions:
    """复制并覆写 MergeOptions 的部分字段。"""
    from dataclasses import replace

    return replace(options, **changes)


def _describe_encoder(options: MergeOptions, env: FfmpegEnv,
                      strategy: Strategy | None = None) -> str:
    # 无损拼接不经过编码器，报告编码器会误导用户
    if strategy == Strategy.LOSSLESS_CONCAT:
        return "无（直接复制原始码流，未重新编码）"
    # 合并即压缩：报告实际档位
    if options.compress is not None:
        p = options.compress
        codec = f"{p.vcodec_name} ({p.speed}, crf {p.crf})"
        extra = []
        if p.max_height:
            extra.append(f"{p.max_height}p 上限")
        if p.fps_cap:
            extra.append(f"{p.fps_cap}fps 上限")
        suffix = f" · {'/'.join(extra)}" if extra else ""
        return f"{codec} · 音频 {p.audio_kbps}k{suffix}"
    if strategy == Strategy.MINIMAL_REENCODE:
        # 只重编码了部分片段
        if options.encoder == Encoder.X264 or not env.nvenc_available:
            return f"libx264 ({options.preset}, crf {options.crf}) · 仅用于偏离片段"
        return f"h264_nvenc ({options.nvenc_preset}, cq {options.crf}) · 仅用于偏离片段"
    if options.encoder == Encoder.X264:
        return f"libx264 ({options.preset}, crf {options.crf})"
    if env.nvenc_available:
        return f"h264_nvenc ({options.nvenc_preset}, cq {options.crf})"
    return f"libx264 ({options.preset}, crf {options.crf})"
