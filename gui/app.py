"""app.py — 语义搜索控制台主程序。

运行：cd obsidian-rag && .venv\\Scripts\\python gui\\app.py
功能：索引状态看板（KPI/进度 stepper/心跳胶囊/设备条）、一键增量/全量重建、
搜索测试、深浅主题。布局纪律：全窗口只有日志区一块弹性区域，其余固定高度。
"""
import asyncio
import datetime
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import flet as ft

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CFG  # noqa: E402
from theme import DARK, LIGHT, FONT_UI, SIZE  # noqa: E402
from store import (  # noqa: E402
    STATE_NONE,
    index_state, index_busy, meta_stats, progress_ratio,
    heartbeat_state, read_progress, library_entries, library_snapshot,
    meta_issues_for, ISSUE_TEXT,
    is_library_dir,
)
from worker import IndexWorker, read_history  # noqa: E402
from widgets import (  # noqa: E402
    KpiCard, StatusCard, HeartbeatPill, ProgressCard, SearchCard, LogView,
    DeviceBar, SettingsDialog, LibraryPicker, LibraryManagerDialog,
    ALL_LIBRARIES,
)

MODEL_NAME = CFG["model_name"]
VAULT_DIR = CFG["vault"]
DATA_DIR = str(Path(__file__).resolve().parent.parent / "data")
GUI_PID_FILE = Path(DATA_DIR) / "gui.pid"
GUI_LOCK_FILE = Path(DATA_DIR) / "gui.lock"
_IS_WINDOWS = os.name == "nt"


def _acquire_gui_singleton():
    """GUI 单例守卫：文件字节锁（msvcrt/flock，与 index.py 同款）。

    背景：flet 0.86 桌面模式是双进程（父引导 + 子进程跑 __main__），PID
    探测不可靠（实测 os.kill 对 pythonw 误判"已死"导致重复实例）。
    文件锁随进程退出自动释放：新实例拿不到锁 = 已有实例在跑，直接退出。
    另写 PID 文件供 gui/stop.py 诊断与命令行兜底匹配使用。
    """
    import atexit

    def _err(msg):
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass

    try:
        p = GUI_PID_FILE
        p.parent.mkdir(exist_ok=True)
        p.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(_release_gui_singleton)
    except OSError as e:
        _err("GUI PID 文件写入失败（忽略）：%s" % e)

    try:
        f = open(GUI_LOCK_FILE, "a+b")
    except OSError as e:
        _err("GUI 锁文件打开失败（忽略，可能重复实例）：%s" % e)
        return
    try:
        if _IS_WINDOWS:
            import msvcrt
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                _err("检测到已有控制台实例运行，本实例退出（单例守卫）。")
                sys.exit(0)
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _err("检测到已有控制台实例运行，本实例退出（单例守卫）。")
                sys.exit(0)
        # 持有锁直到进程退出：锁 fd 保持打开，全局引用防 GC
        global _GUI_LOCK_FD
        _GUI_LOCK_FD = f
    except OSError as e:
        _err("GUI 单例守卫失败（继续启动）：%s" % e)


_GUI_LOCK_FD = None


def _release_gui_singleton():
    """退出清理：仅当 PID 文件是本进程记录时才删除。"""
    try:
        if GUI_PID_FILE.exists():
            cur = int(GUI_PID_FILE.read_text(encoding="utf-8").strip().split()[0])
            if cur == os.getpid():
                GUI_PID_FILE.unlink()
    except (ValueError, OSError):
        pass


def format_elapsed(seconds):
    """耗时格式化：≥60s → 「x分y秒」；<60s → 「x.xs」；无 → 「—」。"""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "—"
    if seconds >= 60:
        m, s = divmod(int(seconds), 60)
        return "%d分%d秒" % (m, s)
    return "%.1fs" % seconds


