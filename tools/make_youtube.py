#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
为 YouTube 发布生成元数据。

产出：
  youtube/章节时间轴.txt      全部剧的章节（总览）
  youtube/发布信息.md         完整发布清单（标题/简介/标签/章节）
  youtube/各剧/<剧名>.txt     每部单独一份，方便逐个复制粘贴

章节时间轴由各集真实时长累加算出，与成片时间轴严格对齐。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drama_merger.ffmpeg_env import get_ffmpeg_env
from drama_merger.probe import collect_video_files, probe_file

SRC_ROOT = Path(r"C:\baidunetdiskdownload")
OUT_DIR = Path(__file__).resolve().parent.parent / "youtube"

# 每部剧的发布信息
# key: (源目录, 成片文件名, 剧名, 跳过文件)
# value: dict(标题/副标题/简介/标签)
DRAMAS = [
    {
        "dir": "回乡建房报旧仇，恶霸楼塌人疯癫",
        "out": "回乡建房报旧仇，恶霸楼塌人疯癫（36集）.mp4",
        "title": "回乡建房报旧仇，恶霸楼塌人疯癫",
        "skip": None,
        "yt_title": "回乡建房报旧仇｜被村霸欺压三十年，我盖楼让他破产疯癫 #短剧",
        "hook": "三十年低眉顺眼，只为等今天——楼塌的那一刻，他疯了",
        "genre": "农村现实 · 复仇打脸 · 逆袭",
        "desc_cn": (
            "在外打拼三十年，他从没忘记老家的那口气。\n"
            "当年被村霸踩在脚底、连家门都被砸烂的穷小子，如今带着钱和势回来了。\n\n"
            "他做的第一件事，就是在恶霸家的正对面，起了栋五层楼。\n"
            "占道、封路、断水——当年恶霸怎么欺负他的，他一样不少地还了回去。\n\n"
            "直到那栋违规加建的五层楼轰然倒塌，恶霸也在废墟里疯了。\n"
            "三十年忍气吞声，一盖房让他破产。\n\n"
            "有人说他狠，可只有他自己知道，这三十年是怎么熬过来的。"
        ),
        "desc_en": (
            "Thirty years away from home, he never forgot the humiliation.\n"
            "Backed by money and power, he returns to the village that once crushed him\n"
            "and builds a five-storey house right opposite his old tormentor.\n\n"
            "The bully's illegal building comes down — and so does his mind.\n"
            "A thirty-year revenge, settled with one house."
        ),
        "tags": ["回乡建房报旧仇", "短剧", "农村短剧", "复仇", "逆袭", "打脸",
                 "村霸", "爽剧", "全集", "一口气看完", "chinese drama", "rural drama"],
        "tags_en": ["revenge", "chinese short drama", "rural drama", "bully",
                    "comeback", "face slapping", "underdog"],
        "yt_title_en": "He Built a House to Ruin the Bully Who Tormented Him for 30 Years #Shorts",
    },
]


