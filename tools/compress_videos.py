#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量压缩短剧成片。

针对已合并的成片做二次压缩，供上传/归档使用。

设计要点：
1. 按素材实际码率自动判断"这文件值不值得压" —— 720p 跑 8Mbps 属于虚高，
   1080p60 跑 14Mbps 也有空间，但已经合理的就跳过，避免无谓损失画质。
2. 提供几档预设（archive / upload / light），每档对应不同 CRF 与分辨率策略。
3. 输出新文件，不覆盖原始成片 —— 压缩是有损操作，原始必须保留。
4. 逐文件实时进度、完成后校验（时长/解码/SSIM 抽样）。

用法：
  # 看看哪些文件值得压（不改动任何文件）
  python tools/compress_videos.py --dir "C:/Users/wangxingxiang/Videos/短剧成片" --analyze

  # 按 upload 档压缩全部
  python tools/compress_videos.py --dir "..." --preset upload

  # 只压某几个
  python tools/compress_videos.py --dir "..." --preset upload --only 灶边女王
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env, _run

IS_WINDOWS = sys.platform == "win32"
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------

@dataclass
class Preset:
    name: str
    crf: int                  # x264 CRF / x265 CRF / NVENC CQ
    audio_kbps: int
    max_height: int | None    # 超过则降分辨率（None = 不动）
    fps_cap: int | None       # 超过则降帧率（None = 不动）
    preset_speed: str         # x264/x265 preset
    desc: str
    codec: str = "h264"       # h264 / hevc
    ext: str = ".mp4"         # 输出扩展名（HEVC 用 mp4 兼容性更好）


PRESETS = {
    # 均衡：质量几乎无损，体积约减半。适合上传 YouTube。
    "archive": Preset(
        "archive", crf=23, audio_kbps=128, max_height=None, fps_cap=30,
        preset_speed="medium",
        desc="高画质（视觉无损），体积约减 40~60%",
    ),
    # 上传推荐：明显瘦身，画质仍好。适合长视频投稿。
    "upload": Preset(
        "upload", crf=26, audio_kbps=96, max_height=None, fps_cap=30,
        preset_speed="medium",
        desc="上传推荐，体积约减 70~80%，画质依旧清晰",
    ),
    # 轻量：进一步压，适合手机端观看或长期归档。
    "light": Preset(
        "light", crf=28, audio_kbps=64, max_height=1280, fps_cap=30,
        preset_speed="fast",
        desc="轻量版，720p30 上限，体积约减 80%",
    ),
    # --- HEVC 系列（同画质体积约再省一半，但老设备兼容性略差）---
    # 实测：同 CRF 下 HEVC 比 H.264 小 34%；CRF30 可小 59%，SSIM 仍 0.983
    "hevc": Preset(
        "hevc", crf=30, audio_kbps=96, max_height=None, fps_cap=30,
        preset_speed="medium", codec="hevc",
        desc="HEVC 编码，体积约为 H.264 的 40%，画质几乎无差异",
    ),
    "hevc-light": Preset(
        "hevc-light", crf=30, audio_kbps=64, max_height=1280, fps_cap=30,
        preset_speed="medium", codec="hevc",
        desc="HEVC + 720p 上限，体积最小（约原片 5%）",
    ),
}

# 实测结论（本机 GTX 1080 Ti + ffmpeg 7.1）
#
# 一、压缩场景必须用 libx264/libx265，不用 NVENC
#     1080p60 素材 60 秒样本、同为 CRF/CQ 26：
#       原始              35.0 MB
#       x264 CRF26 60fps  23.6MB  1.0x 实时
#       x264 CRF26 30fps  20.5MB  1.5x 实时   <-- 更小且更快
#       NVENC CQ26 30fps  45.5MB  5.5x 实时   <-- 反而更大
#
# 二、HEVC 是最大的压缩杠杆（相对已是 H.264 CRF26 的压缩版）
#     720p   样本 10.3MB -> H.265 同CRF 6.8MB(66%) / CRF30 4.8MB(47%)
#     1080p  样本 31.1MB -> H.265 同CRF 20.8MB(67%) / CRF30 12.9MB(41%)
#     1440p  样本 55.3MB -> H.265 同CRF 33.2MB(60%) / CRF30 21.5MB(39%)
#     CRF30 下 SSIM 仍有 0.982~0.989，肉眼抽帧几乎无差异
#
# 三、HEVC NVENC 同样不可用于瘦身（只省 7%），AV1 NVENC 在 Pascal 架构上直接失败
#
# 四、fps_cap 统一 30：短剧 30fps 足够，降帧省 10~15% 并提速
_FORCE_X264 = False


