#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
短剧视频合并工具 —— 命令行入口

用法示例：
  # 看看目录里有什么，以及能不能无损拼接
  python cli.py probe "D:/短剧/某剧"

  # 合并（自动选择最优策略）
  python cli.py merge "D:/短剧/某剧" -o "D:/输出/某剧全集.mp4"

  # 合并 + 烧入字幕
  python cli.py merge "D:/短剧/某剧" -o out.mp4 --burn-subtitles

  # 只合并，强制硬件加速
  python cli.py merge "D:/短剧/某剧" -o out.mp4 --encoder nvenc

  # 启动图形界面
  python cli.py gui
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接运行脚本（不经包安装）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env
from drama_merger.compress import PRESETS, describe_all, get_preset
from drama_merger.merger import (
    Encoder,
    MergeOptions,
    Strategy,
    execute_merge,
    plan_merge,
)
from drama_merger.probe import (
    analyze_consistency,
    collect_video_files,
    probe_file,
)
from drama_merger.subtitles import (
    EpisodeSubtitle,
    burn_subtitles,
    find_subtitles_for,
    merge_subtitles,
)


def _fmt_duration(sec: float) -> str:
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h:
        return f"{h}小时{m}分{s:.1f}秒"
    if m:
        return f"{m}分{s:.1f}秒"
    return f"{s:.1f}秒"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} TB"


class ConsoleProgress:
    """控制台进度条。

    分两种模式：
    - TTY（真实终端）：用 \\r 原地重绘，且仅在百分比变化时刷新，避免高频刷屏。
    - 非 TTY（输出被重定向/被工具捕获）：\\r 不会生效，逐帧打印会拼成一条
      巨长记录。此时改为每跨过 10% 打印一行，日志保持可读。
    """

    def __init__(self, stream=None) -> None:
        import sys as _sys

        self.stream = stream or _sys.stdout
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.last = ""
        self.phase = ""
        self._last_pct = -1          # 上次刷新的整数百分比
        self._milestone = -1         # 非 TTY 下已打印到的 10% 档位

    def __call__(self, phase: str, ratio: float) -> None:
        ratio = max(0.0, min(ratio, 1.0))
        pct = int(ratio * 100)

        if phase != self.phase:
            if self.last:
                print(file=self.stream)
            self.phase = phase
            self.last = ""
            self._last_pct = -1
            self._milestone = -1

        if self.is_tty:
            # 原地重绘：同一百分比不重复画，减少无意义刷新
            if pct == self._last_pct:
                return
            self._last_pct = pct
            width = 34
            filled = int(width * ratio)
            bar = "#" * filled + "-" * (width - filled)
            line = f"\r  [{bar}] {ratio * 100:5.1f}%  {phase[:44]:<44}"
            print(line, end="", flush=True, file=self.stream)
            self.last = line
        else:
            # 非 TTY：每跨 10% 打一行，末尾（>=99%）也补一行
            step = pct // 10
            if step > self._milestone or (pct >= 99 and self._milestone < 10):
                self._milestone = step
                print(f"  [{pct:3d}%] {phase}", flush=True, file=self.stream)
                self.last = "x"

    def done(self) -> None:
        if self.last and self.is_tty:
            print(file=self.stream)
        self.last = ""


# ---------------------------------------------------------------------------
# probe 子命令
# ---------------------------------------------------------------------------

