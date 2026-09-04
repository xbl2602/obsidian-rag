"""patch_lab3.py — 试验台进程隔离 + 控件级刷新。

1. extractors 增加 _preview_job(q, path_str, backend)：子进程入口，结果走队列。
2. ExtractLabDialog：线程→多进程（GIL 不再饿死 UI；超时可 terminate 强杀）；
   所有 UI 更新改控件级定向 update（不再整页 page.update）；运行中禁用选文件；
   关闭对话框 = 立即 terminate 子进程的取消语义。
"""
from pathlib import Path

# ---------- ① extractors：子进程任务入口 ----------
pe = Path("extractors.py")
te = pe.read_text(encoding="utf-8")

job = '''

def _preview_job(q, path_str, backend=None):
    """子进程入口（GUI 提取试验台用）：结果经队列返回父进程。

    独立进程彻底绕开 GIL——重转换期间 UI 线程零争抢；
    超时/取消由父进程 terminate() 即时强杀，无残留状态可担心。
    """
    try:
        info = extract_preview(Path(path_str), backend=backend)
        q.put({"ok": True, "info": info})
    except Exception as e:
        q.put({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
'''
if "_preview_job" not in te:
    te = te.rstrip() + "\n" + job
compile(te, "extractors.py", "exec")
pe.write_text(te, encoding="utf-8", newline="")

# ---------- ② widgets：替换整个 ExtractLabDialog ----------
pw = Path("gui/widgets.py")
t = pw.read_text(encoding="utf-8")

