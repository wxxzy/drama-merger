"""
生成测试素材：模拟三类短剧场景。

场景1 uniform    三集参数完全一致（真实短剧最常见）-> 应走策略A 无损拼接
场景2 mixed      三集参数各异（不同分辨率/帧率/音轨缺失）-> 应走策略B/C
场景3 cn_names   中文文件名 + 自然排序验证（第2集应在第10集前）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FF = r"C:\Users\wangxingxiang\AppData\Roaming\Python\Python311\site-packages\imageio_ffmpeg\binaries\ffmpeg.exe"
ROOT = Path(__file__).resolve().parent

CREATE_NO_WINDOW = 0x08000000


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        print(f"  [FAIL] {' '.join(cmd[-6:])}\n    {err.strip().splitlines()[-1] if err.strip() else ''}")
        raise SystemExit(1)


def make_video(dst: Path, w: int, h: int, fps: int, dur: int, with_audio: bool,
               color_seed: int = 0) -> None:
    """造一段测试视频。画面带序号，便于人眼确认拼接顺序。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 用 testsrc2 造彩色测试图，叠加 drawtext 标注（若无字体则退化为纯图案）
    vf = f"testsrc2=size={w}x{h}:rate={fps}:duration={dur}"
    cmd = [FF, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", vf]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={300 + color_seed * 100}:duration={dur}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-g", str(fps * 2)]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100", "-shortest"]
    cmd += ["-y", str(dst)]
    run(cmd)


def make_subtitle(dst: Path, dur: int, tag: str) -> None:
    """造配套字幕（每条 2 秒）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    t = 0.5
    i = 1
    while t < dur - 0.5:
        lines.append(str(i))
        lines.append(f"00:00:{int(t):02d},000 --> 00:00:{int(t + 1.8):02d},000")
        lines.append(f"{tag} 第 {i} 句对白")
        lines.append("")
        t += 2.0
        i += 1
    dst.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    print("=" * 60)
    print("场景1：参数完全一致的短剧（应走策略A 无损拼接）")
    print("=" * 60)
    d1 = ROOT / "scene1_uniform" / "都市甜宠短剧"
    for i in range(1, 4):
        f = d1 / f"第{i}集.mp4"
        make_video(f, 768, 1344, 24, 6, with_audio=True, color_seed=i)
        make_subtitle(d1 / f"第{i}集.srt", 6, f"第{i}集")
        print(f"  [OK] {f.name}  768x1344@24fps 6s 带音轨")

    print()
    print("=" * 60)
    print("场景2：参数混杂的短剧（应走策略B/C 归一化或重编码）")
    print("=" * 60)
    d2 = ROOT / "scene2_mixed"
    make_video(d2 / "ep01.mp4", 768, 1344, 24, 5, with_audio=True, color_seed=1)
    print("  [OK] ep01.mp4  768x1344@24fps 5s 带音轨")
    make_video(d2 / "ep02.mp4", 1080, 1920, 30, 5, with_audio=True, color_seed=2)
    print("  [OK] ep02.mp4  1080x1920@30fps 5s 带音轨（分辨率帧率均不同）")
    make_video(d2 / "ep03.mp4", 720, 1280, 25, 5, with_audio=False, color_seed=3)
    print("  [OK] ep03.mp4  720x1280@25fps 5s 无音轨（触发补静音逻辑）")

    print()
    print("=" * 60)
    print("场景3：中文名 + 两位数集数（验证自然排序：第2集应在第10集前）")
    print("=" * 60)
    d3 = ROOT / "scene3_cn_names"
    for i in [1, 2, 10, 11]:
        f = d3 / f"《霸总的小娇妻》第{i}集.mp4"
        make_video(f, 768, 1344, 24, 3, with_audio=True, color_seed=i)
        print(f"  [OK] {f.name}")
    print("\n  排序验证（期望顺序：第1 第2 第10 第11）：")
    sys.path.insert(0, str(ROOT.parent))
    from drama_merger.probe import collect_video_files, _natural_key
    got = [p.name for p in collect_video_files(d3)]
    for g in got:
        print(f"    {g}")
    expect = ["《霸总的小娇妻》第1集.mp4", "《霸总的小娇妻》第2集.mp4",
              "《霸总的小娇妻》第10集.mp4", "《霸总的小娇妻》第11集.mp4"]
    if got == expect:
        print("  [PASS] 自然排序正确")
    else:
        print(f"  [CHECK] 实际顺序与期望不符\n    期望: {expect}\n    实际: {got}")

    print()
    print("=" * 60)
    print("场景4：边界情况")
    print("=" * 60)
    d4 = ROOT / "scene4_edge"
    make_video(d4 / "single.mp4", 768, 1344, 24, 3, with_audio=True)
    print("  [OK] single.mp4  仅一个片段（验证单文件兜底）")
    # 造一个损坏文件
    bad = d4 / "corrupted.mp4"
    bad.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 800)
    print("  [OK] corrupted.mp4  损坏文件（验证探测容错）")
    (d4 / "readme.txt").write_text("这不是视频", encoding="utf-8")
    print("  [OK] readme.txt  非视频文件（应被忽略）")

    print("\n测试素材生成完毕。")


if __name__ == "__main__":
    main()
