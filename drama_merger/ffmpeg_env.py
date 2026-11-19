"""
FFmpeg 环境发现与能力探测。

设计要点（基于本机实测结论）：
本机同时存在两个 ffmpeg，且 NVENC 支持情况完全相反：
  - ffmpeg 7.1 (imageio_ffmpeg 自带)  -> NVENC 可用（实测编码成功）
  - ffmpeg 8.1.2 (winget full_build)  -> NVENC 被驱动版本拒绝
    （报错：Driver does not support the required nvenc API version.
      Required: 13.1 Found: 12.2 / 需要 610.00+ 驱动，当前 560.94）

因此绝不能硬编码"哪个 ffmpeg 好"或"能不能用硬件编码"，
必须在运行时探测：候选 ffmpeg 逐个尝试验证，择优选用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# 创建子进程时不弹出黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """执行命令，返回 (returncode, stdout, stderr)。永不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        # ffmpeg 的输出在 Windows 下可能是本地编码，容错解码
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        return proc.returncode, out, err
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"timeout after {timeout}s: {' '.join(cmd[:3])}"
    except Exception as exc:  # noqa: BLE001
        return -3, "", f"{type(exc).__name__}: {exc}"


def _quote_win_path(p: str) -> str:
    """Windows 路径统一为正斜杠，避免 ffmpeg 转义地狱。"""
    return p.replace("\\", "/")


# ---------------------------------------------------------------------------
# 候选 ffmpeg 搜索
# ---------------------------------------------------------------------------

def _candidate_ffmpeg_paths() -> list[Path]:
    """按优先级列出可能的 ffmpeg 可执行文件位置。"""
    cands: list[Path] = []

    # 1) 环境变量显式指定，最高优先级
    for env_key in ("DRAMA_MERGER_FFMPEG", "FFMPEG_BINARY", "FFMPEG"):
        val = os.environ.get(env_key)
        if val:
            cands.append(Path(val))

    # 2) imageio_ffmpeg 自带的（本机实测 NVENC 可用，优先）
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            cands.append(Path(exe))
    except Exception:  # noqa: BLE001
        pass

    # 3) PATH 中的 ffmpeg
    which = shutil.which("ffmpeg")
    if which:
        cands.append(Path(which))

    # 4) Python 脚本目录旁
    cands.append(Path(sys.executable).parent / ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg"))

    # 5) WinGet 安装的 Gyan build（本机存在，但 NVENC 被驱动拒绝，仍作为软件编码候选）
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            winget_pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
            if winget_pkgs.is_dir():
                for exe in winget_pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
                    cands.append(exe)

    # 去重且保持顺序，过滤掉不存在的
    seen: set[str] = set()
    result: list[Path] = []
    for c in cands:
        try:
            key = str(c.resolve()).lower()
        except Exception:  # noqa: BLE001
            key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        if c.is_file():
            result.append(c)
    return result


def _ffprobe_for(ffmpeg_path: Path) -> Path | None:
    """寻找与给定 ffmpeg 配套的 ffprobe。"""
    # 同目录同名
    sibling = ffmpeg_path.with_name("ffprobe.exe" if IS_WINDOWS else "ffprobe")
    if sibling.is_file():
        return sibling
    # imageio_ffmpeg 目录下通常只有 ffmpeg，退回 PATH 里的 ffprobe
    which = shutil.which("ffprobe")
    if which:
        return Path(which)
    return None


# ---------------------------------------------------------------------------
# 能力探测结果
# ---------------------------------------------------------------------------

@dataclass
class FfmpegCapabilities:
    """单个 ffmpeg 可执行文件的能力画像。"""

    path: Path
    version: str = ""
    version_tuple: tuple[int, ...] = ()
    build: str = ""

    encoders: set[str] = field(default_factory=set)
    hwaccels: set[str] = field(default_factory=set)
    filters: set[str] = field(default_factory=set)

    # 实测结论（不是"编译时包含"，而是"真的能跑通"）
    nvenc_verified: bool = False
    nvenc_error: str = ""

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_filter(self, name: str) -> bool:
        return name in self.filters

    @property
    def ffprobe(self) -> Path | None:
        return _ffprobe_for(self.path)

    def describe(self) -> str:
        lines = [
            f"ffmpeg: {self.path}",
            f"version: {self.version}",
            f"build: {self.build or '(unknown)'}",
        ]
        hw = sorted(e for e in self.encoders if e.endswith(("_nvenc", "_qsv", "_amf", "_vaapi")))
        lines.append(f"硬件编码器(编译时): {', '.join(hw) if hw else '无'}")
        lines.append(
            f"NVENC 实测: {'可用' if self.nvenc_verified else '不可用'}"
            + (f"  <- {self.nvenc_error}" if self.nvenc_error else "")
        )
        return "\n".join(lines)


def _probe_one(ffmpeg_path: Path, verify_nvenc: bool = True) -> FfmpegCapabilities:
    """探测单个 ffmpeg 的能力。"""
    cap = FfmpegCapabilities(path=ffmpeg_path)

    code, out, err = _run([str(ffmpeg_path), "-hide_banner", "-version"], timeout=20)
    text = out + err
    first_line = text.splitlines()[0] if text else ""
    cap.version = first_line
    # 解析 "ffmpeg version 7.1-essentials_build-..."
    for token in first_line.split():
        if token[:1].isdigit():
            parts = []
            for chunk in token.split("-")[0].split("."):
                if chunk.isdigit():
                    parts.append(int(chunk))
                else:
                    break
            cap.version_tuple = tuple(parts)
            break
    # build 标识
    if "Copyright" in text:
        for line in text.splitlines()[:3]:
            if "Copyright" in line:
                cap.build = line.strip()

    # 编码器列表
    code, out, err = _run([str(ffmpeg_path), "-hide_banner", "-encoders"], timeout=30)
    for line in (out + err).splitlines():
        line = line.strip()
        # 格式: " V....D h264_nvenc   NVIDIA NVENC H.264 encoder (codec h264)"
        if len(line) > 8 and line[0] in "VAS" and "." in line[:7]:
            parts = line.split()
            if len(parts) >= 2 and parts[1].replace("_", "").isalnum():
                cap.encoders.add(parts[1])

    # 硬件加速方式
    code, out, err = _run([str(ffmpeg_path), "-hide_banner", "-hwaccels"], timeout=20)
    for line in (out + err).splitlines():
        line = line.strip()
        if line and not line.lower().startswith("hardware"):
            cap.hwaccels.add(line)

    # 滤镜列表（只需要知道关键几个，全量解析太慢）
    code, out, err = _run([str(ffmpeg_path), "-hide_banner", "-filters"], timeout=30)
    for line in (out + err).splitlines():
        parts = line.split()
        # 格式: " ... concat          V->V       Concatenate audio and video streams"
        if len(parts) >= 3 and parts[0].startswith("."):
            cap.filters.add(parts[1])

    # NVENC 实测：编译时包含 != 运行时可用（本机的核心坑）
    if verify_nvenc and cap.has_encoder("h264_nvenc"):
        cap.nvenc_verified, cap.nvenc_error = _verify_nvenc(ffmpeg_path)

    return cap


def _verify_nvenc(ffmpeg_path: Path) -> tuple[bool, str]:
    """真正跑一次 NVENC 编码，验证运行时可用性。"""
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "drama_merger_nvenc_probe.mp4"
    cmd = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=1",
        "-c:v", "h264_nvenc", "-preset", "p4", "-f", "mp4",
        "-y", str(tmp),
    ]
    code, out, err = _run(cmd, timeout=60)
    ok = code == 0 and tmp.is_file() and tmp.stat().st_size > 0
    try:
        tmp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    if ok:
        return True, ""
    # 提取关键错误行
    msg = ""
    for line in (err or out).splitlines():
        low = line.lower()
        if "nvenc" in low or "driver" in low or "cuda" in low:
            msg = line.strip()
            break
    return False, msg or "NVENC 编码测试失败"


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