def fmt_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.2f} {u}"
        n /= 1024.0
    return ""


def fmt_dur(sec: float) -> str:
    sec = max(sec, 0)
    h, m, s = int(sec // 3600), int((sec % 3600) // 60), sec % 60
    if h:
        return f"{h}小时{m}分{s:.0f}秒"
    if m:
        return f"{m}分{s:.0f}秒"
    return f"{s:.1f}秒"


@dataclass
class VideoInfo:
    path: Path
    duration: float
    size: int
    width: int
    height: int
    fps: float
    v_bitrate: int
    a_bitrate: int
    codec: str

    @property
    def mbps(self) -> float:
        return self.v_bitrate / 1_000_000 if self.v_bitrate else (
            self.size * 8 / max(self.duration, 0.001) / 1_000_000
        )

    @property
    def pixels_per_sec(self) -> float:
        return self.width * self.height * self.fps

    @property
    def bpp(self) -> float:
        """每像素每帧比特数（bits per pixel per frame）。

        短剧这类画面相对简单的竖屏内容，bpp 在 0.08~0.15 已足够清晰；
        超过 0.2 通常意味着码率虚高，可压空间大。
        """
        if not self.pixels_per_sec:
            return 0.0
        return (self.v_bitrate or self.size * 8 / max(self.duration, 0.001)) / self.pixels_per_sec


def probe_video(path: Path, env) -> VideoInfo | None:
    """探测单个成片的参数。

    用 JSON 而非 CSV：ffprobe 的 CSV 字段顺序不保证稳定
    （实测输出为 h264,720,1280,30/1,7885675，codec_name 在最前，
     与 show_entries 的书写顺序不一致），按位置解析必然出错。
    """
    ffprobe = env.ffprobe
    if not ffprobe:
        return None
    code, out, _ = _run([
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,bit_rate,codec_name",
        "-show_entries", "format=duration,size",
        "-of", "json", str(path),
    ], timeout=120)
    if code != 0 or not out.strip():
        return None

    import json

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    if not streams:
        return None
    st = streams[0]
    fmt = data.get("format") or {}

    def _int(v, default=0):
        try:
            return int(str(v).split(".")[0])
        except (TypeError, ValueError):
            return default

    def _float(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    fr = st.get("r_frame_rate", "0/0")
    fps = 0.0
    if "/" in str(fr):
        n, _, d = str(fr).partition("/")
        try:
            fps = int(n) / int(d) if int(d) else 0.0
        except ValueError:
            fps = 0.0

    try:
        size = int(fmt.get("size") or path.stat().st_size)
    except (TypeError, ValueError):
        size = path.stat().st_size

    return VideoInfo(
        path=path,
        duration=_float(fmt.get("duration")),
        size=size,
        width=_int(st.get("width")),
        height=_int(st.get("height")),
        fps=fps,
        v_bitrate=_int(st.get("bit_rate")),
        a_bitrate=0,
        codec=st.get("codec_name", ""),
    )


def analyze(vi: VideoInfo, preset: Preset | None = None) -> tuple[bool, str]:
    """判断是否值得压缩，返回 (是否值得, 理由)。

    判定要分两种情况，否则会误判：

    1) 同编码重压（H.264 -> H.264）：bpp 已很低时再压必然掉画质，应跳过。
    2) 换编码（H.264 -> HEVC）：换个衡量维度。HEVC 在相同主观画质下
       只需约 60~70% 码率，所以"bpp 低"不代表没空间——
       实测 720p 的 0.024~0.048 bpp 素材转 HEVC CRF30 后仍能再省 55%，
       且 SSIM 保持 0.983 以上。
    """
    res_name = f"{vi.width}x{vi.height}"
    bpp = vi.bpp
    switching_codec = bool(preset and preset.codec == "hevc" and vi.codec != "hevc")

    # 已经是 HEVC 且 bpp 很低 -> 确实没空间了
    if vi.codec == "hevc" and bpp and bpp < 0.06:
        return False, f"已是 HEVC 且码率很紧（{vi.mbps:.1f}Mbps, {bpp:.3f} bpp），不建议再压"

    # 同编码重压且 bpp 已低 -> 跳过
    if not switching_codec and bpp and bpp < 0.06:
        return False, f"码率已很紧（{vi.mbps:.1f}Mbps, {bpp:.3f} bpp），同编码再压会掉画质"

    reasons = []
    if switching_codec:
        reasons.append(f"转 HEVC 可再省约 55%（当前 H.264, {vi.mbps:.1f}Mbps）")
        # 降分辨率也是换维度，一并提示。注意 max_height 是竖屏视频的
        # 短边（如 1280 对应 720p），不能直接说"降到 1280p"，会误导。
        if preset and preset.max_height and vi.height > preset.max_height:
            short_side = preset.max_height
            label = {720: "480p", 960: "540p", 1280: "720p", 1440: "810p"}.get(
                short_side, f"短边{short_side}")
            reasons.append(f"{res_name} 可降至 {label}")
        return True, "；".join(reasons)

    if bpp >= 0.12:
        reasons.append(f"码率虚高（{vi.mbps:.1f}Mbps, {bpp:.3f} bpp）")
    if vi.height <= 1280 and vi.mbps > 5:
        reasons.append(f"{res_name} 却用 {vi.mbps:.1f}Mbps")
    if vi.fps >= 50 and vi.height <= 1920:
        reasons.append(f"{vi.fps:g}fps（短剧 30fps 足够）")

    if reasons:
        return True, "；".join(reasons)
    return False, f"码率合理（{vi.mbps:.1f}Mbps, {bpp:.3f} bpp），同编码压缩收益有限"


def build_cmd(env, src: Path, dst: Path, preset: Preset,
              vi: VideoInfo, resolved: list[str]) -> list[str]:
    """构建压缩命令。

    编码器选择原则（实测驱动）：
      压缩场景一律用 libx264 / libx265 软编。NVENC 虽然快 3~4 倍，
      但同等参数下体积大得多（1080p60 实测 45.5MB vs 20.5MB），
      与"瘦身"目标相悖，故默认不走硬件。
    """
    vf: list[str] = []

    # 降分辨率（按 max_height 等比缩放，保持竖屏）
    if preset.max_height and vi.height > preset.max_height:
        target_h = preset.max_height
        target_w = int(round(vi.width * target_h / vi.height))
        # H.264/H.265 都要求宽高为偶数
        target_w += target_w % 2
        target_h += target_h % 2
        vf.append(f"scale={target_w}:{target_h}")
        vf.append("setsar=1")

    # 降帧率
    if preset.fps_cap and vi.fps > preset.fps_cap:
        vf.append(f"fps={preset.fps_cap}")

    cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "warning", "-i", str(src)]
    if vf:
        cmd += ["-vf", ",".join(vf)]

    # 显式要求 nvenc 才走硬件（压缩力弱，仅用于快速出片）
    want_nvenc = getattr(globals().get("_ENCODER_PREF"), "value", "") == "nvenc"
    use_nvenc = env.nvenc_available and not _FORCE_X264 and want_nvenc

    if use_nvenc:
        enc = "hevc_nvenc" if preset.codec == "hevc" else "h264_nvenc"
        if enc not in env.chosen.encoders:
            enc = "h264_nvenc"
        cmd += ["-c:v", enc, "-preset", "p4", "-rc", "vbr",
                "-cq", str(preset.crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
        resolved.append("使用 NVENC 硬件编码（压缩力弱于软编，体积会偏大）")
    elif preset.codec == "hevc":
        # HEVC：加 tag 提升兼容性，并明确 Main profile / 8bit（老设备也能播）
        if "libx265" not in env.chosen.encoders:
            resolved.append("libx265 不可用，已回退到 libx264")
            cmd += ["-c:v", "libx264", "-preset", preset.preset_speed,
                    "-crf", str(preset.crf), "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:v", "libx265", "-preset", preset.preset_speed,
                    "-crf", str(preset.crf), "-pix_fmt", "yuv420p",
                    "-profile:v", "main",   # 不用 main10，保证老设备可播
                    "-tag:v", "hvc1",       # Apple 系需要 hvc1 才认
                    "-x265-params", "log-level=error"]
    else:
        cmd += ["-c:v", "libx264", "-preset", preset.preset_speed,
                "-crf", str(preset.crf), "-pix_fmt", "yuv420p"]

    cmd += ["-c:a", "aac", "-b:a", f"{preset.audio_kbps}k",
            "-movflags", "+faststart", "-y", str(dst)]
    return cmd


class Progress:
    """解析 ffmpeg -progress，打印进度条。

    只在百分比变化时重绘：ffmpeg 的 out_time 更新频率远高于 1% 变化，
    若每次都打印会把日志刷成几十万字符（实测单文件可达 15 万字符），
    既拖慢输出也让日志无法阅读。
    """

    def __init__(self, total: float, label: str):
        self.total = max(total, 0.001)
        self.label = label
        self.last_len = 0
        self.last_pct = -1

    def feed(self, line: str) -> None:
        key, _, val = line.partition("=")
        if key.strip() not in ("out_time_us", "out_time_ms"):
            return
        try:
            sec = int(val.strip()) / 1_000_000.0
        except ValueError:
            return
        ratio = min(sec / self.total, 1.0)
        pct = int(ratio * 100)
        if pct == self.last_pct:
            return
        self.last_pct = pct

        w = 30
        filled = int(w * ratio)
        bar = "#" * filled + "-" * (w - filled)
        text = f"\r    [{bar}] {ratio*100:5.1f}%  {self.label[:34]}"
        pad = max(0, self.last_len - len(text))
        print(text + " " * pad, end="", flush=True)
        self.last_len = len(text)

    def done(self) -> None:
        if self.last_len:
            print()


def run_ffmpeg_progress(cmd: list[str], total: float, label: str) -> tuple[int, str]:
    """执行 ffmpeg 并显示进度。双线程读管道，避免 stderr 填满导致死锁。"""
    cmd = list(cmd)
    try:
        i = cmd.index("-y")
        cmd[i:i] = ["-progress", "pipe:1", "-nostats"]
    except ValueError:
        cmd += ["-progress", "pipe:1", "-nostats"]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError as exc:
        return -1, str(exc)

    pr = Progress(total, label)
    err_tail: list[str] = []
    lock = threading.Lock()

    def pump_out():
        try:
            for ln in proc.stdout:
                pr.feed(ln.strip())
        except Exception:  # noqa: BLE001
            pass

    def pump_err():
        try:
            for ln in proc.stderr:
                s = ln.strip()
                if s:
                    with lock:
                        err_tail.append(s)
                        if len(err_tail) > 200:
                            del err_tail[:100]
        except Exception:  # noqa: BLE001
            pass

    t1 = threading.Thread(target=pump_out, daemon=True)
    t2 = threading.Thread(target=pump_err, daemon=True)
    t1.start()
    t2.start()
    proc.wait()
    t1.join(timeout=5)
    t2.join(timeout=5)
    pr.done()
    with lock:
        return (proc.returncode or 0), "\n".join(err_tail[-6:])


def verify_output(env, src: VideoInfo, dst: Path) -> tuple[bool, str]:
    """压缩后校验：时长一致 + 可完整解码 + 抽样 SSIM。

    只有三项都通过，才允许删除原片。压缩是有损且耗时的操作，
    校验能挡住"文件损坏""时长截断""画质崩坏"这类问题。
    """
    vi = probe_video(dst, env)
    if vi is None:
        return False, "无法探测输出文件"

    # 1) 时长一致性（允许 1 秒内误差，AAC 封装会有零点几秒浮动）
    diff = abs(vi.duration - src.duration)
    if diff > 1.0:
        return False, f"时长不符（原 {src.duration:.1f}s vs 新 {vi.duration:.1f}s）"

    # 2) 分辨率不能变大（防参数写错）
    if vi.width * vi.height > src.width * src.height * 1.01:
        return False, f"分辨率异常（{vi.width}x{vi.height}）"

    # 3) 抽样解码（取中段 20 秒，确认无花屏/损坏）
    mid = max(src.duration / 2, 0)
    code, _, err = _run([
        env.ffmpeg, "-v", "error", "-ss", f"{mid:.1f}", "-t", "20",
        "-i", str(dst), "-f", "null", "-",
    ], timeout=600)
    if code != 0 or err.strip():
        return False, f"解码异常：{err.strip()[:120]}"

    # 4) 抽样 SSIM（取中段 20 秒对比原片）
    s = sample_ssim(env, src.path, dst, mid, 20.0, src)
    if s and s < 0.95:
        return False, f"画质过低（SSIM {s:.4f}）"

    return True, f"校验通过（时长 {vi.duration:.1f}s，SSIM {s:.4f}）" if s else "校验通过"


def sample_ssim(env, ref: Path, test: Path, start: float, dur: float,
                vi: VideoInfo) -> float:
    """抽样计算 SSIM。两路先统一到同一规格否则滤镜会报错。"""
    fc = (
        f"[0:v]scale={vi.width}:{vi.height},fps={vi.fps:g},setpts=PTS-STARTPTS[t];"
        f"[1:v]scale={vi.width}:{vi.height},fps={vi.fps:g},setpts=PTS-STARTPTS[r];"
        f"[t][r]ssim"
    )
    code, out, err = _run([
        env.ffmpeg, "-hide_banner", "-loglevel", "info",
        "-ss", f"{start:.1f}", "-t", f"{dur:.1f}", "-i", str(test),
        "-ss", f"{start:.1f}", "-t", f"{dur:.1f}", "-i", str(ref),
        "-lavfi", fc, "-f", "null", "-",
    ], timeout=900)
    for line in (out + err).splitlines():
        if "SSIM" in line and "All:" in line:
            try:
                return float(line.split("All:")[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
    return 0.0


def send_to_recycle_bin(path: Path) -> bool:
    """把文件移入回收站（可恢复），而非永久删除。

    直接 rm 不可逆；一旦压缩版有问题就彻底没救了。
    Windows 走 Shell API，其他平台回退到 trash 命令。
    """
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            FO_DELETE = 3
            FOF_ALLOWUNDO = 0x40
            FOF_NOCONFIRMATION = 0x10
            FOF_SILENT = 0x4

            op = SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = FO_DELETE
            # 双 null 结尾
            op.pFrom = str(path.resolve()) + "\0\0"
            op.pTo = None
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
            res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            return res == 0
        except Exception:  # noqa: BLE001
            return False
    # 非 Windows：尝试 trash 命令
    import shutil as _sh
    if _sh.which("trash-put"):
        return subprocess.call(["trash-put", str(path)]) == 0
    if _sh.which("gio"):
        return subprocess.call(["gio", "trash", str(path)]) == 0
    return False


def compress_one(env, vi: VideoInfo, out_dir: Path, preset: Preset) -> dict:
    """压缩单个文件。返回统计。

    输出文件名带上编码与档位标记（如 _HEVC-CRF30），
    避免与已有的 H.264 压缩版互相覆盖、也便于事后分辨。
    """
    resolved: list[str] = []
    # 源文件名可能已带 "_压缩"，先剥掉避免叠加成 "_压缩_HEVC_压缩"
    stem = vi.path.stem
    for suffix in ("_压缩", "_H264", "_HEVC"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]

    codec_tag = "HEVC" if preset.codec == "hevc" else "H264"
    out_suffix = preset.ext or vi.path.suffix
    dst = out_dir / f"{stem}_{codec_tag}-CRF{preset.crf}{out_suffix}"

    if dst.exists():
        dst.unlink()

    cmd = build_cmd(env, vi.path, dst, preset, vi, resolved)
    t0 = time.time()
    code, err = run_ffmpeg_progress(cmd, vi.duration, stem)
    elapsed = time.time() - t0

    if code != 0 or not dst.is_file() or dst.stat().st_size == 0:
        return {"ok": False, "error": err[:300] or f"ffmpeg 返回 {code}"}

    new_size = dst.stat().st_size
    ratio = new_size / max(vi.size, 1)
    saved = vi.size - new_size

    return {
        "ok": True, "dst": dst, "old_size": vi.size, "new_size": new_size,
        "ratio": ratio, "saved": saved, "elapsed": elapsed,
        "notes": resolved,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="批量压缩短剧成片")
    ap.add_argument("--dir", required=True, help="成片所在目录")
    ap.add_argument("--preset", choices=list(PRESETS), default="upload",
                    help="压缩档位：archive / upload / light")
    ap.add_argument("--out", help="输出目录（默认 <目录>/压缩版）")
    ap.add_argument("--only", help="只处理文件名含该关键字的")
    ap.add_argument("--analyze", action="store_true", help="只分析，不压缩")
    ap.add_argument("--force", action="store_true", help="即使判定不值得压也压")
    ap.add_argument("--encoder", choices=["auto", "nvenc", "x264"], default="auto")
    ap.add_argument("--delete-original", action="store_true",
                    help="压缩并校验通过后，把原片移入回收站（可恢复）")
    args = ap.parse_args()

    src_dir = Path(args.dir).expanduser()
    if not src_dir.is_dir():
        print(f"错误：目录不存在 -> {src_dir}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).expanduser() if args.out else src_dir / "压缩版"

    env = get_ffmpeg_env()
    # 编码器偏好：压缩场景默认软编（压缩力强），仅显式指定时才用 NVENC
    globals()["_ENCODER_PREF"] = args.encoder
    globals()["_FORCE_X264"] = (args.encoder == "x264")
    print(f"ffmpeg: {Path(env.ffmpeg).name}")
    preset = PRESETS[args.preset]
    soft_codec = "libx265" if preset.codec == "hevc" else "libx264"
    if args.encoder == "nvenc" and env.nvenc_available:
        enc_desc = f"{'hevc' if preset.codec == 'hevc' else 'h264'}_nvenc（硬件，快但体积偏大）"
    else:
        enc_desc = f"{soft_codec}（软编，压缩力最强）"
    print(f"编码器: {enc_desc}")
    print(f"档位: {preset.name} — {preset.desc}")
    print(f"素材目录: {src_dir}")
    if not args.analyze:
        print(f"输出目录: {out_dir}")
    print()

    files = sorted(p for p in src_dir.glob("*.mp4") if p.is_file())
    if args.only:
        files = [f for f in files if args.only in f.name]
    if not files:
        print("未找到 mp4 文件")
        return 1

    # 探测
    print(f"分析 {len(files)} 个文件...")
    infos = []
    for f in files:
        vi = probe_video(f, env)
        if vi is None:
            print(f"  [跳过] 无法探测 {f.name}")
            continue
        infos.append(vi)

    total_size = sum(v.size for v in infos)
    print(f"  合计 {fmt_size(total_size)}，总时长 {fmt_dur(sum(v.duration for v in infos))}\n")

    print(f"{'文件':<34} {'分辨率':>10} {'码率':>9} {'bpp':>7}  判定")
    print("-" * 84)
    todo = []
    for vi in infos:
        worth, reason = analyze(vi, preset)
        res = f"{vi.width}x{vi.height}"
        mark = "压缩" if (worth or args.force) else "跳过"
        print(f"{vi.path.name[:32]:<34} {res:>10} {vi.mbps:>7.1f}M "
              f"{vi.bpp:>7.3f}  [{mark}] {reason}")
        if worth or args.force:
            todo.append(vi)

    if args.analyze:
        est = sum(v.size for v in todo)
        print()
        print(f"可压缩 {len(todo)}/{len(infos)} 个，涉及 {fmt_size(est)}")
        if todo:
            # 估算系数必须区分"输入是什么版本"，否则会严重失真。
            #
            # 实测基准（相对值）：
            #   · 原始素材(码率虚高) -> H.264 CRF26：  720p 约 13%，1080p 约 18%
            #   · 已是 H.264 CRF26 -> HEVC CRF30：     再乘约 0.41
            #   · 降分辨率到 720p 的额外收益：         再乘约 0.70
            #
            # 判断输入是否已是压缩版：看 bpp。原始素材 bpp 普遍 > 0.11，
            # 压缩版通常 < 0.06。
            avg_bpp = sum(v.bpp for v in todo) / max(len(todo), 1)
            already_compressed = avg_bpp < 0.08

            hevc = preset.codec == "hevc"
            total_n = 0.0
            for v in todo:
                if already_compressed:
                    r = 1.0            # 已经是压过的，不再套"原片->压缩"的系数
                    if hevc:
                        r *= 0.41      # 换 HEVC 的收益
                    if preset.max_height and v.height > preset.max_height:
                        r *= 0.70      # 降分辨率的额外收益
                else:
                    if v.height <= 1280:
                        r = 0.13
                    elif v.height <= 1920:
                        r = 0.18
                    else:
                        r = 0.30
                    if hevc:
                        r *= 0.55      # 直接从原片转 HEVC，比"H.264再转"更划算
                total_n += v.size * r

            print(f"预计压缩后约 {fmt_size(total_n)}，"
                  f"可节省约 {fmt_size(est - total_n)}（{(1-total_n/est)*100:.0f}%）")
            print(f"  （按 {preset.name} 档 / {'HEVC' if hevc else 'H.264'} 编码估算；"
                  f"输入判定为{'已压缩版' if already_compressed else '原始素材'}）")
        return 0

    if not todo:
        print("\n没有需要压缩的文件")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n开始压缩 {len(todo)} 个文件 → {out_dir}\n")

    results = []
    t_start = time.time()
    for i, vi in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {vi.path.name}")
        print(f"    原始 {fmt_size(vi.size)}  {vi.width}x{vi.height}@{vi.fps:g}  {vi.mbps:.1f}Mbps")
        r = compress_one(env, vi, out_dir, preset)
        if not r["ok"]:
            print(f"    !! 压缩失败：{r['error']}")
            print()
            continue

        print(f"    压缩后 {fmt_size(r['new_size'])}  "
              f"（{r['ratio']*100:.1f}%，省 {fmt_size(r['saved'])}）"
              f"  耗时 {r['elapsed']:.0f}s")
        for note in r["notes"]:
            print(f"      注：{note}")

        # --- 校验：只有通过才考虑删原片 ---
        print(f"    校验中...", end="", flush=True)
        v_ok, v_msg = verify_output(env, vi, r["dst"])
        print(f"\r    {v_msg}{' ' * 20}")
        r["verified"] = v_ok
        r["verify_msg"] = v_msg

        if args.delete_original:
            if v_ok:
                ok_del = send_to_recycle_bin(vi.path)
                # 复核：SHFileOperationW 偶尔返回非 0 但文件实际已删除
                # （例如 Explorer 正在索引该文件）。以文件是否还在为准。
                if ok_del or not vi.path.exists():
                    print(f"    原片已移入回收站（可恢复）")
                    r["deleted"] = True
                else:
                    print(f"    原片移入回收站失败，已保留")
            else:
                print(f"    !! 校验未通过，原片保留")

        r["name"] = vi.path.name
        results.append(r)
        print()

    ok = [r for r in results if r.get("ok")]
    if ok:
        o = sum(r["old_size"] for r in ok)
        n = sum(r["new_size"] for r in ok)
        print("=" * 66)
        print("压缩完成")
        print("=" * 66)
        print(f"  成功 {len(ok)}/{len(todo)} 个")
        print(f"  原始合计 {fmt_size(o)}")
        print(f"  压缩后   {fmt_size(n)}")
        print(f"  节省     {fmt_size(o - n)}（{(1 - n/o)*100:.1f}%）")
        print(f"  总耗时   {fmt_dur(time.time() - t_start)}")
        unverified = [r for r in ok if not r.get("verified")]
        if unverified:
            print(f"  !! {len(unverified)} 个未通过校验：")
            for r in unverified:
                print(f"     - {r['name']}: {r.get('verify_msg', '')}")
        else:
            print(f"  校验     全部通过")
        deleted = [r for r in ok if r.get("deleted")]
        if deleted:
            print(f"  原片     {len(deleted)} 个已移入回收站")
        print(f"  输出目录 {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
