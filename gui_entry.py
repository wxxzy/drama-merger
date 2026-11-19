#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyInstaller 打包入口（图形界面）。

打包后运行时，PyInstaller 会把依赖解压到临时目录（sys._MEIPASS），
需要把 imageio_ffmpeg 自带的 ffmpeg 一并带上并在运行时定位到它。
本模块负责在程序启动前把 ffmpeg 路径注入环境变量，
使主程序无需改动即可找到 ffmpeg。

用法:
    pyinstaller --clean build_gui.spec
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_ffmpeg() -> None:
    """把捆绑的 ffmpeg 暴露给主程序。

    打包后优先使用随包携带的 ffmpeg（保证开箱即用、不依赖系统安装）；
    若捆绑失败则回退到已有的探测链（imageio_ffmpeg / PATH）。

    注意：PyInstaller 会保留源文件的基本名，实际落地可能是
    ffmpeg-win-x86_64-v7.1.exe 而非 ffmpeg.exe，因此这里除固定名外
    还按模式兜底查找。
    """
    # 打包运行时，资源在 sys._MEIPASS；开发运行时在脚本同级
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    fixed = [
        base / "ffmpeg" / "ffmpeg.exe",
        base / "ffmpeg.exe",
        base / "binaries" / "ffmpeg.exe",
    ]
    for cand in fixed:
        if cand.is_file():
            os.environ.setdefault("DRAMA_MERGER_FFMPEG", str(cand))
            return

    # 兜底：按模式找（应对带版本号的文件名）
    for pattern in ("ffmpeg/ffmpeg*.exe", "ffmpeg*.exe", "binaries/ffmpeg*.exe"):
        for cand in sorted(base.glob(pattern)):
            if cand.is_file():
                os.environ.setdefault("DRAMA_MERGER_FFMPEG", str(cand))
                return


class _NullWriter:
    """windowed 模式下 sys.stdout/stderr 为 None，用它兜底。

    GUI 程序用 --windowed 打包后没有控制台，Python 会把 sys.stdout /
    sys.stderr 置为 None。底层模块里若有 print/写流动作会抛异常。
    这里替换成丢弃式写入器，保证任何输出都安全无害。
    """

    def write(self, *_args) -> int:
        return 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no fileno")


def _guard_streams() -> None:
    """给缺失的标准流补上空写入器，避免下游代码崩溃。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _NullWriter())


def main() -> int:
    _guard_streams()
    _bootstrap_ffmpeg()
    # 不 chdir：GUI 全部走文件对话框（绝对路径），
    # 而保留调用者工作目录便于用户输出到期望位置。
    from drama_merger.gui import launch

    launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
