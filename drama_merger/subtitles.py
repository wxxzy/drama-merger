"""
字幕合并与时间轴重映射。

短剧合并的一个隐蔽陷阱：每一集的字幕文件都各自从 00:00:00 开始计时。
直接拼接字幕会让第 2 集之后的字幕全部"早到"——第 2 集的对白会出现在第 1 集的时间段里。
必须按每个片段在成片中的起始位置做整体平移。

支持：SRT / VTT / ASS 三种主流格式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}


@dataclass
class Cue:
    """一条字幕。"""

    start: float
    end: float
    text: str
    index: int = 0

    def shifted(self, offset: float) -> "Cue":
        return Cue(self.start + offset, self.end + offset, self.text, self.index)


# ---------------------------------------------------------------------------
# 时间戳工具
# ---------------------------------------------------------------------------

def parse_srt_time(s: str) -> float:
    """解析 00:01:23,456 或 00:01:23.456 -> 83.456"""
    s = s.strip().replace(",", ".")
    m = re.match(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})", s)
    if not m:
        m2 = re.match(r"(\d+):(\d{2}):(\d{2})", s)
        if not m2:
            return 0.0
        h, mi, sec = (int(m2.group(i)) for i in (1, 2, 3))
        return h * 3600 + mi * 60 + sec
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # 毫秒部分补齐到 3 位后再转整数，避免 ".5" 被误当作 5ms（应为 500ms）
    ms_val = int(m.group(4).ljust(3, "0"))
    return h * 3600 + mi * 60 + sec + ms_val / 1000.0


def format_srt_time(t: float) -> str:
    """83.456 -> 00:01:23,456"""
    t = max(t, 0.0)
    h = int(t // 3600)
    mi = int((t % 3600) // 60)
    sec = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms -= 1000
        sec += 1
    return f"{h:02d}:{mi:02d}:{sec:02d},{ms:03d}"


def format_vtt_time(t: float) -> str:
    """83.456 -> 00:01:23.456"""
    return format_srt_time(t).replace(",", ".")


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def parse_srt(content: str) -> list[Cue]:
    """解析 SRT / VTT（两者格式高度相似）。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # 去掉 VTT 头
    content = re.sub(r"^WEBVTT.*?\n\n", "", content, flags=re.DOTALL)
    cues: list[Cue] = []
    # 以空行分块
    blocks = re.split(r"\n\s*\n", content)
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = 0
        time_line_i = 0
        if re.match(r"^\d+$", lines[0].strip()):
            idx = int(lines[0].strip())
            time_line_i = 1
        if time_line_i >= len(lines):
            continue
        tl = lines[time_line_i]
        m = re.match(
            r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", tl
        )
        if not m:
            continue
        start = parse_srt_time(m.group(1))
        end = parse_srt_time(m.group(2))
        text = "\n".join(lines[time_line_i + 1:]).strip()
        if text:
            cues.append(Cue(start, end, text, idx))
    return cues


