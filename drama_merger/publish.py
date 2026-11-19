#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布信息生成（库模块）。

把"合并成片 → 准备发布素材"这条链路收进库内，供 GUI 与 CLI 共用：
    1. 章节时间轴   —— 按各集真实时长累加，与成片严格对齐
    2. 发布文案     —— 标题 / 简介（含章节）/ 中英标签
    3. 缩略图       —— 竖屏封面转 1280x720 横屏

设计：数据（剧名/文案/标签）与生成逻辑分离。
调用方传入 DramaMeta，本模块负责产出文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# 数据中心
# ---------------------------------------------------------------------------

@dataclass
class DramaMeta:
    """一部剧的发布元信息。"""

    title: str                          # 剧名
    genre: str                          # 题材标签（展示用）
    yt_title: str                       # YouTube 标题（中文，含钩子）
    yt_title_en: str                    # 英文备选标题
    desc_cn: str                        # 中文简介
    desc_en: str                        # 英文简介
    tags: list[str] = field(default_factory=list)
    tags_en: list[str] = field(default_factory=list)
    alt_titles: list[str] = field(default_factory=list)   # 素材自带的备用名（A/B 文案）
    cover: str = ""                     # 封面文件名（相对封面源目录）
    top_cut: float = 0.0                # 缩略图：裁掉封面顶部比例
    bottom_cut: float = 0.04            # 缩略图：裁掉封面底部比例
    thumb_title: str = ""               # 缩略图主标题（留空则用剧名）
    thumb_sub: str = ""                 # 缩略图副标题（留空则用首个备用名）


# ---------------------------------------------------------------------------
# 章节与文案
# ---------------------------------------------------------------------------