def format_mmss(seconds):
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    return "%02d:%02d" % (m, s)


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.colors = DARK
        self.worker = IndexWorker()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._searching = False
        self._log_cursor = 0
        self._last_completed = None    # 上次完成耗时（秒）
        self._last_completed_at = None  # 上次完成时间戳
        self._last_completed_lib = ""   # 上次完成所属库
        self._dead_reported = False
        self._lib_by_name = {}

        self._setup_window()
        self._build_widgets()
        self._build_layout()
        self.worker.on_output = self._on_worker_line
        self.worker.on_exit = self._on_worker_exit
        self.log_view.attach(page)
        self.log_view.load_history(read_history()[-300:])
        self._capture_completed(read_progress())
        self._refresh_library_meta()
        self.page.run_task(self._refresh_loop)

    # ---------- 初始化 ----------

    def _setup_window(self):
        self.page.title = "语义搜索控制台"
        self.page.window.width = SIZE["window_w"]
        self.page.window.height = SIZE["window_h"]
        self.page.window.min_width = SIZE["window_min_w"]
        self.page.window.min_height = SIZE["window_min_h"]
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL,
                                   font_family=FONT_UI)
        self.page.bgcolor = DARK["base"]
        self.page.padding = SIZE["pad"]
        self.page.spacing = 0

    def _build_widgets(self):
        self.kpi_files = KpiCard("笔记文件", ft.Icons.DESCRIPTION_OUTLINED)
        self.kpi_chunks = KpiCard("检索块数", ft.Icons.VIEW_AGENDA_OUTLINED)
        self.kpi_time = KpiCard("最后索引耗时", ft.Icons.TIMER_OUTLINED)
        self.status_card = StatusCard()
        self.heartbeat = HeartbeatPill()
        self.progress = ProgressCard()
        self.progress.btn_inc.on_click = lambda e: self._start_index(False)
        self.progress.btn_full.on_click = self._confirm_full
        self.search = SearchCard(self._do_search, on_open=self._open_result)
        self.log_view = LogView()
        self.device = DeviceBar(self._open_vault, self._open_logs)
        self.lib_picker = LibraryPicker(self._on_library_selected)
        self.lib_picker._page = self.page
        self.lib_manager = LibraryManagerDialog(self._on_libraries_changed)
        self.theme_btn = ft.IconButton(
            icon=ft.Icons.DARK_MODE_OUTLINED, icon_color=DARK["t2"],
            tooltip="切换深浅色主题", on_click=self._toggle_theme,
        )
        self.library_btn = ft.IconButton(
            icon=ft.Icons.LIBRARY_BOOKS_OUTLINED, icon_color=DARK["t2"],
            tooltip="库管理（注册/移除/配置）", on_click=self._open_library_manager,
        )
        self.settings_btn = ft.IconButton(
            icon=ft.Icons.SETTINGS_OUTLINED, icon_color=DARK["t2"],
            tooltip="设置", on_click=self._open_settings,
        )
        self.settings = SettingsDialog(self._on_settings_saved)

    def _build_layout(self):
        header = ft.Row([
            ft.Container(width=8, height=8, border_radius=4, bgcolor=DARK["accent"]),
            ft.Text("语义搜索控制台", size=20, weight=ft.FontWeight.W_700,
                    color=DARK["t1"], font_family=FONT_UI),
            ft.Container(expand=True),
            self.lib_picker.card,
            self.library_btn,
            self.settings_btn,
            self.theme_btn,
            self.heartbeat.card,
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER)
        header_box = ft.Container(content=header, height=SIZE["header_h"])

        kpi_row = ft.Row([
            self.kpi_files.card, self.kpi_chunks.card,
            self.kpi_time.card, self.status_card.card,
        ], spacing=SIZE["gap"], height=SIZE["kpi_h"])

        mid = ft.Row([
            self.progress.card,
            ft.VerticalDivider(width=1, color=DARK["border_faint"]),
            self.search.card,
        ], spacing=SIZE["gap"], expand=True,
           vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.mid_row = mid
        self.progress.card.expand = False
        self.progress.card.width = SIZE["progress_w"]

        self.page.add(
            ft.Column([
                header_box,
                ft.Container(height=SIZE["gap"]),
                kpi_row,
                ft.Container(height=SIZE["gap"]),
                mid,
                ft.Container(height=SIZE["gap"]),
                self.device.card,
                ft.Container(height=SIZE["gap"]),
                ft.Container(content=self.log_view.card, height=SIZE["log_h"]),
            ], expand=True, spacing=0),
        )

    # ---------- 主循环 ----------

    async def _refresh_loop(self):
        while True:
            try:
                self._refresh_once()
            except Exception as e:
                self._log_line("ERROR [GUI] 刷新失败：%s" % e)
            await asyncio_sleep_1s()

    def _capture_completed(self, progress):
        """记录上次完成状态（running=False 且 phase=done 时）。"""
        if not progress.get("running") and progress.get("phase") == "done":
            el = progress.get("elapsed_s")
            if isinstance(el, (int, float)) and el > 0:
                self._last_completed = el
                up = progress.get("updated_at") or time.time()
                self._last_completed_at = datetime.datetime.fromtimestamp(up)
                self._last_completed_lib = progress.get("library") or ""

    def _refresh_once(self):
        colors = self.colors
        progress = read_progress()
        state, files, chunks = index_state()
        running = bool(progress.get("running"))

        if not running:
            self._capture_completed(progress)

        self.kpi_files.set_value(str(files) if files else "0",
                                 None if state != STATE_NONE else colors["t4"])
        self.kpi_chunks.set_value(str(chunks) if chunks else "0",
                                  None if state != STATE_NONE else colors["t4"])
        self._update_time_kpi(progress, running, colors)
        self._update_status_card(state, colors)
        self._update_kpi_subs()

        ratio = progress_ratio(progress)
        now = time.time()
        elapsed_txt, eta_txt = "", ""
        if running:
            elapsed_txt = "已用 %s" % format_mmss(progress.get("elapsed_s") or (now - (progress.get("started_at") or now)))
            eta = progress.get("eta_s")
            eta_txt = "ETA %s" % format_mmss(eta) if isinstance(eta, (int, float)) else "ETA …"
        idle_summary = self._idle_summary_text(progress.get("running"))
        self.progress.update(progress, ratio, colors, elapsed_txt, eta_txt, idle_summary)

        hb = heartbeat_state(progress)
        # converting 相位：停滞豁免已在 store.heartbeat_state 对齐；这里把
        # 「心跳正常」文案换成转换中提示，让用户知道不是卡死
        hb_note = ("文档转换中（大文件耗时属预期）"
                   if running and progress.get("phase") == "converting" else None)
        self.heartbeat.set_state(hb, datetime.datetime.now(), colors, note=hb_note)
        self.heartbeat.tick()
        if hb == "dead" and running and not self._dead_reported:
            self._dead_reported = True
            self._log_line("ERROR [GUI] 心跳中断：索引疑似卡死，请查看上方状态")
            self._snack("索引疑似卡死（心跳中断），请查看日志", is_error=True)
        if hb != "dead":
            self._dead_reported = False

        self.device.update(progress.get("device"), MODEL_NAME, files, chunks,
                           self.lib_picker.text.value)

        self._update_last_chip(running, colors)

        busy = index_busy()
        self.progress.set_busy(busy, colors)

        new_lines, self._log_cursor = self.worker.lines_since(self._log_cursor)
        for line in new_lines:
            self._log_line(line)
        self.page.update()

    # ---------- 多库 ----------

    def _refresh_library_meta(self):
        """重建库名索引并刷新库选择胶囊（注册表变化后调用）。"""
        entries = library_entries()
        self._lib_by_name = {e["name"]: e for e in entries}
        names = [e["name"] for e in entries]
        self.lib_picker.set_names(names)

    def _on_library_selected(self, checked):
        self._log_line("── 切换检索范围：%s"
                       % (self.lib_picker._summary_text()
                          if hasattr(self.lib_picker, "_summary_text")
                          else self.lib_picker.text.value))
        self.page.update()

    def _on_libraries_changed(self, names):
        self._refresh_library_meta()

    def _open_library_manager(self, e):
        self.lib_manager.open(self.page)

    def _selected_rows(self):
        """当前范围内的库快照行 [(name, state, files, chunks, path)]。"""
        _, rows = library_snapshot()
        sel = self.lib_picker.selected_names
        return [r for r in rows if r[0] in sel]

    def _idle_summary_text(self, running):
        """进度卡空闲行：当前范围内库汇总（区别于上次任务的统计）。"""
        if running:
            return ""
        sel = self._selected_rows()
        files = sum(r[2] for r in sel)
        chunks = sum(r[3] for r in sel)
        if not files and not chunks:
            return "尚无索引"
        n = len(sel)
        return "共 %d 库 · %d 文件 / %d 块（空闲）" % (n or 0, files, chunks)

    def _update_kpi_subs(self):
        """KPI 副行：全部库 = 各库块数分布；多选 = 选中库合计。"""
        colors = self.colors
        _, rows = library_snapshot()
        by = {r[0]: r for r in rows}
        sel = self.lib_picker.selected_names
        if self.lib_picker.is_all:
            n = len(by)
            if not n:
                self.kpi_files.set_sub("尚未注册库")
                self.kpi_chunks.set_sub("")
                return
            dist = " · ".join("%s %d" % (r[0], r[3]) for r in rows)
            self.kpi_files.set_sub("共 %d 个库" % n)
            self.kpi_chunks.set_sub(dist if len(dist) <= 40 else "各库块数见库管理")
            return
        if not sel:
            self.kpi_files.set_sub("未选库")
            self.kpi_chunks.set_sub("")
            return
        f = sum(by[n][2] for n in sel if n in by)
        c = sum(by[n][3] for n in sel if n in by)
        label = next(iter(sel)) if len(sel) == 1 else "%d 库" % len(sel)
        self.kpi_files.set_sub("%s · %d 文件" % (label, f))
        self.kpi_chunks.set_sub("%s · %d 块" % (label, c))

    def _update_status_card(self, agg_state, colors):
        """状态卡：大状态 = 当前范围内聚合三态；副行 = 选中库明细或聚合文案。"""
        _, rows = library_snapshot()
        by = {r[0]: r for r in rows}
        sel = self.lib_picker.selected_names
        if not self.lib_picker.is_all and sel:
            if len(sel) == 1 and next(iter(sel)) in by:
                name, st, f, c, path = by[next(iter(sel))]
                sub = "%s：%d 文件 / %d 块" % (name, f, c)
            else:
                ns = [r for r in rows if r[0] in sel]
                f = sum(r[2] for r in ns)
                c = sum(r[3] for r in ns)
                sub = "%d 库：%d 文件 / %d 块" % (len(ns), f, c)
        else:
            n = len(by)
            if not n:
                sub = "尚未注册库，点击库管理添加"
            else:
                nstale = sum(1 for r in rows if r[1] == "stale")
                nnone = sum(1 for r in rows if r[1] == "none")
                sub = "%d 库：%d 待索引" % (n, nstale) if nstale else (
                    "%d 库：均最新" % n if nnone == 0 else "%d 库：均未索引" % n)
        issue_txt = self._issues_suffix(by)
        if issue_txt:
            sub = "%s ｜ ⚠ %s" % (sub, issue_txt) if sub else "⚠ %s" % issue_txt
        self.status_card.set_state(agg_state, colors, sub=sub)

    def _issues_suffix(self, by_name_rows):
        """选中范围内提取失败（xfail 终态）文件的汇总短文案；无问题返回空串。

        形如「提取跳过 4 个文件：扫描件×3、不可读×1」——只提示不阻塞，
        处置指引见库管理或 AI_GUIDE（reason 全集定义在 gui.store.ISSUE_TEXT）。
        """
        sel = None if self.lib_picker.is_all else self.lib_picker.selected_names
        names = list(by_name_rows.keys()) if sel is None else \
            [n for n in sel if n in by_name_rows]
        tally = {}
        for n in names:
            cfg = self._lib_by_name.get(n)
            if not cfg:
                continue
            for reason, cnt in meta_issues_for(cfg).items():
                tally[reason] = tally.get(reason, 0) + cnt
        if not tally:
            return ""
        total = sum(tally.values())
        detail = "、".join(
            "%s×%d" % (ISSUE_TEXT.get(r, (r, ""))[0], c)
            for r, c in sorted(tally.items(), key=lambda kv: -kv[1]))
        return "提取跳过 %d 个文件：%s" % (total, detail)

    def _update_time_kpi(self, progress, running, colors):
        """耗时卡状态机：进行中=实时计时；未索引=「—」；其余=上次完成耗时。"""
        if running:
            el = progress.get("elapsed_s")
            self.kpi_time.set_value(format_mmss(el) if el is not None else "…",
                                    color=colors["accent"])
            phase = progress.get("phase") or ""
            self.kpi_time.set_sub("进行中 · 阶段：%s" % phase)
            return
        if self._last_completed is None:
            self.kpi_time.set_value("—", color=colors["t4"])
            self.kpi_time.set_sub("尚无索引记录")
            return
        self.kpi_time.set_value(format_elapsed(self._last_completed),
                                color=colors["t1"])
        if self._last_completed_at:
            ago = max(0, int(time.time() - self._last_completed_at.timestamp()))
            lib_txt = (" · %s" % self._last_completed_lib) if self._last_completed_lib else ""
            if ago < 3:
                self.kpi_time.set_sub("刚刚完成%s" % lib_txt)
            else:
                self.kpi_time.set_sub("上次完成 %s%s" % (self._last_completed_at.strftime("%H:%M"), lib_txt))
        else:
            self.kpi_time.set_sub("")

    def _update_last_chip(self, running, colors):
        if running:
            self.progress.set_last("重建中…", colors, running=True)
        elif self._last_completed is not None:
            self.progress.set_last("上次 %s" % format_elapsed(self._last_completed), colors)
        else:
            self.progress.set_last("尚未索引", colors)

    # ---------- 日志回调 ----------

    def _on_worker_line(self, line):
        pass  # 仅缓冲，UI 由刷新循环统一提交

    def _on_worker_exit(self, rc):
        if rc != 0:
            self._snack("重建失败，请查看底部日志", is_error=True)
            self._log_line("ERROR [GUI] 索引进程退出码 %s" % rc)

    def _log_line(self, line):
        self.log_view.append(line)

    # ---------- 交互 ----------

    def _selected_library_arg(self):
        """索引/搜索参数：全部库 → ''（后端默认全库）；多选 → 'A,B'。"""
        return self.lib_picker.value

    def _selected_blocks(self):
        """当前范围内库的总块数（确认框文案用）。"""
        return sum(r[3] for r in self._selected_rows())

    def _range_label(self):
        """当前范围的短文案：全部库 / 单库名 / N 个库。"""
        if self.lib_picker.is_all:
            return "全部库"
        sel = self.lib_picker.selected_names
        if not sel:
            return "未选库"
        if len(sel) == 1:
            return "库「%s」" % next(iter(sel))
        return "%d 个库" % len(sel)

    def _start_index(self, full):
        if index_busy():
            self._snack("已有索引任务在运行", is_error=True)
            return
        lib = self._selected_library_arg()
        if self.worker.start(full=full, library=lib):
            target = self._range_label()
            self._snack("已开始%s：%s" % ("全量重建" if full else "增量重建", target))
            self._log_line("── 用户触发%s：%s" % ("全量重建" if full else "增量重建", target))
        else:
            self._snack("索引任务已在运行", is_error=True)

    def _confirm_full(self, e):
        lib = self._selected_library_arg()
        target = self._range_label()
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("全量重建", size=18, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Text(
                "将清空并重新嵌入%s的约 %d 个文本块，预计 3–5 分钟，期间检索可用性下降。\n是否继续？"
                % (target, self._selected_blocks()),
                size=14, font_family=FONT_UI,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton(
                    "继续重建",
                    style=ft.ButtonStyle(
                        bgcolor=self.colors["danger"], color="#FFFFFF",
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600)),
                    on_click=lambda _: (self._close_dialog(dlg), self._start_index(True)),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        self.page.show_dialog(dlg)

    def _close_dialog(self, dlg):
        dlg.open = False
        dlg.update()

    def _open_settings(self, e):
        self.settings.open(self.page)

    def _on_settings_saved(self, errors):
        if errors:
            self._snack("设置保存失败，请检查字段", is_error=True)
        else:
            self._snack("已保存 config.json")

    def _toggle_theme(self, e):
        self.colors = LIGHT if self.colors is DARK else DARK
        self.page.theme_mode = (ft.ThemeMode.LIGHT if self.colors is LIGHT
                                else ft.ThemeMode.DARK)
        self.page.bgcolor = self.colors["base"]
        for w in (self.kpi_files, self.kpi_chunks, self.kpi_time,
                  self.status_card, self.heartbeat, self.progress,
                  self.search, self.log_view, self.device, self.settings,
                  self.lib_picker, self.lib_manager):
            w.apply(self.colors)
        self.theme_btn.icon = (ft.Icons.LIGHT_MODE_OUTLINED if self.colors is LIGHT
                               else ft.Icons.DARK_MODE_OUTLINED)
        self.theme_btn.icon_color = self.colors["t2"]
        self.page.update()

    def _do_search(self, query):
        if not query or not query.strip():
            self._snack("请输入问题")
            return
        if self._searching:
            return
        self._searching = True
        self.search.btn.disabled = True
        self.search.set_status("loading")
        self.page.update()
        t0 = time.time()
        top_k = int(self.search.top_k.value or CFG["default_top_k"])
        show_body = bool(self.search.body_switch.value)
        lib = self._selected_library_arg()

        def run():
            from retriever import hybrid_search
            return hybrid_search(query.strip(), top_k=top_k, libraries=lib,
                                 include_body=True, with_scores=True)

        self._search_t0 = t0
        self._search_show_body = show_body
        self._search_lib = lib
        self.executor.submit(run).add_done_callback(
            lambda f: self.page.run_task(self._finish_search, f))

    async def _finish_search(self, fut):
        """搜索完成回调（必须 async：run_task 仅接受 coroutine function）。"""
        self._searching = False
        self.search.btn.disabled = False
        try:
            result = fut.result()
            self.search.show_results(result, self.colors,
                                     show_body=getattr(self, "_search_show_body", True))
            lib_txt = "（%s）" % self._search_lib if self._search_lib else "（全部库）"
            self._log_line("── 检索「%s」%s 耗时 %.1fs"
                           % (self.search.input.value.strip(), lib_txt,
                              time.time() - self._search_t0))
        except Exception as ex:
            self._log_line("ERROR [GUI] 搜索失败：%s" % ex)
            self._snack("搜索失败：%s" % ex, is_error=True)
        self.page.update()

    # ---------- 工具 ----------

    def _open_result(self, rel, heading=""):
        """点击搜索结果 → 打开源文件（多库感知）。

        来源行带 <库名>/<相对路径> 前缀：解析出库名 → 查注册表拿到库路径。
        - 库路径是 Obsidian vault（含 .obsidian）→ obsidian://open?vault=<库文件夹名>
        - 其他库（任意 md 文件夹）→ 系统默认打开库路径下的文件
        - 无前缀/库名未知（旧格式）→ 回退单库旧逻辑（主 vault）
        """
        import urllib.parse
        lib_name, inner = self._split_lib_rel(rel)
        lib_cfg = self._lib_by_name.get(lib_name) if lib_name else None
        if lib_cfg is None:
            lib_name, inner = None, rel

        if lib_cfg is not None and not is_library_dir(lib_cfg["path"]):
            target = str(Path(lib_cfg["path"]) / inner)
            try:
                os.startfile(target)
                self._log_line("── 打开 %s%s" % (inner, ("（%s）" % heading) if heading else ""))
            except OSError as ex:
                self._snack("无法打开：%s" % ex, is_error=True)
            return

        vault_path = lib_cfg["path"] if lib_cfg is not None else VAULT_DIR
        try:
            vault_name = Path(vault_path).name
            file_part = urllib.parse.quote(inner, safe="/")
            url = "obsidian://open?vault=%s&file=%s" % (
                urllib.parse.quote(vault_name), file_part)
            if heading:
                url += "#" + urllib.parse.quote(heading)
            os.startfile(url)
            self._log_line("── 打开 %s%s" % (inner, ("（%s）" % heading) if heading else ""))
        except OSError as ex:
            fallback = str(Path(vault_path) / inner)
            try:
                os.startfile(fallback)
                self._log_line("── Obsidian URI 不可用，用系统打开 %s" % fallback)
            except OSError as ex2:
                self._snack("无法打开：%s" % ex2, is_error=True)
                self._log_line("ERROR [GUI] 打开源文件失败：%s" % ex2)

    @staticmethod
    def _split_lib_rel(rel):
        """把 '<库名>/<相对路径>' 拆成 (库名, 相对路径)；无前缀 → (None, rel)。"""
        s = rel.replace("\\", "/")
        idx = s.find("/")
        if idx == -1:
            return None, rel
        return s[:idx], s[idx + 1:]

    def _open_vault(self, e):
        """打开当前范围内第一个库的文件夹（全部库时打开第一个库）。"""
        sel = self.lib_picker.selected_names
        entries = library_entries()
        for en in entries:
            if en["name"] in sel:
                self._open_dir(en["path"])
                return
        if entries:
            self._open_dir(entries[0]["path"])
            return
        self._open_dir(VAULT_DIR)

    def _open_logs(self, e):
        self._open_dir(DATA_DIR)

    def _open_dir(self, path):
        try:
            os.startfile(path)
        except OSError as ex:
            self._snack("无法打开文件夹：%s" % ex, is_error=True)
            self._log_line("ERROR [GUI] 打开目录失败：%s" % ex)

    def _snack(self, msg, is_error=False):
        self.page.show_dialog(ft.SnackBar(
            content=ft.Text(msg, font_family=FONT_UI, color=self.colors["on_accent"]),
            bgcolor=self.colors["error_bg"] if is_error else self.colors["accent"],
        ))


async def asyncio_sleep_1s():
    await asyncio.sleep(1.0)


def main(page: ft.Page):
    App(page)


if __name__ == "__main__":
    _acquire_gui_singleton()
    ft.run(main)