def parse_ass(content: str) -> tuple[list[Cue], str]:
    """解析 ASS/SSA，返回 (字幕列表, 头部信息)。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    header_lines: list[str] = []
    cues: list[Cue] = []
    in_events = False
    fmt_fields: list[str] = []

    for line in lines:
        if line.strip().startswith("[Events]"):
            in_events = True
            header_lines.append(line)
            continue
        if in_events:
            if line.strip().startswith("Format:"):
                fmt_fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
                if not cues:  # 只保留第一个 Format 行（作为输出模板）
                    header_lines.append(line)
                continue
            if line.strip().startswith("Dialogue:"):
                parts = line.split(":", 1)[1].split(",", len(fmt_fields) - 1)
                if len(parts) >= len(fmt_fields):
                    d = dict(zip(fmt_fields, parts))
                    try:
                        start = parse_ass_time(d.get("start", "0:00:00.00"))
                        end = parse_ass_time(d.get("end", "0:00:00.00"))
                    except Exception:  # noqa: BLE001
                        continue
                    text = d.get("text", "")
                    cues.append(Cue(start, end, text))
                continue
            if line.strip().startswith(("Comment:", "Picture:", "Sound:", "Movie:", "Command:")):
                continue
            header_lines.append(line)
        else:
            header_lines.append(line)

    return cues, "\n".join(header_lines)


def parse_ass_time(s: str) -> float:
    """ASS 时间格式 0:00:01.23（末位是厘秒）"""
    m = re.match(r"\s*(\d+):(\d{2}):(\d{2})[.:](\d{1,2})", s)
    if not m:
        return parse_srt_time(s)
    h, mi, sec, cs = (int(m.group(i)) for i in (1, 2, 3, 4))
    cs_val = int(str(cs).ljust(2, "0"))
    return h * 3600 + mi * 60 + sec + cs_val / 100.0


def format_ass_time(t: float) -> str:
    """83.46 -> 0:01:23.46"""
    t = max(t, 0.0)
    h = int(t // 3600)
    mi = int((t % 3600) // 60)
    sec = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        cs -= 100
        sec += 1
    return f"{h}:{mi:02d}:{sec:02d}.{cs:02d}"


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def to_srt(cues: list[Cue]) -> str:
    out: list[str] = []
    for i, c in enumerate(sorted(cues, key=lambda x: x.start), 1):
        out.append(str(i))
        out.append(f"{format_srt_time(c.start)} --> {format_srt_time(c.end)}")
        out.append(c.text)
        out.append("")
    return "\n".join(out)


def to_vtt(cues: list[Cue]) -> str:
    out: list[str] = ["WEBVTT", ""]
    for c in sorted(cues, key=lambda x: x.start):
        out.append(f"{format_vtt_time(c.start)} --> {format_vtt_time(c.end)}")
        out.append(c.text)
        out.append("")
    return "\n".join(out)


def to_ass(cues: list[Cue], header: str) -> str:
    out: list[str] = [header.rstrip()]
    for c in sorted(cues, key=lambda x: x.start):
        out.append(
            f"Dialogue: 0,{format_ass_time(c.start)},{format_ass_time(c.end)},"
            f"Default,,0,0,0,,{c.text}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

@dataclass
class EpisodeSubtitle:
    """一集的字幕（可能对应多个文件，如中文/英文）。"""

    video_path: Path
    srt_files: list[Path]
    offset: float = 0.0


def find_subtitles_for(video: Path, extra_search_dirs: list[Path] | None = None) -> list[Path]:
    """
    为一集视频寻找配套字幕。
    匹配策略：同目录同名（换扩展名）> 同目录同 stem 前缀 > 同目录唯一字幕文件。
    """
    dirs = [video.parent] + (extra_search_dirs or [])
    stem = video.stem
    found: list[Path] = []

    for d in dirs:
        if not d.is_dir():
            continue
        # 1) 精确同名
        for ext in SUBTITLE_EXTS:
            cand = d / f"{stem}{ext}"
            if cand.is_file():
                found.append(cand)
        if found:
            break
        # 2) 同名带语言后缀，如 ep01.zh.srt / ep01_chs.srt
        for sub in sorted(d.iterdir()):
            if sub.suffix.lower() in SUBTITLE_EXTS and sub.stem.lower().startswith(stem.lower()):
                if sub.stem.lower() != stem.lower():
                    found.append(sub)
        if found:
            break
        # 3) 该目录下唯一的字幕文件（多集视频在一个目录时不用这条）
        all_subs = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in SUBTITLE_EXTS]
        if len(all_subs) == 1:
            found.append(all_subs[0])
            break

    return found


def merge_subtitles(
    episodes: list[EpisodeSubtitle],
    output_path: Path,
    format_hint: str | None = None,
    shift_correction: dict[Path, float] | None = None,
) -> tuple[Path | None, str]:
    """
    合并多集字幕为整片字幕。

    episodes: 每集的视频路径、字幕文件列表、以及该集在成片中的时间偏移（秒）
    offset 通常由调用方按片段实际时长累加计算得到。

    返回 (输出字幕路径 | None, 说明信息)
    """
    all_cues: list[Cue] = []
    ass_header = ""
    used_format = format_hint
    files_used: list[str] = []

    for ep in episodes:
        if not ep.srt_files:
            continue
        for srt in ep.srt_files:
            try:
                content = srt.read_text(encoding="utf-8-sig", errors="replace")
            except Exception:  # noqa: BLE001
                try:
                    content = srt.read_text(encoding="gbk", errors="replace")
                except Exception:  # noqa: BLE001
                    continue

            ext = srt.suffix.lower()
            if ext in (".ass", ".ssa"):
                cues, header = parse_ass(content)
                if not ass_header:
                    ass_header = header
                if not used_format:
                    used_format = "ass"
            else:
                cues = parse_srt(content)
                if not used_format:
                    used_format = "vtt" if ext == ".vtt" else "srt"

            if not cues:
                continue

            extra = (shift_correction or {}).get(srt, 0.0)
            off = ep.offset + extra
            all_cues.extend(c.shifted(off) for c in cues)
            files_used.append(f"{srt.name} (+{off:.2f}s, {len(cues)}条)")

    if not all_cues:
        return None, "未找到任何可用字幕"

    all_cues.sort(key=lambda c: c.start)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = used_format or "srt"
    if fmt == "ass" and ass_header:
        text = to_ass(all_cues, ass_header)
    elif fmt == "vtt":
        text = to_vtt(all_cues)
    else:
        text = to_srt(all_cues)

    with open(output_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)

    total_dur = max(c.end for c in all_cues)
    info = (
        f"合并 {len(files_used)} 个字幕文件，共 {len(all_cues)} 条字幕，"
        f"覆盖时长 {total_dur:.2f}s\n  " + "\n  ".join(files_used)
    )
    return output_path, info


def burn_subtitles(
    video_in: Path,
    subtitle: Path,
    video_out: Path,
    env,
    font_size: int = 20,
    margin_v: int = 40,
    font_name: str = "Microsoft YaHei",
    options=None,
) -> list[str]:
    """
    把字幕烧入视频（硬字幕）。
    注意：本机 ffmpeg 未启用 freetype/fontconfig 的完整字体渲染链，
    因此优先使用 ass 滤镜（libass 可用）。
    """
    sub_path = str(subtitle).replace("\\", "/").replace(":", r"\:")
    # 竖屏短剧：字幕偏下，字号按分辨率自适应
    style = (
        f"FontName={font_name},FontSize={font_size},"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV={margin_v}"
    )
    vf = f"subtitles='{sub_path}':force_style='{style}'"
    cmd = [
        env.ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-i", str(video_in),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy",
        "-y", str(video_out),
    ]
    return cmd
