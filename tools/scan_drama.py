#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量侦察短剧目录（合并前的决策依据）。

输出每部剧的：视频/音频参数一致性、集号连续性、内容重复文件、探测异常。
不改动任何文件，只读。

用法: python tools/scan_drama.py "D:/BaiduNetdiskDownload"
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env
from drama_merger.probe import collect_video_files, probe_file


def fmt_dur(sec: float) -> str:
    sec = max(sec, 0)
    h, m, s = int(sec // 3600), int((sec % 3600) // 60), sec % 60
    if h:
        return f"{h}小时{m}分{s:.0f}秒"
    if m:
        return f"{m}分{s:.0f}秒"
    return f"{s:.1f}秒"


def fmt_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024.0
    return ""


def head_hash(p: Path, n: int = 1 << 20) -> str:
    """读前 1MB 做指纹，用于快速判定重复（避免全量读大文件）。"""
    h = hashlib.md5()
    try:
        with open(p, "rb") as f:
            h.update(f.read(n))
    except Exception:  # noqa: BLE001
        return ""
    return h.hexdigest()


def ep_number(name: str) -> int | None:
    """从文件名提取集号，支持三种常见命名。"""
    for pat in (
        r"\((\d+)\)\s*\.\w+$",            # 1 (1).mp4
        r"第\s*(\d+)\s*集",                # XXX-第10集.mp4
        r"-\s*(\d+)(?:\(\d+\))?\s*\.\w+$",  # 56集58分钟-3.mp4
    ):
        m = re.search(pat, name)
        if m:
            return int(m.group(1))
    return None


def scan_dir(d: Path, env) -> None:
    files = collect_video_files(d)
    if not files:
        return

    print("=" * 68)
    print(f"【{d.name}】")
    print("=" * 68)

    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(lambda f: probe_file(f, env), files))

    ok = [i for i in infos if i.video is not None and not i.probe_error]
    bad = [i for i in infos if i.video is None or i.probe_error]

    total_dur = sum(i.duration for i in ok)
    total_size = sum(i.size for i in infos)
    print(f"  文件数 {len(files)}   成片时长约 {fmt_dur(total_dur)}   总大小 {fmt_size(total_size)}")

    if ok:
        res = Counter(f"{i.width}x{i.height}" for i in ok)
        fps = Counter(f"{i.fps:g}fps" for i in ok)
        codec = Counter(f"{i.video.codec_name}/{i.video.profile or '?'}" for i in ok)
        pix = Counter(i.video.pix_fmt for i in ok)
        print(f"  分辨率 {dict(res)}")
        print(f"  帧率   {dict(fps)}")
        print(f"  编码   {dict(codec)}   像素格式 {dict(pix)}")

        with_audio = [i for i in ok if i.has_audio]
        no_audio = [i for i in ok if not i.has_audio]
        line = f"  音轨   有 {len(with_audio)}/{len(ok)}"
        if no_audio:
            names = ", ".join(i.name for i in no_audio[:4])
            line += f"   无音轨 {len(no_audio)} 个: {names}"
        print(line)
        if with_audio:
            ac = Counter(
                f"{i.audios[0].codec_name} {i.audios[0].sample_rate}Hz {i.audios[0].channels}ch"
                for i in with_audio
            )
            print(f"  音频参数 {dict(ac)}")

    # 集号连续性
    nums = sorted(n for n in (ep_number(f.name) for f in files) if n is not None)
    if nums:
        dup = sorted(n for n, c in Counter(nums).items() if c > 1)
        missing = sorted(set(range(min(nums), max(nums) + 1)) - set(nums))
        line = f"  集号   {min(nums)}~{max(nums)}  实际 {len(nums)} 个"
        if dup:
            line += f"   重复集号 {dup}"
        if missing:
            line += f"   缺 {missing}"
        print(line)

    # 内容重复检测：先按 (大小, 时长) 分桶，再比对头部指纹
    buckets: dict = {}
    for i in infos:
        buckets.setdefault((i.size, round(i.duration, 2)), []).append(i)
    dups = []
    for items in buckets.values():
        if len(items) < 2:
            continue
        by_hash: dict = {}
        for it in items:
            by_hash.setdefault(head_hash(it.path), []).append(it)
        dups.extend(its for its in by_hash.values() if len(its) > 1)
    if dups:
        print("  !! 内容重复的文件（合并时需去重，否则同一集会播两遍）:")
        for its in dups:
            names = "  ==  ".join(f"{i.name}" for i in its)
            print(f"     - {names}   ({fmt_size(its[0].size)})")

    if bad:
        print("  !! 无法探测:")
        for b in bad:
            print(f"     - {b.name}: {b.probe_error or '无视频流'}")

    print()


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\BaiduNetdiskDownload")
    env = get_ffmpeg_env()
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    print(f"扫描根目录: {root}")
    print(f"子目录 {len(dirs)} 个\n")
    for d in dirs:
        scan_dir(d, env)


if __name__ == "__main__":
    main()
