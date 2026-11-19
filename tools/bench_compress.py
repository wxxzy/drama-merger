#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
压缩方案实测对比。

用真实成片素材，测量不同编码参数的：
  压缩率 / 实际耗时 / 主观质量参考(SSIM)

目的是给出有数据支撑的方案建议，而不是凭经验猜。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env, _run

OUT_DIR = Path(r"C:\Users\wangxingxiang\Videos\短剧成片")
TEST_DIR = Path(__file__).resolve().parent.parent / "_cmp"
SAMPLE_SEC = 120          # 每档取 120 秒样本，够稳定又不耗时

# 取样素材：分别代表低码率组和 60fps 高码率组
SAMPLES = {
    "720p30_8Mbps": OUT_DIR / "灶边女王（44集）.mp4",
    "1080p60_14Mbps": OUT_DIR / "造加特林，横扫朝堂敌（35集）.mp4",
}


def run_ff(cmd: list[str], timeout: int = 1800):
    t0 = time.time()
    code, out, err = _run(cmd, timeout=timeout)
    return code, err.strip(), time.time() - t0


def ssim(env, ref: Path, test: Path) -> float:
    """用 ffmpeg 的 ssim 滤镜算结构相似度（越接近 1 越好）。

    两个坑：
    1. 不能用 stats_file=-，它会把每帧统计写到 stdout，污染结果解析
    2. 两路输入分辨率/帧率必须一致，否则 ssim 直接报错。
       故先各自 scale+fps 到统一规格（取参照的分辨率与帧率）。
    注意 ffmpeg 的 ssim 汇总行格式为：
        [Parsed_ssim_0 @ 0x...] SSIM Y:0.98 U:0.99 V:0.99 All:0.985 (14.5)
    它并不以 "SSIM" 开头，必须用包含判断而非 startswith。
    """
    # 取参照的分辨率与帧率
    code, out, _ = _run([
        env.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0", str(ref),
    ], timeout=60) if env.ffprobe else (1, "", "")
    w = h = 0
    fps = ""
    if code == 0 and out.strip():
        parts = out.strip().splitlines()[0].split(",")
        try:
            w, h = int(parts[0]), int(parts[1])
            num, den = parts[2].split("/")
            fps = f"{int(num)/int(den):.6g}"
        except (IndexError, ValueError):
            w = h = 0

    if not w:
        return 0.0

    fc = (
        f"[0:v]scale={w}:{h},fps={fps},setpts=PTS-STARTPTS[t];"
        f"[1:v]scale={w}:{h},fps={fps},setpts=PTS-STARTPTS[r];"
        f"[t][r]ssim"
    )
    cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "info",
           "-i", str(test), "-i", str(ref),
           "-lavfi", fc, "-f", "null", "-"]
    code, out, err = _run(cmd, timeout=1800)
    text = out + err

    val = 0.0
    for line in text.splitlines():
        if "SSIM" in line and "All:" in line:
            try:
                val = float(line.split("All:")[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
    return val


def measure(env, src: Path, dst: Path) -> tuple[float, float, str]:
    """返回 (大小MB, 耗时秒, 错误信息)。"""
    size_mb = dst.stat().st_size / 1048576 if dst.is_file() else 0.0
    return size_mb, 0.0, ""


def main() -> None:
    env = get_ffmpeg_env()
    TEST_DIR.mkdir(exist_ok=True)

    print(f"ffmpeg: {Path(env.ffmpeg).name}")
    print(f"NVENC: {'可用' if env.nvenc_available else '不可用'}")
    print(f"每档取样 {SAMPLE_SEC} 秒\n")

    for tag, src in SAMPLES.items():
        if not src.is_file():
            print(f"[跳过] 找不到 {src}")
            continue

        print("=" * 74)
        print(f"【{tag}】{src.name}")
        print("=" * 74)

        # 原始参照
        ref = TEST_DIR / f"{tag}_ref.mp4"
        cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", "60", "-t", str(SAMPLE_SEC), "-i", str(src),
               "-c", "copy", "-y", str(ref)]
        code, err, _ = run_ff(cmd)
        if not ref.is_file():
            print(f"  取样失败: {err}")
            continue
        ref_mb = ref.stat().st_size / 1048576
        print(f"  原始样本: {ref_mb:.1f} MB  (等同 {ref_mb*3600/SAMPLE_SEC/1024:.2f} GB/小时)\n")

        # 各档方案
        plans = [
            # (名称, 编码器参数, 缩放/帧率参数)
            ("x264 CRF28 原分辨率原帧率", "x264_28", {}),
            ("x264 CRF30 原分辨率原帧率", "x264_30", {}),
            ("x264 CRF28 降到30fps", "x264_28", {"fps": 30}),
            ("NVENC CQ30 原分辨率原帧率", "nvenc_30", {}),
            ("NVENC CQ34 降到30fps", "nvenc_34", {"fps": 30}),
            ("NVENC CQ32 降到720p30", "nvenc_32", {"w": 720, "h": 1280, "fps": 30}),
        ]

        print(f"  {'方案':<32} {'大小MB':>8} {'压缩比':>8} {'耗时s':>7} {'SSIM':>7}")
        print("  " + "-" * 68)

        for name, mode, opt in plans:
            dst = TEST_DIR / f"{tag}_{mode}_{opt.get('w','o')}{opt.get('fps','o')}.mp4"
            vf = []
            if opt.get("w"):
                w, h = opt["w"], opt["h"]
                # 统一按 9:16 竖屏缩放（保持宽高比 + 补边）
                vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
                vf.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
                vf.append("setsar=1")
            if opt.get("fps"):
                vf.append(f"fps={opt['fps']}")

            cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(ref)]
            if vf:
                cmd += ["-vf", ",".join(vf)]

            if mode.startswith("x264"):
                crf = mode.split("_")[1]
                cmd += ["-c:v", "libx264", "-preset", "medium",
                        "-crf", crf, "-pix_fmt", "yuv420p"]
            else:
                cq = mode.split("_")[1]
                cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                        "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"]

            cmd += ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                    "-y", str(dst)]

            code, err, elapsed = run_ff(cmd)
            if not dst.is_file() or dst.stat().st_size == 0:
                print(f"  {name:<32} {'失败':>8}    {err[:40]}")
                continue

            mb = dst.stat().st_size / 1048576
            ratio = ref_mb / mb if mb else 0
            s = ssim(env, ref, dst)
            print(f"  {name:<32} {mb:>8.1f} {ratio:>7.1f}x {elapsed:>7.1f} {s:>7.4f}")

        print()


if __name__ == "__main__":
    main()