def fmt_ts(sec: float) -> str:
    """YouTube 章节时间戳：不足 1 小时用 M:SS，超过用 H:MM:SS。"""
    sec = max(int(round(sec)), 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def ep_num(name: str) -> int | None:
    """从文件名提取集号，支持多种命名风格。"""
    for pat in (
        r"第\s*(\d+)\s*集",                 # 某剧-第10集.mp4
        r"\((\d+)\)\s*\.\w+$",              # 1 (12).mp4
        r"-\s*(\d+)(?:\(\d+\))?\s*\.\w+$",  # 56集58分钟-3.mp4
    ):
        m = re.search(pat, name)
        if m:
            return int(m.group(1))
    return None


def build_chapters(video_files: list[tuple[int, float]]) -> list[tuple[float, int]]:
    """由 (集号, 时长) 列表生成章节时间轴。

    入参需已按集号排序。返回 [(起始秒, 集号)]。
    """
    chapters: list[tuple[float, int]] = []
    t = 0.0
    for ep, dur in video_files:
        chapters.append((t, ep))
        t += dur
    return chapters


def build_description(meta: DramaMeta, chapters: list[tuple[float, int]],
                      total: float) -> str:
    """生成 YouTube 简介（含章节，可直接整段粘贴）。"""
    n = len(chapters)
    ch_lines = "\n".join(f"{fmt_ts(s)} 第{ep}集" for s, ep in chapters)
    return (
        f"{meta.desc_cn}\n\n"
        f"── 剧集信息 ──\n"
        f"共 {n} 集 · 时长 {fmt_ts(total)} · {meta.genre}\n\n"
        f"── 章节 ──\n"
        f"{ch_lines}\n\n"
        f"── Synopsis ──\n"
        f"{meta.desc_en}\n\n"
        f"#shortdrama #chinesedrama #短剧"
    )


def combined_tags(meta: DramaMeta) -> str:
    """中英标签合并（YouTube 单字段上限 500 字符）。"""
    parts = [",".join(meta.tags)]
    if meta.tags_en:
        parts.append(",".join(meta.tags_en))
    return ",".join(p for p in parts if p)


def tags_length_warning(meta: DramaMeta) -> str:
    """标签超限提示（空串表示合规）。"""
    n = len(combined_tags(meta))
    if n > 500:
        return f"合计 {n} 字符，超出 YouTube 500 字符上限，请酌情删减"
    return ""


def build_chapter_block(meta: DramaMeta, chapters: list[tuple[float, int]],
                        total: float) -> str:
    """章节时间轴文本块。"""
    n = len(chapters)
    return (
        f"# {meta.title} —— 章节时间轴（{n} 集，共 {fmt_ts(total)}）\n\n"
        + "\n".join(f"{fmt_ts(s)} 第{ep}集" for s, ep in chapters)
    )


def build_publish_txt(meta: DramaMeta, chapters: list[tuple[float, int]],
                      total: float) -> str:
    """单部剧的发布信息（逐块可复制）。"""
    n = len(chapters)
    desc = build_description(meta, chapters, total)
    warn = tags_length_warning(meta)
    sep = "─" * 66

    lines = [
        "=" * 66,
        f"《{meta.title}》  YouTube 发布信息",
        f"集数 {n} · 时长 {fmt_ts(total)} · {meta.genre}",
        "=" * 66,
        "",
        "【1】视频标题（中文主标题，100 字符内）",
        sep,
        meta.yt_title,
        f"（{len(meta.yt_title)} 字符 · 移动端约显示前 40 字符，钩子已前置）",
        "",
    ]
    if meta.yt_title_en:
        lines += [
            "【1b】英文备选标题（面向海外受众）",
            sep,
            meta.yt_title_en,
            f"（{len(meta.yt_title_en)} 字符）",
            "",
        ]
    lines += [
        "【2】简介 Description（含章节，整段复制）",
        sep,
        desc,
        "",
        "【3】标签 Tags（中文）",
        sep,
        ",".join(meta.tags),
        f"（{len(','.join(meta.tags))} 字符）",
        "",
    ]
    if meta.tags_en:
        lines += [
            "【3b】标签 Tags（英文，建议追加到同一字段）",
            sep,
            ",".join(meta.tags_en),
            f"（{len(','.join(meta.tags_en))} 字符）",
            "",
        ]
    lines += [
        "【3c】合并后（中英一起粘贴）",
        sep,
        combined_tags(meta),
        f"（{len(combined_tags(meta))} 字符）" + (f"  ⚠ {warn}" if warn else ""),
        "",
    ]
    if meta.alt_titles:
        lines += [
            "【4】备用标题（素材自带的 A/B 测试文案）",
            sep,
        ]
        lines += [f"  · {t}" for t in meta.alt_titles]
        lines += ["", "  这些是投放方实测过的高转化钩子，可用于标题改版测试。", ""]
    lines += [
        "【5】缩略图",
        sep,
        "  见 youtube/缩略图/ 目录下对应 jpg（1280x720）",
        "",
        "【6】发布建议",
        sep,
        "  · 分类：Entertainment（娱乐）或 People & Blogs",
        "  · 语言：Chinese (Simplified)",
        "  · 建议开启结尾画面，指向同系列其他剧",
        "  · 观众群标注「不适合 18 岁以下」可减少低龄推荐",
        "  · 字幕已内嵌画面，无需另传字幕文件",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------

THUMB_W, THUMB_H = 1280, 720
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"


def _load_font(path: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _fit_text(draw, text: str, font_path: str, max_w: int, start_size: int):
    """自动缩字号，直到文字宽度不超过 max_w。"""
    size = start_size
    while size > 18:
        f = _load_font(font_path, size)
        box = draw.textbbox((0, 0), text, font=f)
        if box[2] - box[0] <= max_w:
            return f
        size -= 4
    return _load_font(font_path, 24)


def _trim_border(img, px: int = 14):
    """裁掉封面自带的细边框（露出到缩略图上会显得突兀）。"""
    w, h = img.size
    px = max(2, min(px, w // 20, h // 20))
    return img.crop((px, px, w - px, h - px))


def _crop_by_ratio(img, top: float = 0.0, bottom: float = 0.0):
    """按比例裁掉顶部/底部（用于去掉封面自带的剧名与生成水印）。

    保险：最多裁掉一半，避免误填参数把画面裁没。
    """
    w, h = img.size
    t = int(h * top) if top > 0 else 0
    b = int(h * bottom) if bottom > 0 else 0
    if h - t - b < h * 0.4:
        t = min(t, int(h * 0.3))
        b = min(b, int(h * 0.3))
    return img.crop((0, t, w, h - b))


def make_thumbnail(src: Path, dst: Path, title: str, subtitle: str = "",
                   top_cut: float = 0.0, bottom_cut: float = 0.04,
                   brand: str = "精选短剧", footer: str = "全集连播 · 一口气看完",
                   progress=None) -> None:
    """生成 YouTube 缩略图（竖屏封面 -> 1280x720 横屏）。

    做法：原图放大模糊铺底 + 封面贴右侧 + 左侧暗渐变压字。
    """
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    base = _trim_border(Image.open(src).convert("RGB"))
    base = _crop_by_ratio(base, top_cut, bottom_cut)
    bw, bh = base.size

    W, H = THUMB_W, THUMB_H
    bar_h = 56
    usable_h = H - bar_h

    # 背景：整图放大裁切铺满 + 模糊 + 压暗
    scale = max(W / bw, H / bh)
    bg = base.resize((int(bw * scale), int(bh * scale)), Image.LANCZOS)
    left = (bg.width - W) // 2
    top = (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H))
    bg = bg.filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)
    canvas = bg

    # 前景：封面贴右侧，避开底部信息条
    fg_h = int(usable_h * 0.97)
    fg_w = int(bw * (fg_h / bh))
    fg = base.resize((fg_w, fg_h), Image.LANCZOS)
    canvas.paste(fg, (W - fg_w - 26, (usable_h - fg_h) // 2))

    draw = ImageDraw.Draw(canvas, "RGBA")

    # 左侧暗色渐变，保证文字可读
    grad_w = int(W * 0.58)
    for x in range(grad_w):
        alpha = int(215 * (1 - x / grad_w) ** 0.85)
        draw.line([(x, 0), (x, H)], fill=(8, 10, 16, alpha))

    # 品牌角标
    if brand:
        tag_font = _load_font(FONT_BOLD, 30)
        draw.rounded_rectangle([48, 48, 48 + 168, 48 + 52], radius=10,
                               fill=(220, 38, 38, 235))
        draw.text((70, 58), brand, font=tag_font, fill=(255, 255, 255))

    # 主标题
    t_font = _fit_text(draw, title, FONT_BOLD, int(W * 0.52), 92)
    tb = draw.textbbox((0, 0), title, font=t_font)
    th = tb[3] - tb[1]
    block_h = th + (46 + 44 if subtitle else 0)
    ty = (usable_h - block_h) // 2 + 20
    draw.text((56, ty), title, font=t_font, fill=(255, 255, 255),
              stroke_width=8, stroke_fill=(0, 0, 0))

    # 副标题（实色绘制；描边会与主标题描边叠加出重影）
    if subtitle:
        s_font = _fit_text(draw, subtitle, FONT_REG, int(W * 0.50), 44)
        draw.text((58, ty + th + 46), subtitle, font=s_font, fill=(255, 214, 102))

    # 底部信息条
    if footer:
        bar_font = _load_font(FONT_REG, 28)
        draw.rectangle([0, H - bar_h, W, H], fill=(0, 0, 0, 200))
        draw.text((48, H - bar_h + 12), footer, font=bar_font, fill=(235, 235, 235))

    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, quality=92, subsampling=0)


# ---------------------------------------------------------------------------
# 编排：一次性产出全部发布素材
# ---------------------------------------------------------------------------

@dataclass
class PublishOutput:
    """生成结果。"""

    out_dir: Path
    txt_path: Path | None = None
    chapters_path: Path | None = None
    thumb_path: Path | None = None
    summary_path: Path | None = None
    total: float = 0.0
    count: int = 0
    warnings: list[str] = field(default_factory=list)


def generate_all(
    meta: DramaMeta,
    episodes: list[tuple[int, float]],
    out_dir: Path,
    cover_dir: Path | None = None,
    per_drama: bool = True,
    progress=None,
) -> PublishOutput:
    """生成一部剧的全部发布素材。

    参数：
        episodes  —— [(集号, 时长)]，需已按集号排序
        cover_dir —— 封面所在目录（None 表示跳过缩略图）
    """
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    chapters = build_chapters(episodes)
    total = sum(d for _, d in episodes)
    n = len(chapters)
    res = PublishOutput(out_dir=out_dir, total=total, count=n)

    # 章节时间轴
    res.chapters_path = out_dir / "章节时间轴.txt"
    res.chapters_path.write_text(
        build_chapter_block(meta, chapters, total) + "\n", encoding="utf-8")
    say(f"章节时间轴已生成（{n} 集 / {fmt_ts(total)}）")

    # 发布文案
    if per_drama:
        d = out_dir / "各剧"
        d.mkdir(parents=True, exist_ok=True)
        safe = meta.title.replace("，", "_").replace("/", "_")
        res.txt_path = d / f"{safe}.txt"
    else:
        res.txt_path = out_dir / f"{meta.title}.txt"
    res.txt_path.write_text(
        build_publish_txt(meta, chapters, total), encoding="utf-8")
    say(f"发布文案已生成：{res.txt_path.name}")

    warn = tags_length_warning(meta)
    if warn:
        res.warnings.append(warn)
        say(f"提醒：{warn}")

    # 缩略图
    if cover_dir and meta.cover:
        src = Path(cover_dir) / meta.cover
        if src.is_file():
            safe = meta.title.replace("，", "_").replace("/", "_")
            res.thumb_path = out_dir / "缩略图" / f"01_{safe[:12]}.jpg"
            # 缩略图上的文字要短：主标题优先取 thumb_title，副标题用首个备用名
            t_title = meta.thumb_title or _short_title(meta.title)
            t_sub = meta.thumb_sub or (
                meta.alt_titles[0] if meta.alt_titles else ""
            )
            try:
                make_thumbnail(
                    src, res.thumb_path, t_title,
                    subtitle=t_sub,
                    top_cut=meta.top_cut, bottom_cut=meta.bottom_cut,
                )
                say(f"缩略图已生成：{res.thumb_path.name}")
            except ImportError as exc:
                # 只把真正缺 Pillow 的情况报成"未安装"，其余照实抛出
                if "PIL" in str(exc) or "Pillow" in str(exc):
                    res.warnings.append("未安装 Pillow，跳过缩略图生成")
                    res.thumb_path = None
                else:
                    raise
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"缩略图生成失败：{exc}")
                res.thumb_path = None
        else:
            res.warnings.append(f"找不到封面文件：{src.name}")
            say(f"跳过缩略图（找不到 {src.name}）")

    return res


def _short_title(title: str, limit: int = 8) -> str:
    """取适合放在缩略图上的短标题。

    剧名常带逗号分句（「回乡建房报旧仇，恶霸楼塌人疯癫」），
    缩略图取前半句即可，过长会触发自动缩字号、可读性下降。
    """
    head = title.split("，")[0].split(",")[0].strip()
    return head if head else title[:limit]


# ---------------------------------------------------------------------------
# 内置文案模板
#
# 剧名 -> DramaMeta。匹配到就用现成的标题/简介/标签；
# 匹配不到则按剧名生成通用文案（保持工具通用，不写死单部剧）。
# 新增剧目的文案往这里加即可。
#
# 注意：模板放在库模块（而非 gui.py），否则 CLI 版会连带依赖 tkinter。
# ---------------------------------------------------------------------------

DRAMA_TEMPLATES: dict[str, DramaMeta] = {
    "回乡建房报旧仇": DramaMeta(
        title="回乡建房报旧仇，恶霸楼塌人疯癫",
        genre="农村现实 · 复仇打脸 · 逆袭",
        yt_title="回乡建房报旧仇｜被村霸欺压三十年，我盖楼让他破产疯癫 #短剧",
        yt_title_en="He Built a House to Ruin the Bully Who Tormented Him for 30 Years #Shorts",
        desc_cn=(
            "在外打拼三十年，他从没忘记老家的那口气。\n"
            "当年被村霸踩在脚底、连家门都被砸烂的穷小子，如今带着钱和势回来了。\n\n"
            "他做的第一件事，就是在恶霸家的正对面，起了栋五层楼。\n"
            "占道、封路、断水——当年恶霸怎么欺负他的，他一样不少地还了回去。\n\n"
            "直到那栋违规加建的五层楼轰然倒塌，恶霸也在废墟里疯了。\n"
            "三十年忍气吞声，一盖房让他破产。\n\n"
            "有人说他狠，可只有他自己知道，这三十年是怎么熬过来的。"
        ),
        desc_en=(
            "Thirty years away from home, he never forgot the humiliation.\n"
            "Backed by money and power, he returns to the village that once crushed him\n"
            "and builds a five-storey house right opposite his old tormentor.\n\n"
            "The bully's illegal building comes down — and so does his mind.\n"
            "A thirty-year revenge, settled with one house."
        ),
        tags=["回乡建房报旧仇", "短剧", "农村短剧", "复仇", "逆袭", "打脸",
              "村霸", "爽剧", "全集", "一口气看完", "chinese drama", "rural drama"],
        tags_en=["revenge", "chinese short drama", "rural drama", "bully",
                 "comeback", "face slapping", "underdog"],
        alt_titles=["穷小子回乡盖房，怒掀恶霸五层楼", "二十年被压一头，我盖房掀翻全村",
                    "三十年忍气吞声，一盖房让他破产", "被邻欺压三十年，我盖楼让他塌房"],
        cover="3：4.png",
        top_cut=0.0, bottom_cut=0.235,
        thumb_title="回乡建房报旧仇",
        thumb_sub="被欺压三十年，我盖楼让他疯癫",
    ),
}


def match_template(title: str) -> DramaMeta | None:
    """按剧名匹配内置模板（宽松匹配，容忍「（36集）」等后缀）。"""
    t = title.strip()
    for key, meta in DRAMA_TEMPLATES.items():
        if key in t or t in meta.title or key in meta.title:
            return meta
    return None


def generic_meta(title: str) -> DramaMeta:
    """无模板时生成通用文案，保证流程可用。"""
    return DramaMeta(
        title=title,
        genre="短剧 · 全集连播",
        yt_title=f"{title}｜全集连播一口气看完 #短剧",
        yt_title_en=f"{title} | Chinese Short Drama Full Series #Shorts",
        desc_cn=(
            f"《{title}》全集连播。\n"
            "剧情紧凑、爽点密集，建议一口气看完。"
        ),
        desc_en=f"{title} — full series, binge-watch in one go.",
        tags=[title, "短剧", "全集", "一口气看完", "chinese drama"],
        tags_en=["chinese short drama", "full series"],
    )