class FfmpegEnv:
    """管理本机 ffmpeg 环境，自动选择最佳可执行文件。"""

    def __init__(self) -> None:
        self.candidates: list[FfmpegCapabilities] = []
        self.chosen: FfmpegCapabilities | None = None

    @classmethod
    def discover(cls, verify_nvenc: bool = True, verbose: bool = False) -> "FfmpegEnv":
        env = cls()
        paths = _candidate_ffmpeg_paths()
        if not paths:
            raise RuntimeError(
                "未找到 ffmpeg。请安装 ffmpeg 并加入 PATH，"
                "或设置环境变量 DRAMA_MERGER_FFMPEG 指向 ffmpeg 可执行文件。"
            )
        for p in paths:
            cap = _probe_one(p, verify_nvenc=verify_nvenc)
            if cap.version_tuple:
                env.candidates.append(cap)
                if verbose:
                    print(f"[探测] {p} -> {cap.version.split('Copyright')[0].strip()}")

        if not env.candidates:
            raise RuntimeError(f"找到 ffmpeg 但无法执行：{[str(p) for p in paths]}")

        env.chosen = env._pick_best()
        return env

    def _pick_best(self) -> FfmpegCapabilities:
        """择优：优先 NVENC 实测可用，其次版本更高。"""

        def score(cap: FfmpegCapabilities) -> tuple:
            return (
                1 if cap.nvenc_verified else 0,       # 硬件编码可用性权重最高
                1 if cap.ffprobe else 0,              # 有配套 ffprobe
                cap.version_tuple,                    # 版本新旧兜底
            )

        return max(self.candidates, key=score)

    @property
    def ffmpeg(self) -> str:
        assert self.chosen
        return str(self.chosen.path)

    @property
    def ffprobe(self) -> str | None:
        assert self.chosen
        p = self.chosen.ffprobe
        return str(p) if p else None

    @property
    def nvenc_available(self) -> bool:
        return bool(self.chosen and self.chosen.nvenc_verified)

    def report(self) -> str:
        lines = ["=" * 62, "FFmpeg 环境探测报告", "=" * 62]
        for i, cap in enumerate(self.candidates, 1):
            mark = " <== 已选用" if cap is self.chosen else ""
            lines.append(f"\n[{i}] {cap.describe()}{mark}")
        lines.append("\n" + "-" * 62)
        lines.append(f"选用: {self.ffmpeg}")
        lines.append(f"ffprobe: {self.ffprobe or '(未找到，将退化用 ffmpeg 解析)'}")
        lines.append(f"硬件编码加速: {'启用' if self.nvenc_available else '不可用，将使用 CPU 编码'}")
        lines.append("=" * 62)
        return "\n".join(lines)


_cached_env: FfmpegEnv | None = None


def get_ffmpeg_env(refresh: bool = False, verbose: bool = False) -> FfmpegEnv:
    """获取（并缓存）ffmpeg 环境。"""
    global _cached_env
    if _cached_env is None or refresh:
        _cached_env = FfmpegEnv.discover(verbose=verbose)
    return _cached_env


if __name__ == "__main__":
    print(get_ffmpeg_env(refresh=True, verbose=True).report())
