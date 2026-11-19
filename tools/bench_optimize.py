#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
进一步压缩的可行性实测。

当前状况：11.74GB → 2.15GB，视频 bpp 已到 0.024~0.048（相当紧）。
再单纯降码率收益有限，且画质会明显恶化。

本脚本测三个"换维度"的方向：

  方向1  HEVC (H.265) 编码   —— 同画质下比 H.264 省 30~50%
  方向2  降分辨率到 720p      —— 势利岳母 1440x2560 / 两部 1080p 都有空间
  方向3  AV1 编码（若可用）   —— 理论效率最高，但生态兼容性差

输出每个方向的实际体积、耗时、SSIM，用于决策。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env, _run

SRC_DIR = Path(r"C:\Users\wangxingxiang\Videos\短剧成片\压缩版")
OUT = Path(__file__).resolve().parent.parent / "_opt"
SAMPLE = 120          # 每档取 120 秒

# 测试素材：覆盖三种分辨率
SAMPLES = [
    ("720p30", "灶边女王（44集）_压缩.mp4"),
    ("1080p30", "错待婆婆终悔悟（25集）_压缩.mp4"),
    ("1440p30", "势利岳母悔断肠（22集）_压缩.mp4"),
]


def fmt(mb: float) -> str:
    return f"{mb:.1f} MB"


def ssim(env, ref: Path, test: Path) -> float:
    """抽样 SSIM。两路先统一规格，否则滤镜报错。"""
    code, out, _ = _run([
        env.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json", str(ref),
    ], timeout=60)
    import json

    st = json.loads(out)["streams"][0]
    w, h = st["width"], st["height"]
    n, _, d = st["r_frame_rate"].partition("/")
    fps = f"{int(n)/int(d):.6g}" if int(d) else "30"

    fc = (
        f"[0:v]scale={w}:{h},fps={fps},setpts=PTS-STARTPTS[t];"
        f"[1:v]scale={w}:{h},fps={fps},setpts=PTS-STARTPTS[r];"
        f"[t][r]ssim"
    )
    code, out, err = _run([
        env.ffmpeg, "-hide_banner", "-loglevel", "info",
        "-i", str(test), "-i", str(ref), "-lavfi", fc, "-f", "null", "-",
    ], timeout=1200)
    for line in (out + err).splitlines():
        if "SSIM" in line and "All:" in line:
            try:
                return float(line.split("All:")[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
    return 0.0


def run_case(env, ref: Path, tag: str, name: str, args: list[str],
             vf: str | None = None) -> dict:
    dst = OUT / f"{tag}_{name}.mp4"
    cmd = [env.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(ref)]
    if vf:
        cmd += ["-vf", vf]
    cmd += args + ["-c:a", "copy", "-movflags", "+faststart", "-y", str(dst)]

    t0 = time.time()
    code, out, err = _run(cmd, timeout=2400)
    el = time.time() - t0

    if not dst.is_file() or dst.stat().st_size == 0:
        return {"ok": False, "err": (err or out).strip()[:150]}

    mb = dst.stat().st_size / 1048576
    s = ssim(env, ref, dst)
    return {"ok": True, "mb": mb, "sec": el, "ssim": s, "path": dst}


def main() -> None:
    env = get_ffmpeg_env()
    OUT.mkdir(exist_ok=True)

    caps = env.chosen
    print("=" * 78)
    print("可用编码器")
    print("=" * 78)
    for enc in ("libx264", "libx265", "hevc_nvenc", "libsvtav1", "libaom-av1", "av1_nvenc"):
        print(f"  {enc:<14} {'可用' if enc in caps.encoders else '不可用'}")
    print()

    for tag, fname in SAMPLES:
        src = SRC_DIR / fname
        if not src.is_file():
            print(f"[跳过] 找不到 {src}")
            continue

        print("=" * 78)
        print(f"【{tag}】{fname}")
        print("=" * 78)

        ref = OUT / f"{tag}_ref.mp4"
        _run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
              "-ss", "180", "-t", str(SAMPLE), "-i", str(src),
              "-c", "copy", "-y", str(ref)], timeout=600)
        if not ref.is_file():
            print("  取样失败")
            continue
        ref_mb = ref.stat().st_size / 1048576
        print(f"  当前样本 {fmt(ref_mb)}\n")

        print(f"  {'方案':<30} {'大小':>10} {'相对':>8} {'耗时':>7} {'SSIM':>7}")
        print("  " + "-" * 68)

        cases: list[tuple[str, list[str], str | None]] = [
            # (显示名, 编码参数, 滤镜)
            ("H.264 同参数（基线）",
             ["-c:v", "libx264", "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p"], None),
            ("H.265 同CRF",
             ["-c:v", "libx265", "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p"], None),
            ("H.265 CRF30",
             ["-c:v", "libx265", "-preset", "medium", "-crf", "30", "-pix_fmt", "yuv420p"], None),
            ("H.265 CRF32",
             ["-c:v", "libx265", "-preset", "medium", "-crf", "32", "-pix_fmt", "yuv420p"], None),
            ("H.265 CRF30 + 降到720p",
             ["-c:v", "libx265", "-preset", "medium", "-crf", "30", "-pix_fmt", "yuv420p"],
             "scale=720:1280,setsar=1"),
            ("H.264 CRF30 + 降到720p",
             ["-c:v", "libx264", "-preset", "medium", "-crf", "30", "-pix_fmt", "yuv420p"],
             "scale=720:1280,setsar=1"),
        ]

        # HEVC NVENC（若可用）
        if "hevc_nvenc" in caps.encoders:
            cases.append((
                "HEVC NVENC CQ32",
                ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr",
                 "-cq", "32", "-b:v", "0", "-pix_fmt", "yuv420p"], None))
        # AV1（若可用）
        if "av1_nvenc" in caps.encoders:
            cases.append((
                "AV1 NVENC CQ34",
                ["-c:v", "av1_nvenc", "-preset", "p4", "-rc", "vbr",
                 "-cq", "34", "-b:v", "0", "-pix_fmt", "yuv420p"], None))
        elif "libsvtav1" in caps.encoders:
            cases.append((
                "AV1 SVT CRF34",
                ["-c:v", "libsvtav1", "-preset", "6", "-crf", "34"], None))

        for name, args, vf in cases:
            r = run_case(env, ref, tag, name.replace(" ", "_").replace("+", ""), args, vf)
            if not r["ok"]:
                print(f"  {name:<30} {'失败':>10}   {r['err'][:36]}")
                continue
            ratio = r["mb"] / ref_mb
            print(f"  {name:<30} {fmt(r['mb']):>10} {ratio*100:>7.1f}% "
                  f"{r['sec']:>6.0f}s {r['ssim']:>7.4f}")
        print()


if __name__ == "__main__":
    main()