idx = t.index("class ExtractLabDialog:")
new_class = '''class ExtractLabDialog:
    """转换试验台：选一个文件，走与索引完全相同的提取管线（含缓存与 OCR 后端），
    就地预览产出质量。顶部控制行（含后端单次覆盖下拉）+ 活动指示（不确定进度条
    + 秒表）+ 整幅结果区（渲染/源码页签自绘切换）。不做左右分栏——输入是二进制
    文件没有可展示的"原文"，整幅留给产出最省空间。

    执行模型（2026-08-24 修订）：提取跑在**独立子进程**——
    - pymupdf4llm 重转换是纯 Python 计算，线程模型会被 GIL 饿死 UI（实测按钮秒级延迟）；
    - 进程隔离后 UI 零争抢，且超时/取消可 terminate() 即时强杀；
    - 缓存写入是 tmp+os.replace 原子操作，强杀至多留孤儿 tmp（启动清扫回收），
      预览不写 meta/Chroma/终态，任何时刻中断都无需回滚。
    UI 更新全部控件级定向刷新，不整页 page.update（防会话销毁连锁异常）。
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
        self._proc = None

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

        self._dd_backend = ft.Dropdown(
            value="auto", width=270,
            options=[ft.DropdownOption(key="auto", text="跟随全局设置"),
                     ft.DropdownOption(key="none", text="本地直提（无 OCR）"),
                     ft.DropdownOption(key="mineru-cloud", text="MinerU 云端 OCR")])
        # flet 0.86：Dropdown 事件名为 on_select（构造器不接受 on_change）
        self._dd_backend.on_select = lambda _e: self._refresh_hint()

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
        self._safe_update(self._render_box, self._src_box,
                          self._btn_render_tab, self._btn_src_tab)

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
            self._safe_update(self._hint)
        self._refresh_backend_ui()

    def _refresh_backend_ui(self):
        if getattr(self, "_backend_chip", None) is not None:
            self._backend_chip.content.value = \\
                f"全局后端：{self._current_backend_label()}"
        self._safe_update(self._backend_chip)

    def _reset_idle_ui(self):
        """打开/复用实例时确保处于干净待命态（如上次被中途关闭）。"""
        if not self._busy and getattr(self, "_btn_run", None) is not None:
            self._btn_run.disabled = False
            self._btn_run.content = "开始提取"
            self._btn_pick.disabled = False
            self._progress.visible = False
            self._live_row.visible = False

    async def _pick(self, _e=None):
        if self._busy or self._page is None or self._picker is None:
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
        self._safe_update(self._file_text, self._chips)

    # ---- 提取执行（独立子进程） ----

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
        import multiprocessing as mp
        self._busy = True
        self._btn_run.disabled = True
        self._btn_run.content = "提取中…"
        self._btn_pick.disabled = True  # 运行中锁定文件选择，防状态错乱
        self._stop.clear()
        self._t0 = time.monotonic()
        backend = self._dd_backend.value
        budget = self._budget_for(backend)
        self._progress.visible = True
        self._live_row.visible = True
        self._live_text.value = self._live_label(budget)
        self._set_chips([])

        q = mp.Queue()
        from extractors import _preview_job
        self._proc = mp.Process(target=_preview_job,
                                args=(q, str(self._file_path),
                                      None if backend == "auto" else backend),
                                daemon=True)
        self._proc.start()
        threading.Thread(target=self._poll, args=(q, budget), daemon=True,
                         name="extract-lab-poll").start()
        self._safe_update(self._btn_run, self._btn_pick,
                          self._live_row, self._chips)

    def _live_label(self, budget):
        return (f"⏱ {time.monotonic() - self._t0:.0f}s 运行中"
                f" · 超时预算 ~{int(budget)}s")

    def _poll(self, q, budget):
        """轮询子进程结果 + 秒表心跳；超时/取消即 terminate 强杀。"""
        import queue as _q
        deadline = time.monotonic() + max(30.0, budget) + 15  # 宽限 15s 给下载收尾
        payload = None
        outcome = "done"
        while True:
            try:
                payload = q.get(timeout=0.5)
                break
            except _q.Empty:
                pass
            except Exception as e:
                payload = {"ok": False, "error": f"queue:{e.__class__.__name__}"}
                break
            if self._stop.is_set():
                outcome = "cancelled"
                break
            if time.monotonic() > deadline:
                outcome = "timeout"
                break
            try:
                self._live_text.value = self._live_label(budget)
                self._live_text.update()
            except RuntimeError:
                return  # 页面销毁：随 daemon 进程退出
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.is_alive():
            try:
                proc.terminate()
                proc.join(timeout=5)
            except Exception:
                pass
        if outcome != "done" and self._stop.is_set():
            return  # 用户主动关闭对话框：静默，_close 已复位 UI
        self._stop.set()
        self._busy = False
        try:
            self._btn_run.disabled = False
            self._btn_run.content = "开始提取"
            self._btn_pick.disabled = False
            self._progress.visible = False
            if outcome == "timeout":
                self._render({"md": None, "reason": "extract-failed",
                              "route": "-", "cached": False,
                              "elapsed": round(time.monotonic() - self._t0, 2),
                              "chars": 0})
                self._set_chips([("✗ 超时被终止 · 可直接重试", "danger")])
            elif outcome == "done":
                if payload.get("ok"):
                    self._render(payload["info"])
                else:
                    self._set_chips([(f"✗ 子进程异常：{payload.get('error', '?')}",
                                      "danger")])
            else:
                self._render({"md": None, "reason": "extract-failed",
                              "route": "-", "cached": False, "elapsed": 0.0,
                              "chars": 0})
            self._live_row.visible = False
            self._safe_update(self._btn_run, self._btn_pick, self._progress,
                              self._live_row, self._chips,
                              self._md_view, self._src_view)
        except RuntimeError:
            pass  # 窗口已关闭

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

    def _safe_update(self, *controls):
        """控件级定向刷新（不整页 update，防 destroyed-session 连锁）。"""
        for ctl in controls:
            try:
                ctl.update()
            except Exception:
                pass  # 会话销毁/控件未挂载：静默

    def _switch(self, idx):
        self._render_box.visible = idx == 0
        self._src_box.visible = idx == 1
        self._safe_update(self._render_box, self._src_box,
                          self._btn_render_tab, self._btn_src_tab)

    def _close(self, _e=None):
        self._stop.set()  # 轮询线程收到即 terminate 子进程并静默退出
        self._busy = False
        if getattr(self, "_btn_run", None) is not None:
            self._btn_run.disabled = False
            self._btn_run.content = "开始提取"
        if getattr(self, "_btn_pick", None) is not None:
            self._btn_pick.disabled = False
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
pw.write_text(t, encoding="utf-8", newline="")
print("widgets OK 行数:", len(t.splitlines()))
