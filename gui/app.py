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
    heartbeat_state, read_progress,
)
from worker import IndexWorker, read_history  # noqa: E402
from widgets import (  # noqa: E402
    KpiCard, StatusCard, HeartbeatPill, ProgressCard, SearchCard, LogView, DeviceBar,
)

MODEL_NAME = CFG["model_name"]
VAULT_DIR = CFG["vault"]
DATA_DIR = str(Path(__file__).resolve().parent.parent / "data")


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
        self._dead_reported = False

        self._setup_window()
        self._build_widgets()
        self._build_layout()
        self.worker.on_output = self._on_worker_line
        self.worker.on_exit = self._on_worker_exit
        self.log_view.attach(page)
        self.log_view.load_history(read_history()[-300:])
        self._capture_completed(read_progress())
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
        self.search = SearchCard(self._do_search)
        self.log_view = LogView()
        self.device = DeviceBar(self._open_vault, self._open_logs)
        self.theme_btn = ft.IconButton(
            icon=ft.Icons.DARK_MODE_OUTLINED, icon_color=DARK["t2"],
            tooltip="切换深浅色主题", on_click=self._toggle_theme,
        )

    def _build_layout(self):
        header = ft.Row([
            ft.Container(width=8, height=8, border_radius=4, bgcolor=DARK["accent"]),
            ft.Text("语义搜索控制台", size=20, weight=ft.FontWeight.W_700,
                    color=DARK["t1"], font_family=FONT_UI),
            ft.Container(expand=True),
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
        ], spacing=SIZE["gap"], height=SIZE["mid_h"],
           vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.mid_row = mid

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
                self.log_view.card,
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
        self.status_card.set_state(state, colors)

        ratio = progress_ratio(progress)
        now = time.time()
        elapsed_txt, eta_txt = "", ""
        if running:
            elapsed_txt = "已用 %s" % format_mmss(progress.get("elapsed_s") or (now - (progress.get("started_at") or now)))
            eta = progress.get("eta_s")
            eta_txt = "ETA %s" % format_mmss(eta) if isinstance(eta, (int, float)) else "ETA …"
        self.progress.update(progress, ratio, colors, elapsed_txt, eta_txt)

        hb = heartbeat_state(progress)
        self.heartbeat.set_state(hb, datetime.datetime.now(), colors)
        self.heartbeat.tick()
        if hb == "dead" and running and not self._dead_reported:
            self._dead_reported = True
            self._log_line("ERROR [GUI] 心跳中断：索引疑似卡死，请查看上方状态")
            self._snack("索引疑似卡死（心跳中断），请查看日志", is_error=True)
        if hb != "dead":
            self._dead_reported = False

        self.device.update(progress.get("device"), MODEL_NAME, files, chunks)

        self._update_last_chip(running, colors)

        busy = index_busy()
        self.progress.set_busy(busy, colors)

        new_lines, self._log_cursor = self.worker.lines_since(self._log_cursor)
        for line in new_lines:
            self._log_line(line)
        self.page.update()

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
            if ago < 3:
                self.kpi_time.set_sub("刚刚完成")
            else:
                self.kpi_time.set_sub("上次完成 %s" % self._last_completed_at.strftime("%H:%M"))
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

    def _start_index(self, full):
        if index_busy():
            self._snack("已有索引任务在运行", is_error=True)
            return
        if self.worker.start(full=full):
            self._snack("已开始全量重建" if full else "已开始增量重建")
            self._log_line("── 用户触发%s" % ("全量重建" if full else "增量重建"))
        else:
            self._snack("索引任务已在运行", is_error=True)

    def _confirm_full(self, e):
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("全量重建", size=18, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Text(
                "将清空并重新嵌入全部约 %d 个文本块，预计 3–5 分钟，期间检索可用性下降。\n是否继续？"
                % meta_stats()[1],
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

    def _toggle_theme(self, e):
        self.colors = LIGHT if self.colors is DARK else DARK
        self.page.theme_mode = (ft.ThemeMode.LIGHT if self.colors is LIGHT
                                else ft.ThemeMode.DARK)
        self.page.bgcolor = self.colors["base"]
        for w in (self.kpi_files, self.kpi_chunks, self.kpi_time,
                  self.status_card, self.heartbeat, self.progress,
                  self.search, self.log_view, self.device):
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

        def run():
            from retriever import hybrid_search
            return hybrid_search(query.strip(), include_body=True)

        def done(fut):
            self._searching = False
            self.search.btn.disabled = False
            try:
                result = fut.result()
                self.search.show_results(result, self.colors)
                self._log_line("── 检索「%s」耗时 %.1fs" % (query.strip(), time.time() - t0))
            except Exception as ex:
                self._log_line("ERROR [GUI] 搜索失败：%s" % ex)
                self._snack("搜索失败：%s" % ex, is_error=True)
            self.page.update()

        self.executor.submit(run).add_done_callback(lambda f: self.page.run_task(lambda: done(f)))

    # ---------- 工具 ----------

    def _open_vault(self, e):
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
    ft.run(main)
