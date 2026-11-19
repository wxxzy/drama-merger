#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
端到端回归测试。

覆盖：
  1. 环境探测与择优
  2. 自然排序（ep2 < ep10）
  3. 一致性分析（一致 / 不一致判定）
  4. 策略A 无损拼接：时长精确、起点归零、解码无错
  5. 策略B 归一化拼接：异构分辨率/帧率/无音轨统一
  6. 字幕解析与时间轴重映射
  7. 裁剪、淡变
  8. 边界容错：损坏文件、非视频文件、单文件
  9. 探测模块对无 ffprobe 场景的退化路径（语法层）

运行：python tests/run_tests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drama_merger.ffmpeg_env import get_ffmpeg_env, _run  # noqa: E402
from drama_merger.merger import (  # noqa: E402
    Encoder, MergeOptions, Strategy, choose_strategy, execute_merge,
)
from drama_merger.probe import (  # noqa: E402
    analyze_consistency, collect_video_files, probe_file,
)
from drama_merger.subtitles import (  # noqa: E402
    EpisodeSubtitle, find_subtitles_for, merge_subtitles, parse_srt, to_srt,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))


def probe_streams(env, path: Path) -> dict:
    """返回 {codec_type: start_time} 与 format 信息。"""
    ffprobe = env.ffprobe
    if not ffprobe:
        return {}
    code, out, _ = _run([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration,start_time",
        "-show_entries", "stream=codec_type,start_time",
        "-of", "json", str(path),
    ], timeout=60)
    if code != 0:
        return {}
    import json
    return json.loads(out)


