#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量合并 D:/BaiduNetdiskDownload 下的短剧。

本批素材的实际情况（侦察结论）：
  1. 假面婚姻        29 个文件，但 -1 与 -1(1) 内容完全相同（md5 一致）→ 需去重
  2. 儿媳算计赶婆婆走  25 集，参数完全一致 → 无损拼接
  3. 寻回流失国宝     22 集，参数完全一致 → 无损拼接
  4. 灶边女王        44 集，命名为 "1 (N).mp4" → 自然排序已支持
  5. 造加特林，横扫朝堂敌  35 集，混装两个剧名（第1~30集 / 第31~35集），
                     且第1集是 4K/HEVC，与其余 34 集不同 → 最小重编码

用法: python tools/merge_baidu.py [--dry-run] [--only 剧名]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env
from drama_merger.merger import (
    Encoder, MergeOptions, Strategy, execute_merge, plan_merge, choose_strategy,
)
from drama_merger.probe import (
    analyze_consistency, collect_video_files, probe_file,
)

SRC_ROOT = Path(r"D:\BaiduNetdiskDownload")
OUT_ROOT = Path.home() / "Videos" / "短剧成片"


def ep_number(name: str) -> int | None:
    """提取集号，支持三种命名风格。"""
    for pat in (
        r"\((\d+)\)\s*\.\w+$",             # 灶边女王: 1 (12).mp4
        r"第\s*(\d+)\s*集",                 # 某剧-第10集.mp4
        r"-\s*(\d+)(?:\(\d+\))?\s*\.\w+$",  # 假面婚姻: 56集58分钟-3.mp4
    ):
        m = re.search(pat, name)
        if m:
            return int(m.group(1))
    return None


def dedupe_by_content(items, verbose: bool = True):
    """按 (大小) 分桶 + 头部指纹识别内容重复，每组只保留一个。

    本批素材中「假面婚姻」的 -1 与 -1(1) 内容完全相同，若不去重，
    第 1 集会连续播放两遍。
    """
    import hashlib

    def head_hash(p: Path, n: int = 1 << 20) -> str:
        h = hashlib.md5()
        try:
            with open(p, "rb") as f:
                h.update(f.read(n))
        except Exception:  # noqa: BLE001
            return p.name  # 读取失败则视为不重复
        return h.hexdigest()

    seen: dict = {}
    kept = []
    dropped = []
    for it in items:
        key = (it.size, head_hash(it.path))
        if key in seen:
            dropped.append((it, seen[key]))
            continue
        seen[key] = it
        kept.append(it)

    if dropped and verbose:
        print(f"    去重：丢弃 {len(dropped)} 个内容重复文件")
        for it, original in dropped:
            print(f"      - {it.name}  （与 {original.name} 内容相同）")
    return kept


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
            return f"{n:.2f} {u}"
        n /= 1024.0
    return ""


def merge_one(dir_name: str, out_name: str, env, dry_run: bool = False,
              encoder: str = "auto") -> dict:
    """合并一部剧。返回统计信息。"""
    d = SRC_ROOT / dir_name
    print()
    print("=" * 68)
    print(f"【{dir_name}】")
    print("=" * 68)

    files = collect_video_files(d)
    if not files:
        print("  未找到视频文件，跳过")
        return {"ok": False, "reason": "无视频文件"}

    print(f"  发现 {len(files)} 个视频文件，探测中...")
    t0 = time.time()
    items = [probe_file(f, env) for f in files]

    bad = [i for i in items if i.probe_error or not i.has_video]
    if bad:
        print(f"  警告：{len(bad)} 个文件无法探测，将跳过")
        for b in bad:
            print(f"    - {b.name}: {b.probe_error or '无视频流'}")
        items = [i for i in items if not i.probe_error and i.has_video]

    # 去重
    before = len(items)
    items = dedupe_by_content(items)
    if len(items) != before:
        print(f"    过滤后剩 {len(items)} 个片段")

    if not items:
        print("  无可合并片段，跳过")
        return {"ok": False, "reason": "无可合并片段"}

    # 按集号重排，确保顺序正确
    numbered = [(ep_number(i.name), i) for i in items]
    if all(n is not None for n, _ in numbered):
        numbered.sort(key=lambda x: x[0])
        items = [i for _, i in numbered]
        nums = [n for n, _ in numbered]
        missing = sorted(set(range(min(nums), max(nums) + 1)) - set(nums))
        print(f"  集号顺序：{min(nums)} ~ {max(nums)}，共 {len(nums)} 集"
              + (f"（缺 {missing}）" if missing else "（连续）"))

    real_dur = sum(i.duration for i in items)
    real_size = sum(i.size for i in items)
    print(f"  成片时长约 {fmt_dur(real_dur)}，素材 {fmt_size(real_size)}")

    report = analyze_consistency(items)
    strategy = choose_strategy(report, MergeOptions(output=OUT_ROOT / "x.mp4"))

    # 策略说明
    if strategy == Strategy.LOSSLESS_CONCAT:
        print("  策略：A 无损拼接（参数完全一致，零损失）")
    elif strategy == Strategy.MINIMAL_REENCODE:
        dev, maj = report.minimal_reencode_plan()
        print(f"  策略：D 最小重编码（{len(maj)} 集原样保留，"
              f"{len(dev)} 集需重编码）")
        for x in dev:
            print(f"      · {x.name}  {x.width}x{x.height}@{x.fps:g}fps "
                  f"{x.video.codec_name}")
    else:
        print(f"  策略：{strategy.label}")

    output = OUT_ROOT / out_name
    options = MergeOptions(output=output, encoder=Encoder(encoder))

    if dry_run:
        print(f"  [试运行] 将输出到：{output}")
        return {"ok": True, "dry": True, "strategy": strategy.value,
                "duration": real_dur, "count": len(items)}

    print(f"  输出：{output}")
    print(f"  开始合并...")
    try:
        res = execute_merge(items, report, options, env)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 合并失败：{str(exc)[:400]}")
        return {"ok": False, "reason": str(exc)[:200]}

    speed = res.duration / max(res.elapsed, 0.001)
    print(f"  完成：{fmt_dur(res.duration)}  {fmt_size(res.size)}  "
          f"耗时 {res.elapsed:.1f}s（{speed:.1f}x 实时）")
    print(f"  实际使用：{res.strategy.label}")
    print(f"  编码器：{res.encoder_used}")
    for w in res.warnings:
        print(f"  注意：{w}")

    return {"ok": True, "strategy": res.strategy.value,
            "duration": res.duration, "size": res.size,
            "elapsed": res.elapsed, "count": len(items),
            "output": str(output)}