def cmd_probe(args: argparse.Namespace) -> int:
    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        print(f"错误：目录不存在 -> {directory}", file=sys.stderr)
        return 2

    env = get_ffmpeg_env(refresh=args.refresh_env, verbose=True)
    print(env.report())
    print()

    files = collect_video_files(directory, recursive=args.recursive)
    if not files:
        print(f"目录中未发现视频文件：{directory}")
        return 1

    print(f"发现 {len(files)} 个视频文件，开始探测...\n")
    items = []
    t0 = time.time()
    for i, f in enumerate(files, 1):
        info = probe_file(f, env)
        items.append(info)
        print(f"  [{i:>3}/{len(files)}] {info.summary()}")
    print(f"\n探测耗时 {time.time() - t0:.1f}s")

    report = analyze_consistency(items)
    print("\n" + "=" * 62)
    print("一致性分析")
    print("=" * 62)
    if report.is_uniform:
        print("所有片段参数完全一致 -> 可走【策略A 无损拼接】（秒级完成，画质零损失）")
    else:
        print(f"发现 {len(report.inconsistencies)} 处差异：")
        for inc in report.inconsistencies:
            tag = "必须处理" if inc.severity == "blocker" else "建议处理"
            print(f"  [{'阻断' if inc.severity == 'blocker' else '提示'}] {inc.describe()}  ({tag})")

        dev, maj = report.minimal_reencode_plan()
        if dev and maj:
            pct = len(maj) * 100.0 / max(len(items), 1)
            print(f"\n其中 {len(maj)} 个片段（{pct:.0f}%）参数一致，"
                  f"仅 {len(dev)} 个片段偏离 -> 可走【策略D 最小重编码】")
            print(f"  只需重编码 {len(dev)} 个偏离片段，其余 {len(maj)} 个原样复制（零损失）")
            print(f"  偏离片段：")
            for d in dev[:10]:
                vt = f"{d.width}x{d.height}@{d.fps:g}fps"
                print(f"    - {d.name}  ({vt})")
            if len(dev) > 10:
                print(f"    ... 另有 {len(dev) - 10} 个")
        elif report.can_concat_lossless:
            print("\n无阻断性问题 -> 可走【策略B 归一化拼接】")
        else:
            print("\n存在阻断性问题 -> 需走【策略C 滤镜重编码】")

    tv = report.target_video_params()
    ta = report.target_audio_params()
    print(f"\n目标输出参数：{tv['width']}x{tv['height']} @ {tv['fps']:g}fps，"
          f"音频 {ta['sample_rate']}Hz {ta['channels']}ch")
    total = sum(i.duration for i in items)
    print(f"合并后总时长：约 {_fmt_duration(total)}")
    total_size = sum(i.size for i in items)
    print(f"素材总大小：{_fmt_size(total_size)}")

    # 顺带报告字幕情况
    sub_count = 0
    for info in items:
        subs = find_subtitles_for(info.path)
        sub_count += len(subs)
        if subs:
            print(f"  [字幕] {info.name} -> {', '.join(s.name for s in subs)}")
    if sub_count:
        print(f"\n共发现 {sub_count} 个配套字幕文件（合并时会自动重映射时间轴）")
    else:
        print("\n未发现配套字幕文件")
    return 0