def decode_ok(env, path: Path) -> bool:
    """完整解码，确认无错误。"""
    code, out, err = _run([env.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                          timeout=600)
    return code == 0 and not err.strip()


def main() -> int:
    out_dir = ROOT / "tests" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print("1. 环境探测")
    print("=" * 64)
    env = get_ffmpeg_env(refresh=True)
    check("ffmpeg 可执行", Path(env.ffmpeg).is_file())
    check("候选 ffmpeg >= 1 个", len(env.candidates) >= 1,
          f"实际 {len(env.candidates)}")
    print(f"    选用: {Path(env.ffmpeg).name}")
    print(f"    NVENC: {'可用' if env.nvenc_available else '不可用'}")

    print()
    print("=" * 64)
    print("2. 自然排序")
    print("=" * 64)
    d3 = ROOT / "tests" / "scene3_cn_names"
    if d3.is_dir():
        got = [p.name for p in collect_video_files(d3)]
        expect = ["《霸总的小娇妻》第1集.mp4", "《霸总的小娇妻》第2集.mp4",
                  "《霸总的小娇妻》第10集.mp4", "《霸总的小娇妻》第11集.mp4"]
        check("中文名+两位数集数排序", got == expect, f"{got}")
    else:
        check("场景3 素材存在", False, "请先运行 make_fixtures.py")

    print()
    print("=" * 64)
    print("3. 一致性分析")
    print("=" * 64)
    d1 = ROOT / "tests" / "scene1_uniform" / "都市甜宠短剧"
    d2 = ROOT / "tests" / "scene2_mixed"
    if d1.is_dir() and d2.is_dir():
        items1 = [probe_file(f, env) for f in collect_video_files(d1)]
        items2 = [probe_file(f, env) for f in collect_video_files(d2)]
        r1 = analyze_consistency(items1)
        r2 = analyze_consistency(items2)
        check("一致素材判定为无差异", r1.is_uniform,
              f"发现 {len(r1.inconsistencies)} 处差异")
        check("异构素材判定出差异", len(r2.inconsistencies) > 0)
        check("检测到分辨率不一致",
              any("分辨率" in i.field for i in r2.inconsistencies))
        check("检测到音轨有无不一致",
              any("音轨" in i.field for i in r2.inconsistencies))
        check("一致素材选用策略A",
              choose_strategy(r1, MergeOptions(output=out_dir / "x.mp4"))
              == Strategy.LOSSLESS_CONCAT)
        check("异构素材选用策略B",
              choose_strategy(r2, MergeOptions(output=out_dir / "x.mp4"))
              == Strategy.NORMALIZE_CONCAT)
    else:
        check("场景1/2 素材存在", False, "请先运行 make_fixtures.py")

    print()
    print("=" * 64)
    print("4. 策略A 无损拼接")
    print("=" * 64)
    if d1.is_dir():
        items = [probe_file(f, env) for f in collect_video_files(d1)]
        report = analyze_consistency(items)
        out = out_dir / "t_lossless.mp4"
        opt = MergeOptions(output=out, encoder=Encoder.AUTO)
        res = execute_merge(items, report, opt, env)
        check("采用策略A", res.strategy == Strategy.LOSSLESS_CONCAT)
        check("输出文件存在", out.is_file())
        info = probe_streams(env, out)
        dur = float(info.get("format", {}).get("duration", 0))
        check("时长精确 (18.0s)", abs(dur - 18.0) < 0.05, f"实际 {dur}")
        check("起始时间归零", float(info.get("format", {}).get("start_time", 1)) < 0.001)
        check("解码无错误", decode_ok(env, out))
        expect_size = sum(i.size for i in items)
        check("文件大小与素材总和接近（无损）",
              abs(res.size - expect_size) / max(expect_size, 1) < 0.02,
              f"输出 {res.size} vs 素材 {expect_size}")
        check("编码器报告为'未重编码'", "未重新编码" in res.encoder_used)

    print()
    print("=" * 64)
    print("5. 策略B 归一化拼接（含无音轨片段）")
    print("=" * 64)
    if d2.is_dir():
        items = [probe_file(f, env) for f in collect_video_files(d2)]
        report = analyze_consistency(items)
        out = out_dir / "t_normalize.mp4"
        opt = MergeOptions(output=out)
        res = execute_merge(items, report, opt, env)
        check("采用策略B", res.strategy == Strategy.NORMALIZE_CONCAT)
        info = probe_streams(env, out)
        streams = info.get("streams", [])
        types = {s.get("codec_type") for s in streams}
        check("输出含视频流", "video" in types)
        check("输出含音频流（无音轨片段已补静音）", "audio" in types)
        for s in streams:
            if s.get("codec_type") in ("video", "audio"):
                check(f"{s['codec_type']} 起始时间归零",
                      float(s.get("start_time", 1) or 1) < 0.001)
        dur = float(info.get("format", {}).get("duration", 0))
        check("时长约 15.0s", abs(dur - 15.0) < 0.1, f"实际 {dur}")
        check("解码无错误", decode_ok(env, out))

    print()
    print("=" * 64)
    print("5b. 策略D 最小重编码（多数一致 + 少数偏离）")
    print("=" * 64)
    d5 = ROOT / "tests" / "scene5_minimal"
    if d5.is_dir():
        items = [probe_file(f, env) for f in collect_video_files(d5)]
        report = analyze_consistency(items)
        dev, maj = report.minimal_reencode_plan()
        check("识别出偏离片段数为 1", len(dev) == 1,
              f"实际 {len(dev)}")
        check("多数派片段数为 6", len(maj) == 6, f"实际 {len(maj)}")
        check("选用策略D",
              choose_strategy(report, MergeOptions(output=out_dir / "x.mp4"))
              == Strategy.MINIMAL_REENCODE)
        # 目标参数必须取自多数派（1080x1920@30），而非被 720x1280@25 带偏
        tv = report.target_video_params()
        check("目标分辨率取自多数派 (1080x1920)",
              tv["width"] == 1080 and tv["height"] == 1920, f"实际 {tv}")
        check("目标帧率取自多数派 (30fps)", abs(tv["fps"] - 30.0) < 0.01,
              f"实际 {tv['fps']}")

        out = out_dir / "t_minimal.mp4"
        res = execute_merge(items, report, MergeOptions(output=out), env)
        check("策略D 执行成功", out.is_file() and res.size > 0)
        check("策略D 报告为'仅用于偏离片段'", "仅用于偏离片段" in res.encoder_used)
        info = probe_streams(env, out)
        dur = float(info.get("format", {}).get("duration", 0))
        check("时长约 21s (7x3s)", abs(dur - 21.0) < 0.15, f"实际 {dur}")
        check("起始时间归零",
              float(info.get("format", {}).get("start_time", 1)) < 0.001)
        streams = info.get("streams", [])
        check("输出含音频流", any(s.get("codec_type") == "audio" for s in streams))
        check("策略D 输出解码无错误", decode_ok(env, out))
    else:
        check("场景5 素材存在", False, "请先运行 make_fixtures.py")

    print()
    print("=" * 64)
    print("6. 字幕解析与时间轴重映射")
    print("=" * 64)
    srt_text = ("1\n00:00:00,500 --> 00:00:02,500\n第一句\n\n"
                "2\n00:01:23,456 --> 00:01:25,000\n第二句\n")
    cues = parse_srt(srt_text)
    check("SRT 解析条数", len(cues) == 2, f"实际 {len(cues)}")
    if cues:
        check("毫秒解析正确 (0.5s)", abs(cues[0].start - 0.5) < 0.001,
              f"实际 {cues[0].start}")
        check("分钟进位正确 (83.456s)", abs(cues[1].start - 83.456) < 0.001,
              f"实际 {cues[1].start}")
        shifted = cues[0].shifted(6.0)
        check("平移正确 (0.5 + 6.0)", abs(shifted.start - 6.5) < 0.001)

    if d1.is_dir():
        eps = []
        offset = 0.0
        for f in collect_video_files(d1):
            subs = find_subtitles_for(f)
            if subs:
                eps.append(EpisodeSubtitle(f, subs, offset=offset))
            offset += 6.0
        srt_out = out_dir / "t_merged.srt"
        path, info_text = merge_subtitles(eps, srt_out)
        check("字幕合并产出文件", path is not None and path.is_file())
        if path:
            content = path.read_text(encoding="utf-8")
            merged_cues = parse_srt(content)
            check("字幕总条数正确 (3集x3条=9)", len(merged_cues) == 9,
                  f"实际 {len(merged_cues)}")
            # 第2集首句应从 6.0s 附近开始
            has_ep2 = any(6.0 <= c.start <= 7.5 and "第2集" in c.text
                          for c in merged_cues)
            check("第2集字幕已平移至 6s 后", has_ep2)

    print()
    print("=" * 64)
    print("7. 裁剪与淡变")
    print("=" * 64)
    if d1.is_dir():
        items = [probe_file(f, env) for f in collect_video_files(d1)]
        report = analyze_consistency(items)

        out = out_dir / "t_trim.mp4"
        opt = MergeOptions(output=out, trim_head=1.0, trim_tail=1.0)
        res = execute_merge(items, report, opt, env)
        info = probe_streams(env, out)
        dur = float(info.get("format", {}).get("duration", 0))
        check("裁剪后时长约 12s (3x4s)", abs(dur - 12.0) < 0.2, f"实际 {dur}")
        check("裁剪输出解码无错误", decode_ok(env, out))

        out2 = out_dir / "t_fade.mp4"
        opt2 = MergeOptions(output=out2, fade_in=1.0, fade_out=1.0)
        res2 = execute_merge(items, report, opt2, env)
        info2 = probe_streams(env, out2)
        dur2 = float(info2.get("format", {}).get("duration", 0))
        check("淡变后时长约 18s", abs(dur2 - 18.0) < 0.2, f"实际 {dur2}")
        check("淡变输出解码无错误", decode_ok(env, out2))

    print()
    print("=" * 64)
    print("8. 边界容错")
    print("=" * 64)
    d4 = ROOT / "tests" / "scene4_edge"
    if d4.is_dir():
        files = collect_video_files(d4)
        names = [f.name for f in files]
        check("非视频文件被忽略", "readme.txt" not in names)
        # 损坏文件应被识别为探测失败
        bad = [probe_file(f, env) for f in files if f.name == "corrupted.mp4"]
        check("损坏文件标记为探测失败",
              bool(bad) and bool(bad[0].probe_error),
              f"probe_error={bad[0].probe_error if bad else 'N/A'}")
        good = [probe_file(f, env) for f in files if f.name == "single.mp4"]
        check("单文件可正常探测", bool(good) and good[0].has_video)

        # 单文件合并
        items = [i for i in (probe_file(f, env) for f in files)
                 if not i.probe_error and i.has_video]
        report = analyze_consistency(items)
        out = out_dir / "t_single.mp4"
        res = execute_merge(items, report, MergeOptions(output=out), env)
        check("单片段合并成功", out.is_file() and res.size > 0)

    print()
    print("=" * 64)
    print(f"测试完成：通过 {PASS}，失败 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
