#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
压缩档位定义（供合并流程直接调用）。

这里的档位与 tools/compress_videos.py 中的 PRESETS 保持一致，
但本模块是**库内定义**，不依赖 tools 目录，供 cli.py / gui.py / merger.py 使用。

设计意图：
    "合并即压缩" —— 在合并阶段一次性完成编码输出，
    避免"先合出大文件、再二次压缩"的画质损失与额外耗时。

实测数据（本机 GTX 1080 Ti + ffmpeg 7.1，素材为竖屏短剧）：
    - 同 CRF 下 HEVC 体积约为 H.264 的 60~66%
    - HEVC CRF30 可压到 H.264 的约 41%，SSIM 仍达 0.983
    - 压缩场景必须用软编（x264/x265）：NVENC 追求吞吐，码率压不下来
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompressPreset:
    """一个压缩档位的全部参数。"""

    name: str                    # 档位标识（CLI/GUI 用）
    label: str                   # 显示名
    codec: str                   # "x264" | "hevc"
    crf: int                     # x264 用 CRF；x265 用 CRF；NVENC 用 CQ
    audio_kbps: int
    max_height: int | None       # 超过则等比降分辨率（None = 不动）
    fps_cap: int | None          # 超过则降帧率（None = 不动）
    speed: str                   # 编码速度预设（x264: medium；x265: medium）
    desc: str

    @property
    def ext(self) -> str:
        return ".mp4"

    @property
    def vcodec_name(self) -> str:
        """FFmpeg 编码器名。"""
        return "libx265" if self.codec == "hevc" else "libx264"

    @property
    def short(self) -> str:
        """简短描述，用于文件名与日志。"""
        return f"{'HEVC' if self.codec == 'hevc' else 'H264'}-CRF{self.crf}"


# 档位表（与 tools/compress_videos.py 的 PRESETS 保持一致）
PRESETS: dict[str, CompressPreset] = {
    # 高画质：视觉无损，体积约减 40~60%
    "archive": CompressPreset(
        name="archive", label="高画质", codec="x264",
        crf=23, audio_kbps=128, max_height=None, fps_cap=30,
        speed="medium",
        desc="视觉无损，体积约减 40~60%",
    ),
    # 上传推荐：明显瘦身，画质依旧清晰
    "upload": CompressPreset(
        name="upload", label="上传推荐", codec="x264",
        crf=26, audio_kbps=96, max_height=None, fps_cap=30,
        speed="medium",
        desc="体积约减 70~80%，画质清晰（推荐）",
    ),
    # 轻量：进一步压，适合手机端观看
    "light": CompressPreset(
        name="light", label="轻量", codec="x264",
        crf=28, audio_kbps=64, max_height=1280, fps_cap=30,
        speed="fast",
        desc="720p 上限，体积约减 80%",
    ),
    # --- HEVC 系列（同画质体积再省约一半，老设备兼容性略差）---
    "hevc": CompressPreset(
        name="hevc", label="HEVC 高压缩", codec="hevc",
        crf=30, audio_kbps=96, max_height=None, fps_cap=30,
        speed="medium",
        desc="体积约为 H.264 的 40%，画质几乎无差异",
    ),
    "hevc-light": CompressPreset(
        name="hevc-light", label="HEVC 最小体积", codec="hevc",
        crf=30, audio_kbps=64, max_height=1280, fps_cap=30,
        speed="medium",
        desc="HEVC + 720p 上限，体积最小（约原片 5%）",
    ),
}

# CLI / GUI 里展示的档位名列表
PRESET_NAMES = list(PRESETS.keys())


def get_preset(name: str) -> CompressPreset:
    """按名取档位，未知名称抛 ValueError。"""
    if name not in PRESETS:
        valid = ", ".join(PRESETS)
        raise ValueError(f"未知压缩档位 '{name}'，可选：{valid}")
    return PRESETS[name]


def describe_all() -> str:
    """生成档位说明（多行文本）。"""
    lines = []
    for p in PRESETS.values():
        lines.append(f"  {p.name:<12} {p.label:<14} {p.desc}")
    return "\n".join(lines)
