#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成 YouTube 缩略图（1280x720）。

短剧素材是竖屏（9:16），YouTube 缩略图是横屏（16:9）。
做法：取封面主视觉区域放大铺满，两侧用同图模糊填充（避免拉伸变形），
底部压深色渐变条，叠加大号标题——这是短剧类频道的常见做法，
手机上信息流里辨识度高。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
# 封面源目录：优先用工作区内的 _cover_src（工具链自包含），
# 找不到时回退到素材目录
COVER_DIR = ROOT / "_cover_src"
SRC_FALLBACK = Path(r"C:\baidunetdiskdownload\回乡建房报旧仇，恶霸楼塌人疯癫")
OUT_DIR = ROOT / "youtube" / "缩略图"

W, H = 1280, 720
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"

# (源封面, 输出名, 主标题, 副标题/卖点, 顶部裁剪比例, 底部裁剪比例)
# - 顶部裁剪：封面顶部自带大号剧名的，按比例裁掉，避免与左侧标题重复
# - 底部裁剪：封面底部自带剧名/水印的，按比例裁掉
JOBS = [
    ("3：4.png", "01_回乡建房报旧仇.jpg", "回乡建房报旧仇",
     "被欺压三十年，我盖楼让他疯癫", 0.0, 0.235),
]


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str,
             max_width: int, start_size: int = 96) -> ImageFont.FreeTypeFont:
    """自动缩小字号直到文字宽度适配。"""
    size = start_size
    while size > 24:
        f = load_font(font_path, size)
        bbox = draw.textbbox((0, 0), text, font=f)
        if bbox[2] - bbox[0] <= max_width:
            return f
        size -= 4
    return load_font(font_path, 24)


def text_with_stroke(draw: ImageDraw.ImageDraw, xy, text, font,
                     fill=(255, 255, 255), stroke=(0, 0, 0), width=6):
    draw.text(xy, text, font=font, fill=fill,
              stroke_width=width, stroke_fill=stroke)


def trim_border(img: Image.Image, px: int = 14) -> Image.Image:
    """裁掉封面自带的细边框/描边。

    实测部分封面（如「造加特林」）四周带 1~2px 的彩色描边，
    直接贴到缩略图上会露出一条突兀的彩线，故先裁掉。
    """
    w, h = img.size
    px = max(2, min(px, w // 20, h // 20))
    return img.crop((px, px, w - px, h - px))


def trim_top_title(img: Image.Image, ratio: float = 0.0) -> Image.Image:
    """裁掉封面顶部自带的剧名文字区。

    多数封面顶部有大号剧名，而缩略图左侧已重排了标题，
    重叠两处标题会显得杂乱；故按比例裁掉顶部。
    """
    if ratio <= 0:
        return img
    w, h = img.size
    cut = int(h * ratio)
    if h - cut < h * 0.5:      # 保险：最多裁一半
        cut = int(h * 0.5)
    return img.crop((0, cut, w, h))


def trim_watermark(img: Image.Image, ratio: float = 0.0) -> Image.Image:
    """裁掉封面右下角的生成水印（如「豆包AI生成」）。

    实测本批封面右下角普遍带有生成工具水印，出现在缩略图上会显得不专业。
    水印高度约占封面全高的 2%~4%，按比例裁掉底部即可，不影响画面主体。
    """
    if ratio <= 0:
        return img
    w, h = img.size
    cut = int(h * ratio)
    if h - cut < h * 0.5:      # 保险：最多裁一半
        cut = int(h * 0.5)
    return img.crop((0, 0, w, h - cut))


def make_thumbnail(src: Path, dst: Path, title: str, subtitle: str,
                   top_cut: float = 0.0, bottom_cut: float = 0.04) -> None:
    base = trim_border(Image.open(src).convert("RGB"))
    base = trim_top_title(base, top_cut)
    base = trim_watermark(base, bottom_cut)
    bw, bh = base.size

    BAR_H = 56                      # 底部信息条高度
    usable_h = H - BAR_H

    # --- 背景：整图放大裁切铺满 + 高斯模糊 + 压暗 ---
    scale = max(W / bw, H / bh)
    bg = base.resize((int(bw * scale), int(bh * scale)), Image.LANCZOS)
    left = (bg.width - W) // 2
    top = (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H))
    bg = bg.filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)
    canvas = bg

    # --- 前景：保留封面构图，按可用高度贴右侧，避开底部信息条 ---
    fg_h = int(usable_h * 0.97)
    fg_w = int(bw * (fg_h / bh))
    fg = base.resize((fg_w, fg_h), Image.LANCZOS)
    fg_x = W - fg_w - 26
    fg_y = (usable_h - fg_h) // 2
    canvas.paste(fg, (fg_x, fg_y))

    draw = ImageDraw.Draw(canvas, "RGBA")

    # --- 左侧暗色渐变，保证文字可读 ---
    grad_w = int(W * 0.58)
    for x in range(grad_w):
        alpha = int(215 * (1 - x / grad_w) ** 0.85)
        draw.line([(x, 0), (x, H)], fill=(8, 10, 16, alpha))

    # --- 品牌角标 ---
    tag_font = load_font(FONT_BOLD, 30)
    draw.rounded_rectangle([48, 48, 48 + 168, 48 + 52], radius=10,
                           fill=(220, 38, 38, 235))
    draw.text((48 + 22, 48 + 10), "精选短剧", font=tag_font, fill=(255, 255, 255))

    # --- 主标题 ---
    t_font = fit_text(draw, title, FONT_BOLD, int(W * 0.52), 92)
    tb = draw.textbbox((0, 0), title, font=t_font)
    th = tb[3] - tb[1]
    # 副标题与主标题一起在可用区域内垂直居中
    block_h = th + (46 + 44 if subtitle else 0)
    ty = (usable_h - block_h) // 2 + 20
    text_with_stroke(draw, (56, ty), title, t_font,
                     fill=(255, 255, 255), stroke=(0, 0, 0), width=8)

    # --- 副标题 ---
    if subtitle:
        s_font = fit_text(draw, subtitle, FONT_REG, int(W * 0.50), 44)
        # 文字用不带描边的实色绘制，描边叠加会产生重影
        draw.text((58, ty + th + 46), subtitle, font=s_font,
                  fill=(255, 214, 102))

    # --- 底部信息条 ---
    bar_font = load_font(FONT_REG, 28)
    draw.rectangle([0, H - BAR_H, W, H], fill=(0, 0, 0, 200))
    draw.text((48, H - BAR_H + 12), "全集连播 · 一口气看完", font=bar_font,
              fill=(235, 235, 235))

    canvas.save(dst, quality=92, subsampling=0)
    print(f"  [OK] {dst.name}  {canvas.size[0]}x{canvas.size[1]}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 优先用工具内的 _cover_src；没有则回退到素材目录
    cover_dir = COVER_DIR
    if not cover_dir.is_dir() or not any(cover_dir.glob("*.png")):
        cover_dir = SRC_FALLBACK
    print(f"封面源目录: {cover_dir}")

    missing = []
    for job in JOBS:
        src_name, out_name, title, subtitle, top_cut = job[:5]
        bottom_cut = job[5] if len(job) > 5 else 0.04
        src = cover_dir / src_name
        if not src.is_file():
            missing.append(src_name)
            print(f"  [跳过] 找不到 {src_name}")
            continue
        make_thumbnail(src, OUT_DIR / out_name, title, subtitle, top_cut, bottom_cut)
    if missing:
        print(f"\n缺失 {len(missing)} 个封面：{missing}")
    print(f"\n输出目录：{OUT_DIR}")


if __name__ == "__main__":
    main()