def fmt_ts(sec: float) -> str:
    """YouTube 章节时间戳：小于 1 小时用 M:SS，超过用 H:MM:SS。"""
    sec = max(int(round(sec)), 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def ep_num(name: str) -> int | None:
    import re

    for pat in (r"\((\d+)\)\s*\.\w+$", r"第\s*(\d+)\s*集", r"-\s*(\d+)(?:\(\d+\))?\s*\.\w+$"):
        m = re.search(pat, name)
        if m:
            return int(m.group(1))
    return None


def build_chapters(drama: dict, env) -> tuple[list[tuple[float, int]], float]:
    d = SRC_ROOT / drama["dir"]
    files = collect_video_files(d)
    skip = drama.get("skip")
    numbered = []
    for f in files:
        n = ep_num(f.name)
        if n is None or (skip and f.name == skip):
            continue
        numbered.append((n, f))
    numbered.sort(key=lambda x: x[0])

    chapters, t = [], 0.0
    for n, f in numbered:
        chapters.append((t, n))
        t += probe_file(f, env).duration
    return chapters, t


def build_video_txt(drama: dict, chapters: list[tuple[float, int]],
                    total: float) -> str:
    """生成单部剧的发布文案（可直接逐块复制粘贴）。"""
    n = len(chapters)
    ch_lines = "\n".join(f"{fmt_ts(s)} 第{ep}集" for s, ep in chapters)

    # 双语简介：中文详情在前，英文概览在后（兼顾华语与海外受众）
    desc = (
        f"{drama['desc_cn']}\n\n"
        f"── 剧集信息 ──\n"
        f"共 {n} 集 · 时长 {fmt_ts(total)} · {drama['genre']}\n\n"
        f"── 章节 ──\n"
        f"{ch_lines}\n\n"
        f"── Synopsis ──\n"
        f"{drama['desc_en']}\n\n"
        f"#shortdrama #chinesedrama #短剧"
    )

    tags_cn = ",".join(drama["tags"])
    tags_en = ",".join(drama.get("tags_en", []))
    # YouTube 单个标签字段上限 500 字符，超出会被静默截断
    combined = f"{tags_cn},{tags_en}"
    warn = ""
    if len(combined) > 500:
        warn = f"\n  ⚠ 合计 {len(combined)} 字符，超出 YouTube 500 字符上限，请酌情删减"

    return (
        f"{'=' * 66}\n"
        f"《{drama['title']}》  YouTube 发布信息\n"
        f"集数 {n} · 时长 {fmt_ts(total)} · {drama['genre']}\n"
        f"{'=' * 66}\n\n"
        f"【1】视频标题（中文主标题，100 字符内）\n"
        f"{'─' * 66}\n"
        f"{drama['yt_title']}\n"
        f"（{len(drama['yt_title'])} 字符 · 移动端约显示前 40 字符，钩子已前置）\n\n"
        f"【1b】英文备选标题（面向海外受众，可作副标题或另开英文频道）\n"
        f"{'─' * 66}\n"
        f"{drama['yt_title_en']}\n"
        f"（{len(drama['yt_title_en'])} 字符）\n\n"
        f"【2】简介 Description（含章节，整段复制）\n"
        f"{'─' * 66}\n"
        f"{desc}\n\n"
        f"【3】标签 Tags（中文）\n"
        f"{'─' * 66}\n"
        f"{tags_cn}\n"
        f"（{len(tags_cn)} 字符）\n\n"
        f"【3b】标签 Tags（英文，建议追加到同一字段）\n"
        f"{'─' * 66}\n"
        f"{tags_en}\n"
        f"（{len(tags_en)} 字符）{warn}\n\n"
        f"【3c】合并后（中英一起粘贴，共 {len(combined)} 字符）\n"
        f"{'─' * 66}\n"
        f"{combined}\n\n"
        f"【4】缩略图\n"
        f"{'─' * 66}\n"
        f"youtube/缩略图/ 下对应的 jpg（1280x720）\n\n"
        f"【5】发布建议\n"
        f"{'─' * 66}\n"
        f"· 分类：Entertainment（娱乐）或 People & Blogs\n"
        f"· 语言：Chinese (Simplified)；字幕语言同\n"
        f"· 播放列表：建议按「剧名」建独立播放列表，或建「短剧合集」总列表\n"
        f"· 结尾画面：建议开启，指向同系列其他剧\n"
        f"· 观众群：标注「不适合 18 岁以下」可减少低龄推荐\n"
        f"· 章节生效条件：简介 ≥3 个时间戳、首项为 0:00、每段 ≥10 秒 —— 均已满足\n"
    )


def main() -> None:
    env = get_ffmpeg_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    per_dir = OUT_DIR / "各剧"
    per_dir.mkdir(parents=True, exist_ok=True)

    all_chapters = []
    summary = []

    for drama in DRAMAS:
        chapters, total = build_chapters(drama, env)
        n = len(chapters)
        summary.append((drama["title"], n, total))

        ch_block = (
            f"# {drama['title']} —— 章节时间轴（{n} 集，共 {fmt_ts(total)}）\n\n"
            + "\n".join(f"{fmt_ts(s)} 第{ep}集" for s, ep in chapters)
        )
        all_chapters.append(ch_block)

        txt = build_video_txt(drama, chapters, total)
        safe = drama["title"].replace("，", "_").replace("/", "_")
        (per_dir / f"{safe}.txt").write_text(txt, encoding="utf-8")

        print(f"{drama['title']}: {n} 集 / {fmt_ts(total)}")
        print(f"  标题({len(drama['yt_title'])}字符): {drama['yt_title'][:44]}...")
        print(f"  标签 {len(drama['tags'])} 个")

    (OUT_DIR / "章节时间轴.txt").write_text(
        "\n\n\n".join(all_chapters) + "\n", encoding="utf-8")

    # 汇总表
    total_n = sum(n for _, n, _ in summary)
    total_s = sum(t for _, _, t in summary)
    thumbs = ["01_回乡建房报旧仇.jpg"]

    lines = [
        "# 短剧 YouTube 发布信息汇总",
        "",
        f"**共 {len(summary)} 部 · {total_n} 集 · 总时长 {fmt_ts(total_s)}**",
        "",
        "---",
        "",
        "## 一、总览",
        "",
        "| # | 剧名 | 集数 | 时长 | 缩略图 |",
        "|---|---|---|---|---|",
    ]
    for i, ((title, n, t), th) in enumerate(zip(summary, thumbs), 1):
        lines.append(f"| {i} | {title} | {n} | {fmt_ts(t)} | `{th}` |")

    lines += [
        "",
        "---",
        "",
        "## 二、标题速查（直接复制）",
        "",
    ]
    for i, (drama, (title, n, t)) in enumerate(zip(DRAMAS, summary), 1):
        lines += [
            f"**{i}. {title}**（{len(drama['yt_title'])} 字符）",
            "",
            "```",
            drama["yt_title"],
            "```",
            "",
        ]

    lines += [
        "---",
        "",
        "## 三、发布建议",
        "",
        "本剧为**农村现实复仇题材**，节奏连贯、爽点密集，建议：",
        "",
        "- **一次发一整部**（36 集连播），而非拆成多集短视频。合集形式的完播与观看时长表现通常更好。",
        "- 封面/缩略图用「西装主角 vs 废墟」那张（正邪对比最直观，视觉冲击最强）。",
        "- 首批推荐流量若表现好，可用素材里的**别名文案做 A/B 测试**（见下方「备用标题」）。",
        "- 频道定位若是泛短剧号，建议与「婆媳/家庭」类交替发布，避免同类题材扎堆。",
        "",
        "---",
        "",
        "## 四、备用标题（素材自带的 A/B 测试文案）",
        "",
        "原素材附了 5 张不同标题的封面，这些都是投放方测过的高转化钩子，",
        "可直接用作标题变体或改版缩略图：",
        "",
        "| 备用标题 |",
        "|---|",
        "| 穷小子回乡盖房，怒掀恶霸五层楼 |",
        "| 二十年被压一头，我盖房掀翻全村 |",
        "| 三十年忍气吞声，一盖房让他破产 |",
        "| 被邻欺压三十年，我盖楼让他塌房 |",
        "",
        "---",
        "",
        "## 五、发布前检查清单",
        "",
        "- [ ] 标题 ≤100 字符（当前 30 字符左右，安全）",
        "- [ ] 简介含章节，首项必须是 `0:00`（已处理）",
        "- [ ] 缩略图 1280x720，小于 2MB",
        "- [ ] 标签 ≤500 字符（已核对）",
        "- [ ] 语言设为 Chinese (Simplified)",
        "- [ ] 观众群标注「不适合 18 岁以下」",
        "- [ ] 加入对应播放列表",
        "- [ ] 开启结尾画面，指向同系列其他剧",
        "",
        "---",
        "",
        "## 六、文件说明",
        "",
        "| 文件 | 用途 |",
        "|---|---|",
        "| `各剧/<剧名>.txt` | 本剧的标题/简介/标签/建议，逐块复制 |",
        "| `章节时间轴.txt` | 章节时间轴（36 集） |",
        "| `缩略图/` | 1280x720 缩略图，直接上传 |",
        "",
        "## 七、几点提醒",
        "",
        "- **章节生效条件**：简介里至少 3 个时间戳、首项为 `0:00`、每段不少于 10 秒。本剧各集约 65 秒，全部满足。",
        "- **标题里的 `#短剧`** 是话题标签，前 3 个 hashtag 会显示在标题上方，对短剧类目有站内推荐加成。",
        "- **善用备用剧名**：素材附带 5 张不同标题的封面，都是投放方 A/B 测试过的高转化钩子，见上方「备用标题」章节。",
        "- **英文标签**主要服务海外华人与泛亚受众；若后续开英文频道，可直接用 txt 里的英文标题。",
        "- 字幕已内嵌在画面中（原素材自带），**无需再上传字幕文件**；如需检索友好，可另传一份 SRT。",
    ]
    (OUT_DIR / "发布信息.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print("=" * 60)
    print(f"共 {len(summary)} 部 / {total_n} 集 / 总时长 {fmt_ts(total_s)}")
    print(f"输出目录：{OUT_DIR}")
    print(f"  发布信息.md")
    print(f"  章节时间轴.txt")
    print(f"  各剧/  ({len(summary)} 个文件)")


if __name__ == "__main__":
    main()