# ---------------------------------------------------------------------------
# merge 子命令
# ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace) -> int:
    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        print(f"错误：目录不存在 -> {directory}", file=sys.stderr)
        return 2

    env = get_ffmpeg_env(refresh=args.refresh_env, verbose=args.verbose)
    if args.verbose:
        print(env.report())

    files = collect_video_files(directory, recursive=args.recursive)
    if not files:
        print(f"目录中未发现视频文件：{directory}", file=sys.stderr)
        return 1

    if args.limit:
        files = files[: args.limit]

    print(f"发现 {len(files)} 个视频文件，开始探测...")
    items = []
    for i, f in enumerate(files, 1):
        info = probe_file(f, env)
        if info.probe_error:
            print(f"  [{i}/{len(files)}] {info.name} 探测失败：{info.probe_error}")
        items.append(info)

    bad = [i for i in items if i.probe_error or not i.has_video]
    if bad:
        print(f"\n警告：{len(bad)} 个文件无有效视频流，将跳过：")
        for b in bad:
            print(f"  - {b.name}: {b.probe_error or '无视频流'}")
        items = [i for i in items if not i.probe_error and i.has_video]
        if not items:
            print("错误：没有可用的视频片段", file=sys.stderr)
            return 1

    report = analyze_consistency(items)
    print()
    print("=" * 62)
    for inc in report.inconsistencies:
        tag = "阻断" if inc.severity == "blocker" else "提示"
        print(f"  [{tag}] {inc.describe()}")
    if not report.inconsistencies:
        print("  片段参数完全一致")
    print("=" * 62)

    output = Path(args.output).expanduser() if args.output else (
        directory.parent / f"{directory.name}_merged.mp4"
    )

    strategy_override = None
    if args.strategy:
        strategy_override = {
            "a": Strategy.LOSSLESS_CONCAT,
            "b": Strategy.NORMALIZE_CONCAT,
            "c": Strategy.FILTER_CONCAT,
        }[args.strategy.lower()]

    # 合并即压缩档位
    compress_preset = None
    if args.compress:
        try:
            compress_preset = get_preset(args.compress)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

    # 压缩档位会改变输出文件名后缀，避免与"未压缩成片"混淆
    if compress_preset is not None and not args.output:
        output = output.with_name(f"{output.stem}_{compress_preset.short}{output.suffix}")

    progress = ConsoleProgress()
    options = MergeOptions(
        output=output,
        encoder=Encoder(args.encoder),
        crf=args.crf,
        preset=args.preset,
        trim_head=args.trim_head,
        trim_tail=args.trim_tail,
        fade_in=args.fade_in,
        fade_out=args.fade_out,
        force_strategy=strategy_override,
        keep_temp=args.keep_temp,
        on_progress=progress,
        compress=compress_preset,
    )

    # 先规划，让用户看到将采用什么策略
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="drama_merger_plan_"))
    try:
        strategy, steps = plan_merge(items, report, options, env, workdir)
        print(f"\n采用策略：{strategy.label}")
        if compress_preset is not None:
            print(f"压缩档位：{compress_preset.label}（{compress_preset.name}）"
                  f" - {compress_preset.desc}")
        print(f"执行步骤：")
        for s in steps:
            print(f"  - {s.label}")
        print(f"\n输出文件：{output}")

        if args.dry_run:
            print("\n[试运行模式] 未实际执行。完整命令如下：")
            for s in steps:
                print(f"\n# {s.label}")
                print("  " + " ".join(str(c) for c in s.cmd))
            return 0

        print(f"\n开始合并（预计输出时长 {_fmt_duration(sum(i.duration for i in items))}）...")
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    t0 = time.time()
    try:
        result = execute_merge(items, report, options, env)
    except RuntimeError as exc:
        progress.done()
        print(f"\n合并失败：\n{exc}", file=sys.stderr)
        return 1

    progress.done()
    print(f"\n合并完成！")
    print(f"  输出：{result.output}")
    print(f"  策略：{result.strategy.label}")
    print(f"  编码器：{result.encoder_used}")
    print(f"  时长：{_fmt_duration(result.duration)}")
    print(f"  大小：{_fmt_size(result.size)}")
    if compress_preset is not None:
        src_total = sum(i.size for i in items)
        if src_total > 0 and result.size > 0:
            ratio = result.size / src_total
            print(f"  压缩：素材 {_fmt_size(src_total)} -> 成片 {_fmt_size(result.size)}"
                  f"（{ratio * 100:.1f}%，省 {_fmt_size(src_total - result.size)}）")
    print(f"  耗时：{result.elapsed:.1f} 秒（{result.duration / max(result.elapsed, 0.001):.1f}x 实时速度）")

    # 字幕处理
    if args.subtitles or args.burn_subtitles:
        print("\n处理字幕...")
        episodes: list[EpisodeSubtitle] = []
        offset = 0.0
        for info in items:
            subs = find_subtitles_for(info.path)
            if subs:
                episodes.append(EpisodeSubtitle(info.path, subs, offset=offset))
            offset += info.duration

        if not episodes:
            print("  未找到配套字幕文件，跳过")
        else:
            srt_out = output.with_suffix(".srt")
            sub_path, info_text = merge_subtitles(episodes, srt_out)
            if sub_path:
                print(f"  外挂字幕已生成：{sub_path}")
                print(f"  {info_text}")
                if args.burn_subtitles:
                    burned = output.with_name(output.stem + "_硬字幕" + output.suffix)
                    print(f"  正在烧入字幕（耗时较长）...")
                    from drama_merger.ffmpeg_env import _run

                    cmd = burn_subtitles(output, sub_path, burned, env)
                    code, out, err = _run(cmd, timeout=7200)
                    if code == 0:
                        print(f"  硬字幕视频：{burned}")
                    else:
                        print(f"  字幕烧入失败：{err.strip().splitlines()[-1] if err.strip() else '未知错误'}")
            else:
                print(f"  字幕合并失败：{info_text}")

    return 0