# 本批 5 部剧的配置：(目录名, 输出文件名, 备注)
JOBS = [
    ("假面婚姻", "假面婚姻（28集）.mp4", "含1个重复文件需去重"),
    ("儿媳算计赶婆婆走，不料成全婆婆富贵晚年", "错待婆婆终悔悟（25集）.mp4", "参数一致"),
    ("寻回流失国宝", "势利岳母悔断肠（22集）.mp4", "参数一致"),
    ("灶边女王", "灶边女王（44集）.mp4", "命名格式 1 (N)"),
    ("造加特林，横扫朝堂敌", "造加特林，横扫朝堂敌（35集）.mp4",
     "混装两个剧名 + 第1集为4K/HEVC"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="批量合并百度网盘短剧")
    ap.add_argument("--dry-run", action="store_true", help="只分析不合并")
    ap.add_argument("--only", help="只处理名称包含该关键字的剧")
    ap.add_argument("--encoder", default="auto", choices=["auto", "nvenc", "x264"])
    args = ap.parse_args()

    env = get_ffmpeg_env()
    print(f"ffmpeg: {Path(env.ffmpeg).name}")
    print(f"硬件编码: {'可用（NVENC）' if env.nvenc_available else '不可用，使用 CPU'}")
    print(f"素材目录: {SRC_ROOT}")
    print(f"输出目录: {OUT_ROOT}")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    jobs = JOBS
    if args.only:
        jobs = [j for j in JOBS if args.only in j[0]]
        if not jobs:
            print(f"没有匹配 '{args.only}' 的剧")
            return 1

    results = []
    t_start = time.time()
    for dir_name, out_name, note in jobs:
        print(f"\n>>> {note}")
        r = merge_one(dir_name, out_name, env, dry_run=args.dry_run,
                      encoder=args.encoder)
        r["name"] = dir_name
        results.append(r)

    # 汇总
    print()
    print("=" * 68)
    print("汇总")
    print("=" * 68)
    total_dur = total_size = total_elapsed = 0.0
    for r in results:
        if r.get("ok"):
            status = "试运行" if r.get("dry") else "完成"
            line = f"  [{status}] {r['name']}"
            if r.get("duration"):
                line += f"  时长 {fmt_dur(r['duration'])}"
                total_dur += r["duration"]
            if r.get("size"):
                line += f"  {fmt_size(r['size'])}"
                total_size += r["size"]
            if r.get("elapsed"):
                line += f"  耗时 {r['elapsed']:.1f}s"
                total_elapsed += r["elapsed"]
            print(line)
        else:
            print(f"  [失败] {r['name']}  <- {r.get('reason', '未知')}")

    if not args.dry_run:
        print()
        print(f"  合计：{len([r for r in results if r.get('ok')])} 部成片，"
              f"总时长 {fmt_dur(total_dur)}，总大小 {fmt_size(total_size)}，"
              f"总耗时 {total_elapsed:.1f}s（含编码约 {fmt_dur(time.time() - t_start)}）")
        print(f"  输出目录：{OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
