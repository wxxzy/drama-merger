"""
图形界面（Tkinter，标准库自带，无需额外安装）。

设计目标：选目录 -> 看一眼探测结果 -> 点合并 -> 看进度。
短剧创作者通常不需要懂 ffmpeg 参数，所以界面上尽量少露技术细节，
但把关键决策（策略、性能预估）明确展示出来。
"""

from __future__ import annotations

import queue
import re
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .compress import PRESETS
from .ffmpeg_env import get_ffmpeg_env
from .merger import Encoder, MergeOptions, Strategy, execute_merge
from .probe import analyze_consistency, collect_video_files, probe_file
from .publish import (
    DRAMA_TEMPLATES, ep_num, fmt_ts, generate_all, generic_meta, match_template,
)

# 兼容旧名（gui 内部与外部工具曾用这两个名字）
_match_meta = match_template
_generic_meta = generic_meta


class MergeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("短剧合并工具 · drama-merger")
        root.geometry("900x760")
        root.minsize(800, 620)

        self.env = None
        self.files: list[Path] = []
        self.items = []
        self.report = None
        self.worker: threading.Thread | None = None
        self.msg_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._init_env()
        self._poll_queue()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """整体布局：上方标签页切换「合并 / 发布」，下方共用进度与日志。

        为什么用标签页：合并与发布是两个前后相继但操作独立的环节，
        同一屏堆叠控件会让窗口过高、关键按钮被挤出视野（踩过这个坑）。
        标签页把两者分开，各自内部空间充裕。
        """
        # 用 Notebook 承载两条流水线
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.tab_merge = ttk.Frame(self.nb)
        self.tab_publish = ttk.Frame(self.nb)
        self.nb.add(self.tab_merge, text="  ① 合并视频  ")
        self.nb.add(self.tab_publish, text="  ② 发布准备  ")

        self._build_merge_tab()
        self._build_publish_tab()
        self._build_bottom()

    # ------------------------------------------------------------------
    # 标签页①：合并
    # ------------------------------------------------------------------

    def _build_merge_tab(self) -> None:
        pad = {"padx": 8, "pady": 4}
        parent = self.tab_merge

        # --- 顶部：目录选择 ---
        top = ttk.LabelFrame(parent, text="1. 选择短剧目录")
        top.pack(fill="x", **pad)

        row = ttk.Frame(top)
        row.pack(fill="x", padx=8, pady=6)
        self.dir_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self.on_browse).pack(side="left", padx=4)
        ttk.Button(row, text="扫描并分析", command=self.on_scan).pack(side="left")

        opt = ttk.Frame(top)
        opt.pack(fill="x", padx=8, pady=(0, 6))
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="包含子目录", variable=self.recursive_var).pack(side="left")
        ttk.Label(opt, text="  排序方式：").pack(side="left")
        self.sort_var = tk.StringVar(value="文件名自然排序")
        ttk.Label(opt, text="文件名自然排序（第2集在 第10集 之前）",
                  foreground="#666").pack(side="left")

        # --- 中部：探测结果 ---
        mid = ttk.LabelFrame(parent, text="2. 片段清单与分析结果")
        mid.pack(fill="both", expand=True, **pad)

        cols = ("seq", "name", "duration", "video", "audio")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        for cid, title, width in (
            ("seq", "#", 44),
            ("name", "文件名", 300),
            ("duration", "时长", 82),
            ("video", "视频参数", 190),
            ("audio", "音频", 150),
        ):
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=width, anchor="w" if cid == "name" else "center")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y", pady=6, padx=(0, 8))
        self.tree.configure(yscrollcommand=sb.set)

        # --- 分析结论 ---
        ana = ttk.LabelFrame(parent, text="3. 合并策略")
        ana.pack(fill="x", **pad)
        self.analysis_var = tk.StringVar(value="请先选择目录并点击「扫描并分析」")
        ttk.Label(ana, textvariable=self.analysis_var, justify="left",
                  wraplength=830, foreground="#0a5").pack(anchor="w", padx=8, pady=6)

        # --- 选项 ---
        optf = ttk.LabelFrame(parent, text="4. 输出选项")
        optf.pack(fill="x", **pad)

        r1 = ttk.Frame(optf)
        r1.pack(fill="x", padx=8, pady=4)
        ttk.Label(r1, text="输出文件：").pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="另存为...", command=self.on_choose_output).pack(side="left", padx=4)

        r2 = ttk.Frame(optf)
        r2.pack(fill="x", padx=8, pady=4)
        ttk.Label(r2, text="编码器：").pack(side="left")
        self.encoder_var = tk.StringVar(value="auto")
        ttk.Combobox(r2, textvariable=self.encoder_var, width=12, state="readonly",
                     values=["auto", "nvenc", "x264"]).pack(side="left", padx=(0, 12))
        ttk.Label(r2, text="画质 CRF：").pack(side="left")
        self.crf_var = tk.IntVar(value=20)
        ttk.Spinbox(r2, from_=14, to=30, textvariable=self.crf_var, width=5).pack(side="left", padx=(0, 12))
        self.burn_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="烧入字幕（若存在）", variable=self.burn_var).pack(side="left")

        # --- 合并即压缩 ---
        rc = ttk.Frame(optf)
        rc.pack(fill="x", padx=8, pady=4)
        self.compress_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(rc, text="合并时直接压缩", variable=self.compress_var,
                        command=self._on_compress_toggle).pack(side="left")

        self.preset_var = tk.StringVar(value="upload")
        self.preset_box = ttk.Combobox(
            rc, textvariable=self.preset_var, width=16, state="disabled",
            values=list(PRESETS.keys()))
        self.preset_box.pack(side="left", padx=(8, 6))
        self.preset_box.bind("<<ComboboxSelected>>", lambda _e: self._update_preset_hint())

        self.preset_hint = tk.StringVar(value="")
        ttk.Label(rc, textvariable=self.preset_hint, foreground="#0a7d28"
                  ).pack(side="left", padx=(4, 0))

        r3 = ttk.Frame(optf)
        r3.pack(fill="x", padx=8, pady=(0, 4))
        self.keep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text="保留临时文件", variable=self.keep_var).pack(side="left")
        ttk.Label(r3, text="（排错用，正常不用勾）", foreground="#888").pack(side="left")

        # --- 主操作按钮 ---
        act = ttk.Frame(optf)
        act.pack(fill="x", padx=8, pady=(2, 8))
        self.start_btn = ttk.Button(act, text="▶  开始合并", command=self.on_merge, width=16)
        self.start_btn.pack(side="left")
        ttk.Button(act, text="打开输出目录", command=self.on_open_outdir).pack(side="left", padx=6)
        self.hint_var = tk.StringVar(value="选好选项后点左侧「开始合并」")
        ttk.Label(act, textvariable=self.hint_var, foreground="#888").pack(side="left", padx=10)

    # ------------------------------------------------------------------
    # 标签页②：发布准备
    # ------------------------------------------------------------------

    def _build_publish_tab(self) -> None:
        pad = {"padx": 8, "pady": 4}
        parent = self.tab_publish

        # --- 成片来源 ---
        src = ttk.LabelFrame(parent, text="1. 选择已合并的成片")
        src.pack(fill="x", **pad)

        row = ttk.Frame(src)
        row.pack(fill="x", padx=8, pady=6)
        self.pub_video_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.pub_video_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self.on_pub_browse).pack(side="left", padx=4)
        ttk.Button(row, text="用刚合并的成片",
                   command=self.on_pub_use_last).pack(side="left")

        hint = ttk.Frame(src)
        hint.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(hint, text="发布素材会依据成片时长与剧集信息生成",
                  foreground="#666").pack(side="left")

        # --- 剧名与封面 ---
        info = ttk.LabelFrame(parent, text="2. 剧名与封面")
        info.pack(fill="x", **pad)

        r1 = ttk.Frame(info)
        r1.pack(fill="x", padx=8, pady=4)
        ttk.Label(r1, text="剧名：").pack(side="left")
        self.pub_title_var = tk.StringVar()
        e = ttk.Entry(r1, textvariable=self.pub_title_var)
        e.pack(side="left", fill="x", expand=True, padx=(0, 8))
        e.bind("<FocusOut>", lambda _e: self._pub_on_title_change())
        ttk.Label(r1, text="（用于查找内置文案模板）", foreground="#888").pack(side="left")

        r2 = ttk.Frame(info)
        r2.pack(fill="x", padx=8, pady=4)
        ttk.Label(r2, text="封面图：").pack(side="left")
        self.pub_cover_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.pub_cover_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="选择...", command=self.on_pub_choose_cover).pack(side="left", padx=4)

        r3 = ttk.Frame(info)
        r3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(r3, text="输出目录：").pack(side="left")
        self.pub_out_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.pub_out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r3, text="选择...", command=self.on_pub_choose_out).pack(side="left", padx=4)

        # --- 生成内容说明 ---
        what = ttk.LabelFrame(parent, text="3. 将生成的内容")
        what.pack(fill="x", **pad)
        self.pub_check_vars: dict[str, tk.BooleanVar] = {}
        items = [
            ("chapters", "章节时间轴", "按各集真实时长累加，可直接粘进简介让观众跳集"),
            ("desc", "发布文案", "标题 / 简介（含章节）/ 中英标签，逐块可复制"),
            ("thumb", "缩略图 1280×720", "竖屏封面转横屏，自动去掉封面自带文字与水印"),
        ]
        for key, label, desc in items:
            fr = ttk.Frame(what)
            fr.pack(fill="x", padx=8, pady=2)
            v = tk.BooleanVar(value=True)
            self.pub_check_vars[key] = v
            ttk.Checkbutton(fr, text=label, variable=v, width=18).pack(side="left")
            ttk.Label(fr, text=desc, foreground="#666").pack(side="left")

        # --- 操作 ---
        act = ttk.Frame(parent)
        act.pack(fill="x", padx=8, pady=(8, 4))
        self.pub_btn = ttk.Button(act, text="▶  生成发布素材",
                                  command=self.on_generate_publish, width=18)
        self.pub_btn.pack(side="left")
        ttk.Button(act, text="打开输出目录",
                   command=self.on_pub_open_outdir).pack(side="left", padx=6)
        self.pub_hint_var = tk.StringVar(value="")
        ttk.Label(act, textvariable=self.pub_hint_var, foreground="#0a7d28"
                  ).pack(side="left", padx=10)

    # ------------------------------------------------------------------
    # 共用底部：进度与日志
    # ------------------------------------------------------------------

    def _build_bottom(self) -> None:
        pad = {"padx": 8, "pady": 4}

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)

        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=8, pady=(4, 2))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, foreground="#333").pack(anchor="w", padx=8)

        logf = ttk.LabelFrame(self.root, text="运行日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=6, wrap="word", state="disabled",
                           background="#fbfbfb", relief="flat", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=6)

    # ------------------------------------------------------------------
    # 环境初始化（放后台线程，避免卡界面）
    # ------------------------------------------------------------------

    def _init_env(self) -> None:
        def work() -> None:
            try:
                self.env = get_ffmpeg_env()
                if self.env.nvenc_available:
                    self._log(f"ffmpeg 就绪：{Path(self.env.ffmpeg).name}，硬件编码已启用（速度更快）")
                else:
                    self._log(f"ffmpeg 就绪：{Path(self.env.ffmpeg).name}，将使用 CPU 编码")
            except Exception as exc:  # noqa: BLE001
                self._log(f"ffmpeg 环境探测失败：{exc}")
                self._set_status("ffmpeg 不可用")

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def on_browse(self) -> None:
        d = filedialog.askdirectory(title="选择短剧分集所在目录")
        if d:
            self.dir_var.set(d)

    def on_choose_output(self) -> None:
        p = filedialog.asksaveasfilename(
            title="选择输出文件", defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4"), ("所有文件", "*.*")],
        )
        if p:
            self.out_var.set(p)

    def on_open_outdir(self) -> None:
        out = self.out_var.get().strip()
        target = Path(out).parent if out else None
        if target and target.is_dir():
            try:
                import os
                os.startfile(str(target))  # noqa: S606 (Windows)
            except Exception:  # noqa: BLE001
                messagebox.showinfo("提示", f"输出目录：{target}")
        else:
            messagebox.showinfo("提示", "请先选择输出文件")

    def _on_compress_toggle(self) -> None:
        """合并时压缩开关：联动档位下拉框与提示。"""
        on = self.compress_var.get()
        self.preset_box.configure(state="readonly" if on else "disabled")
        self._update_preset_hint()

    def _update_preset_hint(self) -> None:
        if not self.compress_var.get():
            self.preset_hint.set("")
            return
        p = PRESETS.get(self.preset_var.get())
        self.preset_hint.set(f"（{p.desc}）" if p else "")

    def _current_preset(self):
        """当前选中的压缩档位（未启用则返回 None）。"""
        if not self.compress_var.get():
            return None
        return PRESETS.get(self.preset_var.get())

    def on_scan(self) -> None:
        directory = Path(self.dir_var.get().strip()).expanduser()
        if not directory.is_dir():
            messagebox.showwarning("提示", "请选择一个有效的目录")
            return
        if self.env is None:
            messagebox.showinfo("请稍候", "正在初始化 ffmpeg 环境，请稍后再试")
            return

        self.tree.delete(*self.tree.get_children())
        self._set_status("正在扫描...")
        self._log(f"扫描目录：{directory}")

        def work() -> None:
            try:
                files = collect_video_files(directory, recursive=self.recursive_var.get())
                self.files = files
                if not files:
                    self._ui(lambda: self._set_status("目录中未发现视频文件"))
                    self._log("目录中未发现视频文件")
                    return
                items = []
                for i, f in enumerate(files, 1):
                    info = probe_file(f, self.env)
                    items.append(info)
                    self._ui(lambda n=i, t=len(files), nm=f.name:
                             self._set_status(f"正在探测 {n}/{t}：{nm}"))
                self.items = items
                report = analyze_consistency(items)
                self.report = report
                self._ui(lambda: self._render_scan_result(items, report))
            except Exception:  # noqa: BLE001
                self._log("扫描出错：\n" + traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _render_scan_result(self, items, report) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, info in enumerate(items, 1):
            v = info.video
            vt = f"{v.codec_name} {v.width}x{v.height} {v.fps:g}fps" if v else "无视频流"
            at = (f"{info.audios[0].codec_name} {info.audios[0].sample_rate}Hz "
                  f"{info.audios[0].channels}ch") if info.audios else "无音轨"
            dur = f"{info.duration:.1f}s"
            if info.probe_error:
                vt, at = f"探测失败: {info.probe_error[:24]}", "-"
            self.tree.insert("", "end", values=(i, info.name, dur, vt, at))

        total = sum(x.duration for x in items)
        h, m = int(total // 3600), int((total % 3600) // 60)
        lines = [f"共 {len(items)} 个片段，合并后约 {h}小时{m}分{total % 60:.0f}秒"]

        if report.is_uniform:
            lines.append("✓ 所有片段参数完全一致 → 采用【无损拼接】，秒级完成，画质零损失")
        else:
            lines.append(f"⚠ 发现 {len(report.inconsistencies)} 处参数差异：")
            for inc in report.inconsistencies[:5]:
                tag = "阻断" if inc.severity == "blocker" else "提示"
                lines.append(f"    · [{tag}] {inc.describe()}")
            if report.can_concat_lossless:
                lines.append("→ 采用【归一化拼接】：先统一参数再无损拼接，仅转码一次")
            else:
                lines.append("→ 采用【滤镜重编码】：兼容性最强，速度较慢")

        tv = report.target_video_params()
        lines.append(f"输出规格：{tv['width']}x{tv['height']}@{tv['fps']:g}fps")
        self.analysis_var.set("\n".join(lines))
        self._set_status(f"扫描完成，共 {len(items)} 个片段 —— 点左侧「▶ 开始合并」即可开始")
        self.hint_var.set("一切就绪，点这里开始 →")

        # 自动填输出路径
        if not self.out_var.get().strip():
            self.out_var.set(str(
                Path(self.dir_var.get()) .parent / f"{Path(self.dir_var.get()).name}_合并.mp4"
            ))

    def on_merge(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在合并中，请稍候")
            return
        if not self.items or self.report is None:
            messagebox.showwarning("提示", "请先点击「扫描并分析」")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("提示", "请指定输出文件路径")
            return

        output = Path(out).expanduser()
        if output.exists():
            if not messagebox.askyesno("确认", f"文件已存在，是否覆盖？\n{output}"):
                return

        options = MergeOptions(
            output=output,
            encoder=Encoder(self.encoder_var.get()),
            crf=int(self.crf_var.get()),
            keep_temp=self.keep_var.get(),
            compress=self._current_preset(),
            on_progress=lambda ph, r: self._ui(
                lambda: (self.progress.configure(value=r * 100),
                         self._set_status(f"{ph}  {r * 100:.0f}%"))
            ),
        )

        self.start_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self._log(f"开始合并 → {output}")
        if options.compress is not None:
            self._log(f"压缩档位：{options.compress.label}"
                      f"（{options.compress.desc}）")

        def work() -> None:
            try:
                result = execute_merge(self.items, self.report, options, self.env)
                src_total = sum(i.size for i in self.items)
                msg = (
                    f"完成！策略 {result.strategy.value}，编码器 {result.encoder_used}，"
                    f"时长 {result.duration:.1f}s，大小 {result.size / 1024 / 1024:.1f}MB，"
                    f"耗时 {result.elapsed:.1f}s"
                )
                self._log(msg)
                if options.compress is not None and src_total > 0:
                    saved = src_total - result.size
                    pct = result.size / src_total * 100
                    self._log(
                        f"  压缩：素材 {src_total / 1048576:.1f}MB -> "
                        f"成片 {result.size / 1048576:.1f}MB"
                        f"（{pct:.1f}%，省 {saved / 1048576:.1f}MB）"
                    )
                for w in result.warnings:
                    self._log(f"  注意：{w}")
                self._ui(lambda: self._set_status(f"合并完成 → {result.output}"))
                if self.burn_var.get():
                    self._burn(result.output)
                self._ui(lambda: messagebox.showinfo(
                    "完成", f"合并完成！\n\n{result.output}\n\n"
                            f"策略：{result.strategy.label}\n"
                            f"耗时：{result.elapsed:.1f} 秒"))
            except Exception as exc:  # noqa: BLE001
                self._log("合并失败：\n" + traceback.format_exc())
                self._ui(lambda: messagebox.showerror("失败", str(exc)[:600]))
                self._ui(lambda: self._set_status("合并失败"))
            finally:
                self._ui(lambda: self.start_btn.configure(state="normal"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------
    # 标签页②：发布准备 —— 事件处理
    # ------------------------------------------------------------------

    def on_pub_browse(self) -> None:
        """选择已合并的成片。"""
        p = filedialog.askopenfilename(
            title="选择已合并的成片",
            filetypes=[("MP4 视频", "*.mp4"), ("所有文件", "*.*")],
        )
        if p:
            self._pub_apply_video(Path(p))

    def on_pub_use_last(self) -> None:
        """取「合并」页刚产出的成片。"""
        out = self.out_var.get().strip() if hasattr(self, "out_var") else ""
        if out and Path(out).is_file():
            self._pub_apply_video(Path(out))
            return
        messagebox.showinfo("提示", "还没有合并好的成片，请先到「① 合并视频」页合并。")

    def _pub_apply_video(self, video: Path) -> None:
        """选中成片后，自动推断剧名/封面/输出目录。"""
        self.pub_video_var.set(str(video))
        # 剧名：去掉「（N集）」与后缀，作为模板匹配线索
        stem = video.stem
        title = re.sub(r"[（(]\s*\d+\s*集\s*[）)]", "", stem).strip()
        title = re.sub(r"_合并$|_merged$", "", title).strip()
        if not self.pub_title_var.get().strip():
            self.pub_title_var.set(title)
        # 封面：在成片同目录与素材目录找 png
        if not self.pub_cover_var.get().strip():
            found = self._pub_find_cover(video.parent, title)
            if found:
                self.pub_cover_var.set(str(found))
        # 输出目录：成片同级的 youtube 子目录
        if not self.pub_out_var.get().strip():
            self.pub_out_var.set(str(video.parent / "youtube"))
        self._pub_on_title_change()

    @staticmethod
    def _pub_find_cover(folder: Path, title: str) -> Path | None:
        """在目录里找一个可用的封面图。"""
        for pat in ("*.png", "*.jpg", "*.jpeg"):
            for p in sorted(folder.glob(pat)):
                return p
        # 素材目录常与成片不同，退而查找标题相近的
        for pat in ("*.png",):
            for p in sorted(folder.glob(pat)):
                if title and title[:4] in p.stem:
                    return p
        return None

    def on_pub_choose_cover(self) -> None:
        p = filedialog.askopenfilename(
            title="选择封面图",
            filetypes=[("图片", "*.png *.jpg *.jpeg"), ("所有文件", "*.*")],
        )
        if p:
            self.pub_cover_var.set(p)

    def on_pub_choose_out(self) -> None:
        p = filedialog.askdirectory(title="选择发布素材输出目录")
        if p:
            self.pub_out_var.set(p)

    def _pub_on_title_change(self) -> None:
        """剧名变化时，提示是否有内置文案模板。"""
        title = self.pub_title_var.get().strip()
        if not title:
            self.pub_hint_var.set("")
            return
        hit = _match_meta(title)
        if hit:
            self.pub_hint_var.set(f"✓ 已匹配内置文案模板：{hit.title}")
        else:
            self.pub_hint_var.set("（无内置模板，将生成通用文案，可自行编辑）")

    def on_pub_open_outdir(self) -> None:
        out = self.pub_out_var.get().strip()
        if out and Path(out).is_dir():
            try:
                import os
                os.startfile(out)  # noqa: S606
            except Exception:  # noqa: BLE001
                messagebox.showinfo("提示", f"输出目录：{out}")
        else:
            messagebox.showinfo("提示", "请先选择输出目录")

    def on_generate_publish(self) -> None:
        """生成发布素材。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在处理中，请稍候")
            return

        video = self.pub_video_var.get().strip()
        if not video or not Path(video).is_file():
            messagebox.showwarning("提示", "请先选择已合并的成片")
            return
        title = self.pub_title_var.get().strip()
        if not title:
            messagebox.showwarning("提示", "请填写剧名")
            return
        out_dir = self.pub_out_var.get().strip()
        if not out_dir:
            messagebox.showwarning("提示", "请选择输出目录")
            return
        if self.env is None:
            messagebox.showinfo("请稍候", "正在初始化 ffmpeg 环境，请稍后再试")
            return

        cover = self.pub_cover_var.get().strip()
        want = {k: v.get() for k, v in self.pub_check_vars.items()}

        self.pub_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self._log(f"生成发布素材 → {out_dir}")

        def work() -> None:
            try:
                # 1) 由成片时长与素材推章节
                episodes = self._pub_collect_episodes(video, title)
                if not episodes:
                    self._ui(lambda: messagebox.showwarning(
                        "提示", "无法确定各集时长。请确认成片目录或素材目录里"
                                "仍有分集文件（章节与缩略图需要它）。"))
                    return

                meta = _match_meta(title) or _generic_meta(title)

                # 封面：用户显式选的优先；否则用模板里的文件名在同目录找
                cover_dir = None
                want_thumb = want.get("thumb", True)
                if want_thumb:
                    cov = cover.strip()
                    if cov and Path(cov).is_file():
                        # 把 meta.cover 指到用户选的文件名，目录用其父目录
                        meta.cover = Path(cov).name
                        cover_dir = Path(cov).parent
                    elif meta.cover:
                        # 模板给了文件名，去成片目录/素材目录找
                        for cand in (Path(video).parent,
                                     Path(self.dir_var.get() or ".")):
                            if (cand / meta.cover).is_file():
                                cover_dir = cand
                                break

                res = generate_all(
                    meta,
                    episodes,
                    Path(out_dir),
                    cover_dir=cover_dir,
                    progress=self._log,
                )

                # 按勾选清理未选中的产物
                if not want.get("chapters", True) and res.chapters_path:
                    res.chapters_path.unlink(missing_ok=True)
                    res.chapters_path = None
                if not want.get("desc", True) and res.txt_path:
                    res.txt_path.unlink(missing_ok=True)
                    res.txt_path = None
                if not want.get("thumb", True) and res.thumb_path:
                    res.thumb_path.unlink(missing_ok=True)
                    res.thumb_path = None

                msgs = [f"共 {res.count} 集 · 总时长 {fmt_ts(res.total)}"]
                if res.chapters_path:
                    msgs.append(f"章节时间轴：{res.chapters_path.name}")
                if res.txt_path:
                    msgs.append(f"发布文案：{res.txt_path.name}")
                if res.thumb_path:
                    msgs.append(f"缩略图：{res.thumb_path.name}")
                for w in res.warnings:
                    msgs.append(f"注意：{w}")

                self._log("生成完成：\n  " + "\n  ".join(msgs))
                self._ui(lambda: self._set_status(
                    f"发布素材已生成 → {res.out_dir}"))
                self._ui(lambda: messagebox.showinfo(
                    "完成", "发布素材已生成！\n\n" + "\n".join(msgs)
                    + f"\n\n输出目录：{res.out_dir}"))
            except Exception as exc:  # noqa: BLE001
                self._log("生成失败：\n" + traceback.format_exc())
                self._ui(lambda: messagebox.showerror("失败", str(exc)[:600]))
            finally:
                self._ui(lambda: self.pub_btn.configure(state="normal"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _pub_collect_episodes(self, video: str, title: str) -> list[tuple[int, float]]:
        """取 [(集号, 时长)]。

        优先用素材目录的分集文件（最准，能给出逐集时长）；
        退而用成片按集数均分（够用但章节会有微小偏差）。
        """
        from .probe import collect_video_files, probe_file

        # 1) 素材目录：与成片同级的剧场目录，或「合并」页选过的目录
        candidates: list[Path] = []
        d = self.dir_var.get().strip() if hasattr(self, "dir_var") else ""
        if d and Path(d).is_dir():
            candidates.append(Path(d))
        vp = Path(video)
        if vp.parent.is_dir():
            candidates.append(vp.parent)

        for cand in candidates:
            files = collect_video_files(cand)
            if not files:
                continue
            try:
                numbered = []
                for f in files:
                    n = ep_num(f.name)
                    if n is not None:
                        numbered.append((n, f))
                if len(numbered) < 2:
                    continue
                numbered.sort(key=lambda x: x[0])
                return [(n, probe_file(f, self.env).duration) for n, f in numbered]
            except Exception:  # noqa: BLE001
                continue

        # 2) 兜底：按成片时长均分（集数未知则按 36 估算不合适，直接放弃）
        return []

    def _burn(self, video: Path) -> None:
        """烧入字幕。"""
        from .subtitles import (
            EpisodeSubtitle, burn_subtitles, find_subtitles_for, merge_subtitles,
        )
        from .ffmpeg_env import _run

        episodes: list[EpisodeSubtitle] = []
        offset = 0.0
        for info in self.items:
            subs = find_subtitles_for(info.path)
            if subs:
                episodes.append(EpisodeSubtitle(info.path, subs, offset=offset))
            offset += info.duration
        if not episodes:
            self._log("未找到配套字幕，跳过烧入")
            return

        srt_out = video.with_suffix(".srt")
        sub_path, text = merge_subtitles(episodes, srt_out)
        if not sub_path:
            self._log(f"字幕合并失败：{text}")
            return
        self._log(f"外挂字幕已生成：{sub_path}")
        burned = video.with_name(video.stem + "_硬字幕" + video.suffix)
        self._set_status("正在烧入字幕...")
        cmd = burn_subtitles(video, sub_path, burned, self.env)
        code, out, err = _run(cmd, timeout=14400)
        if code == 0:
            self._log(f"硬字幕视频：{burned}")
        else:
            self._log(f"字幕烧入失败：{err.strip()[-300:] if err.strip() else '未知错误'}")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _ui(self, fn) -> None:
        """把回调丢到主线程执行。"""
        self.msg_queue.put(fn)

    def _poll_queue(self) -> None:
        try:
            while True:
                fn = self.msg_queue.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _log(self, text: str) -> None:
        def do() -> None:
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self._ui(do)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)


def launch() -> None:
    root = tk.Tk()
    try:
        # 高 DPI 适配
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    MergeApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch()
