# -*- mode: python ; coding: utf-8 -*-
"""
短剧合并工具 · PyInstaller 打包配置

产出（onedir，免安装）：
    dist/短剧合并工具/
        短剧合并工具.exe            ← 双击启动图形界面
        短剧合并工具-命令行.exe      ← 拖目录/命令行使用
        _internal/                  ← 运行时与 ffmpeg（两个 exe 共享）

设计要点：
1. 两个 exe 共享一个 _internal 目录，ffmpeg 只存一份（83MB），不翻倍。
2. 显式排除 imageio_ffmpeg 包 —— 否则 PyInstaller 会把它的 83MB 二进制
   再打一份；我们改为手动放入 ffmpeg/ffmpeg.exe，并由入口脚本通过
   环境变量 DRAMA_MERGER_FFMPEG 定位（优先级最高）。
3. GUI 用 console=False（无黑框），CLI 用 console=True（否则子进程管道
   拿不到句柄，ffmpeg 进度回调会失效）。
"""

import os
import sys
from pathlib import Path

# ---- 定位要捆绑的 ffmpeg（取 imageio_ffmpeg 自带的那份，本机实测 NVENC 可用）----
def _find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).is_file():
            return str(p)
    except Exception:
        pass
    return None


_FFMPEG = _find_ffmpeg()
_datas = []
if _FFMPEG:
    _datas.append((_FFMPEG, "ffmpeg"))     # -> _internal/ffmpeg/ffmpeg.exe
    print(f"[spec] 捆绑 ffmpeg: {_FFMPEG}")
else:
    print("[spec] 警告：未找到 ffmpeg，打包后将依赖系统安装的 ffmpeg")

# 项目自带资源（封面源目录供缩略图脚本用；有则带，无则跳过）
_root = Path(SPECPATH) if "SPECPATH" in dir() else Path.cwd()
for extra in ("README.md",):
    f = _root / extra
    if f.is_file():
        _datas.append((str(f), "."))

block_cipher = None

# =========================== GUI 版 ===========================
a_gui = Analysis(
    ["gui_entry.py"],
    pathex=[str(_root)],
    binaries=[],
    datas=list(_datas),
    hiddenimports=[
        "tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk",
        "drama_merger.gui", "drama_merger.merger", "drama_merger.probe",
        "drama_merger.ffmpeg_env", "drama_merger.subtitles",
        "drama_merger.compress", "drama_merger.publish",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "imageio_ffmpeg",       # 见文件头说明 2
        "numpy", "PIL", "matplotlib", "scipy", "pandas",
        "PyQt5", "PySide2", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="短剧合并工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# =========================== 命令行版 ===========================
a_cli = Analysis(
    ["cli_entry.py"],
    pathex=[str(_root)],
    binaries=[],
    datas=[],                # ffmpeg 已在 GUI 那份里，COLLECT 时共享
    hiddenimports=[
        "drama_merger.merger", "drama_merger.probe",
        "drama_merger.ffmpeg_env", "drama_merger.subtitles",
        "drama_merger.compress", "drama_merger.publish",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "imageio_ffmpeg",
        "numpy", "matplotlib", "scipy", "pandas",
        "PyQt5", "PySide2", "pytest", "tkinter",
        # 注意：不能排除 PIL —— publish 生成缩略图需要它
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="短剧合并工具-命令行",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # CLI：必须保留控制台，子进程管道才正常
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# =========================== 合并收集 ===========================
coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="短剧合并工具",
)
