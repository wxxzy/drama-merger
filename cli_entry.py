#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyInstaller 打包入口（命令行）。

与 gui_entry 的关键差异：
- 后台子进程调用（subprocess）需要真实的标准句柄，因此**不能**用 windowed
  方式启动（那样 sys.stdout 为 None，会导致子进程管道异常）。
- 保留控制台，参数原样转发给 cli.py。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_ffmpeg() -> None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for cand in (
        base / "ffmpeg" / "ffmpeg.exe",
        base / "ffmpeg.exe",
        base / "binaries" / "ffmpeg.exe",
    ):
        if cand.is_file():
            os.environ.setdefault("DRAMA_MERGER_FFMPEG", str(cand))
            return
    # 兜底：PyInstaller 会保留源文件基本名（可能带版本号）
    for pattern in ("ffmpeg/ffmpeg*.exe", "ffmpeg*.exe", "binaries/ffmpeg*.exe"):
        for cand in sorted(base.glob(pattern)):
            if cand.is_file():
                os.environ.setdefault("DRAMA_MERGER_FFMPEG", str(cand))
                return


def main() -> int:
    _bootstrap_ffmpeg()
    # 不 chdir：命令行工具应遵守调用者的工作目录，
    # 否则用户给的相对路径（如 -o out.mp4）会被解析到 exe 所在目录，
    # 结果文件"凭空消失"在某处，很难排查。
    # 需要读随包资源时用 sys._MEIPASS，与工作目录无关。

    sys.path.insert(0, str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))))
    from cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