# ---------------------------------------------------------------------------
# presets 子命令
# ---------------------------------------------------------------------------

def cmd_presets(args: argparse.Namespace) -> int:
    """列出所有压缩档位。"""
    print("可用的压缩档位（merge --compress 后接档位名）：")
    print()
    print(describe_all())
    print()
    print("  用法示例：")
    print('    python cli.py merge "D:/短剧/某剧" --compress           # 默认 upload 档')
    print('    python cli.py merge "D:/短剧/某剧" --compress hevc      # HEVC 档')
    print()
    print("  说明：合并即压缩 —— 拼接同时编码，一次成型，不产生中间大文件。")
    return 0


# ---------------------------------------------------------------------------
# publish 子命令
# ---------------------------------------------------------------------------

def cmd_publish(args: argparse.Namespace) -> int:
    """由成片生成发布素材（章节时间轴 / 文案 / 缩略图）。"""
    from drama_merger.publish import (
        ep_num, fmt_ts, generate_all, generic_meta, match_template,
    )

    src_dir = Path(args.source).expanduser() if args.source else None
    if not src_dir or not src_dir.is_dir():
        print("错误：请用 --source 指定分集素材所在目录"
              "（章节时间轴与缩略图需要它）", file=sys.stderr)
        return 2

    env = get_ffmpeg_env()
    numbered = []
    for f in collect_video_files(src_dir):
        n = ep_num(f.name)
        if n is not None:
            numbered.append((n, f))
    if len(numbered) < 2:
        print(f"错误：在 {src_dir} 中找到的分集不足 2 个", file=sys.stderr)
        return 1
    numbered.sort(key=lambda x: x[0])
    episodes = [(n, probe_file(f, env).duration) for n, f in numbered]
    print(f"共 {len(episodes)} 集")

    title = args.title or src_dir.name
    matched = match_template(title)
    meta = matched or generic_meta(title)
    if matched:
        print(f"已匹配内置文案模板：{meta.title}")

    out_dir = Path(args.output).expanduser() if args.output else src_dir / "youtube"
    res = generate_all(
        meta, episodes, out_dir,
        cover_dir=src_dir,
        progress=lambda m: print(f"  {m}"),
    )

    print()
    print("=" * 60)
    print(f"发布素材已生成：{res.count} 集 / {fmt_ts(res.total)}")
    print(f"  输出目录：{res.out_dir}")
    if res.chapters_path:
        print(f"  章节时间轴：{res.chapters_path.name}")
    if res.txt_path:
        print(f"  发布文案：{res.txt_path}")
    if res.thumb_path:
        print(f"  缩略图：{res.thumb_path}")
    for w in res.warnings:
        print(f"  注意：{w}")
    return 0


# ---------------------------------------------------------------------------
# gui 子命令
# ---------------------------------------------------------------------------

