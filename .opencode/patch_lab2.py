"""patch_lab2.py — 试验台交互重做：后端选择/动画/秒表/动态提示/净化渲染/收尾语义。"""
from pathlib import Path

p = Path("gui/widgets.py")
t = p.read_text(encoding="utf-8")

# 0) 顶部补 time 导入
if "\nimport time\n" not in t.split('import flet')[0]:
    t = t.replace("import datetime\nimport re\nimport threading\n",
                  "import datetime\nimport re\nimport threading\nimport time\n")

idx = t.index("class ExtractLabDialog:")
new_class = '''class ExtractLabDialog:
    """转换试验台：选一个文件，走与索引完全相同的提取管线（含缓存与 OCR 后端），
    就地预览产出质量。顶部控制行（含后端单次覆盖下拉）+ 活动指示（不确定进度条
    + 秒表）+ 整幅结果区（渲染/源码页签）。不做左右分栏——输入是二进制文件没有
    可展示的"原文"，整幅留给产出最省空间。

    中断与收尾语义：
    - 提取跑在 daemon 线程：关闭对话框/GUI 时线程随进程消亡，无守护进程残留；
    - 缓存写入是 tmp+os.replace 原子操作，中断至多留孤儿 tmp（启动清扫回收），
      绝不产生半截缓存；
    - 预览不写 meta/Chroma/终态，任何时刻中断都无需回滚。
    """

    def __init__(self, colors=DARK):
        self._cols = colors
        self._page = None
        self._picker = None
        self._file_path = None
        self._dlg = None
        self._busy = False
        self._stop = threading.Event()
        self._t0 = 0.0

    # ---- 打开 / 构建 ----

    def open(self, page):
        if self._dlg is None:
            self._build()
        self._page = page
        self._reset_idle_ui()
        self._refresh_backend_ui()
        try:
            page.show_dialog(self._dlg)
        except Exception:
            pass

    def _build(self):
        c = self._cols
        self._file_text = ft.Text("未选择文件", size=12, color=c["t3"],
                                  font_family=FONT_MONO, expand=True,
                                  max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._picker = ft.FilePicker()
        self._btn_pick = ft.OutlinedButton(
            "选择文件", icon=ft.Icons.UPLOAD_FILE, height=SIZE["input_h"],
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                text_style=ft.TextStyle(font_family=FONT_UI)),
            on_click=self._pick)

        self._btn_run = ft.FilledButton(
            "开始提取", icon=ft.Icons.PLAY_ARROW, height=SIZE["input_h"],
            style=ft.ButtonStyle(
                bgcolor=c["accent"], color=c["on_accent"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                padding=ft.Padding.symmetric(horizontal=18),
                text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600)),
            on_click=self._run)

        # 后端单次覆盖下拉："跟随全局" = 不覆盖，其余仅影响本次预览
        self._dd_backend = ft.Dropdown(
            value="auto", width=270,
            options=[ft.DropdownOption(key="auto", text="跟随全局设置"),
                     ft.DropdownOption(key="none", text="本地直提（无 OCR）"),
                     ft.DropdownOption(key="mineru-cloud", text="MinerU 云端 OCR")],
            on_change=lambda _e: self._refresh_hint())

        self._backend_chip = ft.Container(
            content=ft.Text("", size=11, color=c["t3"], font_family=FONT_MONO),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=SIZE["radius_pill"],
            bgcolor=ft.Colors.with_opacity(0.08, c["t4"]))

        ext = getattr(ft, "MarkdownExtensionSet", None)
        md_kw = {"extension_set": ext.GITHUB_WEB} if ext else {}
        self._md_view = ft.Markdown(value="", selectable=True, **md_kw)
        self._src_view = ft.TextField(value="", multiline=True, read_only=True,
                                      border=ft.InputBorder.NONE, filled=False,
                                      text_size=12, expand=True,
                                      text_style=ft.TextStyle(
                                          font_family="Consolas", size=12))
        # 自绘页签（版本免疫，不依赖具体 Tabs 控件签名）
        self._btn_render_tab = ft.TextButton("渲染预览",
                                             on_click=lambda _e: self._switch(0))
        self._btn_src_tab = ft.TextButton("Markdown 源码",
                                          on_click=lambda _e: self._switch(1))
        self._render_box = ft.Container(
            content=ft.Column([self._md_view], scroll=ft.ScrollMode.AUTO, expand=True),
            expand=True)
        self._src_box = ft.Container(content=self._src_view, expand=True,
                                     visible=False)
        self._chips = ft.Row([], spacing=8, wrap=True)

        # 活动指示：不确定进度条 + 运行秒表（心跳），完成即隐藏
        self._progress = ft.ProgressBar(value=None, visible=False,
                                        bar_height=4, color=c["accent"],
                                        bgcolor=c["sunken"])
        self._live_text = ft.Text("", size=11, color=c["t2"], font_family=FONT_MONO)
        self._live_row = ft.Row([self._progress, self._live_text], spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                visible=False)

        self._hint = ft.Text(self._hint_text(), size=11, color=c["t4"],
                             font_family=FONT_UI, height=1.4)

        body = ft.Column([
            ft.Row([self._btn_pick, self._file_text],
                   spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self._btn_run, self._dd_backend, self._backend_chip],
                   spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self._live_row,
            self._chips,
            ft.Row([self._btn_render_tab, self._btn_src_tab], spacing=4,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self._render_box,
            self._src_box,
        ], spacing=SIZE["gap_tight"], expand=True)

        self._dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("提取试验台 — 转译效果预览", size=17,
                          weight=ft.FontWeight.W_600, font_family=FONT_UI),
            content=ft.Container(content=ft.Column([body, self._hint],
                                                   spacing=8, expand=True),
                                 width=920, height=640),
            actions=[ft.TextButton("关闭", on_click=self._close)],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )

    def _switch(self, idx):
        self._render_box.visible = idx == 0
        self._src_box.visible = idx == 1
        self._update()

    @staticmethod
    def _current_backend_label():
        import extractors as ex
        b = ex.get_scan_backend()
        has_key = "已配 Key" if ex.current_backend_sig().endswith(":key") else "未配 Key"
        return {"none": "本地直提（OCR 关）",
                "mineru-cloud": f"MinerU 云端（{has_key}）",
                "mineru-local": "本地部署（未支持）"}.get(b, b)

    def _effective_backend(self):
        val = getattr(self, "_dd_backend", None)
        sel = val.value if val is not None else "auto"
        if sel == "auto":
            import extractors as ex
            return ex.get_scan_backend()
        return sel

    def _hint_text(self):
        base = "与索引用同一条提取管线（含缓存）；预览不写入索引。"
        tail = {"none": "当前后端：本地直提——扫描件将被跳过并说明原因。",
                "mineru-cloud": "当前后端：MinerU 云端——每个扫描件可能需要数十秒。",
                "mineru-local": "本地部署属 R3b 尚未支持，扫描件将跳过。",
                }.get(self._effective_backend(), "")
        return base + tail

    def _refresh_hint(self):
        if getattr(self, "_hint", None) is not None:
            self._hint.value = self._hint_text()
        self._refresh_backend_ui()

    def _refresh_backend_ui(self):
        if getattr(self, "_backend_chip", None) is not None:
            self._backend_chip.content.value = \
                f"全局后端：{self._current_backend_label()}"
        self._update()

    def _reset_idle_ui(self):
        """打开/复用实例时确保处于干净待命态（如上次被中途关闭）。"""
        if not self._busy and getattr(self, "_btn_run", None) is not None:
            self._btn_run.disabled = False
            self._btn_run.text = "开始提取"
            self._progress.visible = False
            self._live_row.visible = False

    async def _pick(self, _e=None):
        if self._page is None or self._picker is None:
            return
        try:
            if self._picker not in getattr(self._page, "overlay", []):
                self._page.overlay.append(self._picker)
            files = await self._picker.pick_files(
                dialog_title="选择要试提取的文档",
                allowed_extensions=["pdf", "docx", "md", "txt"],
                allow_multiple=False)
        except Exception:
            return
        if not files:
            return
        self._file_path = files[0].path
        self._file_text.value = self._file_path
        self._set_chips([])
        self._update()

    # ---- 提取执行 ----

    @staticmethod
    def _budget_for(backend):
        try:
            import config as cm
            if backend in ("auto", "mineru-cloud"):
                return max(30.0, float(cm.CFG.get("mineru_timeout_seconds") or 600))
        except Exception:
            pass
        return 60.0

    def _run(self, _e=None):
        if self._busy or not self._file_path:
            return  # 防重入：进行中/未选文件一律忽略
        self._busy = True
        self._btn_run.disabled = True
        self._btn_run.text = "提取中…"
        self._stop.clear()
        self._t0 = time.monotonic()
        backend = self._dd_backend.value
        budget = self._budget_for(backend)
        self._progress.visible = True
        self._live_row.visible = True
        self._live_text.value = self._live_label(budget)
        self._set_chips([])
        self._update()
        threading.Thread(target=self._ticker, args=(budget,), daemon=True,
                         name="extract-lab-ticker").start()
        threading.Thread(target=self._work, daemon=True,
                         args=(None if backend == "auto" else backend,),
                         name="extract-lab-work").start()

    def _live_label(self, budget):
        return (f"⏱ {time.monotonic() - self._t0:.0f}s 运行中"
                f" · 超时预算 ~{int(budget)}s")

    def _ticker(self, budget):
        """活动秒表（心跳）：每 0.7s 刷新运行时长，让用户确信没死机。"""
        while not self._stop.wait(0.7):
            try:
                self._live_text.value = self._live_label(budget)
                self._update()
            except RuntimeError:
                return

    def _work(self, backend):
        import extractors as ex
        try:
            info = ex.extract_preview(self._file_path, backend=backend)
        except Exception as e:  # 双保险：契约之外的异常也不允许线程悬死无反馈
            info = {"md": None, "reason": f"internal:{e.__class__.__name__}",
                    "route": "-", "cached": False, "elapsed": 0.0, "chars": 0}
        self._stop.set()
        self._busy = False
        try:
            self._btn_run.disabled = False
            self._btn_run.text = "开始提取"
            self._progress.visible = False
            self._render(info)
            self._live_row.visible = False
            self._update()
        except RuntimeError:
            pass  # 窗口已关闭：daemon 线程自然结束，无任何需回收的状态

    def _render(self, info):
        c = self._cols
        md, reason = info["md"], info["reason"]
        chips = [(f"耗时 {info['elapsed']}s", "t3"),
                 (f"{info['chars']} 字符", "t3")]
        if info["cached"]:
            chips.append(("缓存命中", "success"))
        if reason:
            label, guide = reason, ""
            try:
                from gui.store import ISSUE_TEXT
                label, guide = ISSUE_TEXT.get(reason, (reason, ""))
            except Exception:
                pass
            chips.insert(0, (f"✗ {reason} · {label}｜{guide}", "danger"))
            self._md_view.value = ""
            self._src_view.value = ""
        else:
            chips.insert(0, (f"路由 {info['route']}", "accent"))
            # 渲染视图做内联标签净化（<u> 等 flet Markdown 不渲染的裸 HTML）；
            # 源码页保持原样，以源码为准
            import extractors as ex
            self._md_view.value = ex.sanitize_render_md(md or "")
            self._src_view.value = md or ""
        self._set_chips([(txt, self._cols.get(key, key)) for txt, key in chips])

    def _set_chips(self, items):
        c = self._cols
        self._chips.controls = [
            ft.Container(content=ft.Text(txt, size=11, color=c.get(key, c["t2"]),
                                         font_family=FONT_UI),
                         padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                         border_radius=SIZE["radius_pill"],
                         bgcolor=ft.Colors.with_opacity(0.10, c.get(key, c["t4"])))
            for txt, key in items]

    def _update(self):
        try:
            if self._page is not None:
                self._page.update()
        except RuntimeError:
            pass  # 页面已销毁（GUI 关闭）：静默放弃 UI 更新

    def _close(self, _e=None):
        self._stop.set()          # 停秒表；worker 完成回调自行静默
        self._busy = False
        if getattr(self, "_btn_run", None) is not None:
            self._btn_run.disabled = False
            self._btn_run.text = "开始提取"
        if getattr(self, "_progress", None) is not None:
            self._progress.visible = False
            self._live_row.visible = False
        try:
            if self._dlg is not None:
                self._dlg.open = False
                self._dlg.update()
            if self._picker is not None and self._page is not None \\
                    and self._picker in getattr(self._page, "overlay", []):
                self._page.overlay.remove(self._picker)
                self._page.update()
        except RuntimeError:
            pass
'''
t = t[:idx] + new_class
compile(t, "gui/widgets.py", "exec")
p.write_text(t, encoding="utf-8", newline="")
print("OK 行数:", len(t.splitlines()))