def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from drama_merger.gui import launch
    except ImportError as exc:
        print(f"无法加载图形界面：{exc}", file=sys.stderr)
        print("请确认 Python 安装包含 tkinter 组件。", file=sys.stderr)
        return 1
    launch()
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drama-merger",
        description="短剧视频合并工具 —— 把目录下的多集短剧合并成一部完整视频",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version="drama-merger 1.0.0")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # --- probe ---
    p_probe = sub.add_parser("probe", help="探测目录中的视频并分析一致性（不改动任何文件）")
    p_probe.add_argument("directory", help="短剧分集所在目录")
    p_probe.add_argument("-r", "--recursive", action="store_true", help="递归搜索子目录")
    p_probe.add_argument("--refresh-env", action="store_true", help="强制重新探测 ffmpeg 环境")
    p_probe.set_defaults(func=cmd_probe)

    # --- merge ---
    p_merge = sub.add_parser("merge", help="合并目录中的视频")
    p_merge.add_argument("directory", help="短剧分集所在目录")
    p_merge.add_argument("-o", "--output", help="输出文件路径（默认：<父目录>/<目录名>_merged.mp4）")
    p_merge.add_argument("-r", "--recursive", action="store_true", help="递归搜索子目录")
    p_merge.add_argument("-n", "--limit", type=int, help="只合并前 N 个片段（用于快速验证）")
    p_merge.add_argument("--strategy", choices=["a", "b", "c", "A", "B", "C"],
                         help="强制指定策略：a=无损拼接 b=归一化拼接 c=滤镜重编码")
    p_merge.add_argument("--encoder", choices=[e.value for e in Encoder], default="auto",
                         help="视频编码器：auto(自动择优) / nvenc(硬件) / x264(软件)")
    p_merge.add_argument("--crf", type=int, default=20, help="画质参数（越小越清晰，默认 20）")
    p_merge.add_argument("--preset", default="veryfast", help="libx264 速度预设（默认 veryfast）")
    p_merge.add_argument("--compress", nargs="?", const="upload", default=None,
                         metavar="档位",
                         help="合并即压缩（一次成型，不产生中间大文件）。"
                              "不带值默认 upload 档；可用档位见 presets 子命令")
    p_merge.add_argument("--trim-head", type=float, default=0.0, help="裁掉每集开头的秒数")
    p_merge.add_argument("--trim-tail", type=float, default=0.0, help="裁掉每集结尾的秒数")
    p_merge.add_argument("--fade-in", type=float, default=0.0, help="整片开头淡入秒数")
    p_merge.add_argument("--fade-out", type=float, default=0.0, help="整片结尾淡出秒数")
    p_merge.add_argument("--subtitles", action="store_true", help="合并配套字幕并输出外挂字幕文件")
    p_merge.add_argument("--burn-subtitles", action="store_true", help="把字幕烧入视频（硬字幕，耗时）")
    p_merge.add_argument("--keep-temp", action="store_true", help="保留临时文件（排错用）")
    p_merge.add_argument("--dry-run", action="store_true", help="只显示将执行的命令，不实际执行")
    p_merge.add_argument("--verbose", action="store_true", help="输出详细环境信息")
    p_merge.add_argument("--refresh-env", action="store_true", help="强制重新探测 ffmpeg 环境")
    p_merge.set_defaults(func=cmd_merge)

    # --- presets ---
    p_pre = sub.add_parser("presets", help="列出所有可用的压缩档位")
    p_pre.set_defaults(func=cmd_presets)

    # --- publish ---
    p_pub = sub.add_parser("publish", help="由成片生成发布素材（章节/文案/缩略图）")
    p_pub.add_argument("--source", required=True,
                       help="分集素材所在目录（章节时间轴与缩略图需要它）")
    p_pub.add_argument("--title", help="剧名（用于匹配内置文案模板，默认取目录名）")
    p_pub.add_argument("-o", "--output", help="输出目录（默认：<素材目录>/youtube）")
    p_pub.set_defaults(func=cmd_publish)

    # --- gui ---
    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
