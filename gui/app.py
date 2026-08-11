"""app.py — 语义搜索控制台主程序。

运行：cd obsidian-rag && .venv\\Scripts\\python gui\\app.py
功能：索引状态看板（KPI/进度/心跳/设备）、一键增量/全量重建、搜索测试、深浅主题。
架构：零侵入——复用现有 progress/meta 数据文件，索引跑独立子进程。
"""
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
    index_state, index_busy, meta_stats, progress_ratio,
    heartbeat_state, last_elapsed, read_progress,
)
from worker import IndexWorker, read_history  # noqa: E402
from widgets import (  # noqa: E402
    KpiCard, StatusCard, HeartbeatDot, ProgressCard, SearchCard, LogView, DeviceBar,
)

MODEL_NAME = CFG["model_name"]
VAULT_DIR = CFG["vault"]
LOG_DIR = str(Path(__file__).resolve().parent.parent / "data")


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.colors = DARK
        self.worker = IndexWorker()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._searching = False
        self._log_cursor = 0

        self._setup_window()
        self._build_widgets()
        self._build_layout()
        self.worker.on_output = self._on_worker_line
        self.worker.on_exit = self._on_worker_exit
        self.log_view.attach(page)
        self.log_view.load_history(read_history()[-300:])
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
        self.page.padding = SIZE["gap_card"]
        self.page.spacing = 0

    def _build_widgets(self):
        self.kpi_files = KpiCard("笔记文件")
        self.kpi_chunks = KpiCard("检索块数")
        self.kpi_time = KpiCard("最后索引耗时")
        self.status_card = StatusCard()
        self.heartbeat = HeartbeatDot()
        self.progress = ProgressCard(180)
        self.search = SearchCard(self._do_search)
        self.log_view = LogView()
        self.device = DeviceBar()
        self.theme_btn = ft.IconButton(
            icon=ft.Icons.DARK_MODE, icon_color=DARK["text_secondary"],
            tooltip="切换深浅色", on_click=self._toggle_theme,
        )
        self.btn_inc = ft.FilledButton(
            "增量重建",
            icon=ft.Icons.REFRESH,
            height=SIZE["btn_height"],
            style=ft.ButtonStyle(
                bgcolor=DARK["primary_fill"], color=DARK["on_primary"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                padding=ft.Padding.symmetric(horizontal=20),
                text_style=ft.TextStyle(font_family=FONT_UI),
            ),
            on_click=lambda e: self._start_index(False),
        )
        self.btn_full = ft.OutlinedButton(
            "全量重建…",
            icon=ft.Icons.WARNING_AMBER,
            height=SIZE["btn_height"],
            style=ft.ButtonStyle(
                color=DARK["danger"], side=ft.BorderSide(1, DARK["danger"] + "99"),
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                padding=ft.Padding.symmetric(horizontal=20),
                text_style=ft.TextStyle(font_family=FONT_UI),
            ),
            on_click=self._confirm_full,
        )
        self.btn_vault = ft.TextButton("打开 Vault 文件夹", icon=ft.Icons.FOLDER_OPEN,
                                       on_click=lambda e: self._open_dir(VAULT_DIR))
        self.btn_logs = ft.TextButton("打开日志目录", icon=ft.Icons.DESCRIPTION,
                                      on_click=lambda e: self._open_dir(LOG_DIR))
        for b in (self.btn_vault, self.btn_logs):
            b.style = ft.ButtonStyle(text_style=ft.TextStyle(font_family=FONT_UI))

    def _build_layout(self):
        kpi_row = ft.Row([
            self.kpi_files.card, self.kpi_chunks.card,
            self.kpi_time.card, self.status_card.card,
        ], spacing=SIZE["gap_card"])
        btn_row = ft.Row([
            self.btn_inc, self.btn_full, self.btn_vault, self.btn_logs,
        ], spacing=SIZE["gap_tight"])
        header = ft.Row([
            ft.Container(width=8, height=8, border_radius=4, bgcolor=DARK["primary"]),
            ft.Text("语义搜索控制台", size=16, weight=ft.FontWeight.W_600,
                    color=DARK["text_main"], font_family=FONT_UI),
            ft.Container(expand=True),
            self.theme_btn,
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER)
        mid = ft.Row([
            self.progress.card,
            ft.VerticalDivider(width=1, color=DARK["outline"]),
            self.search.card,
        ], spacing=SIZE["gap_card"], vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        header_row = ft.Row([
            ft.Text("索引进度", size=14, weight=ft.FontWeight.W_600,
                    color=DARK["text_main"], font_family=FONT_UI),
            self.heartbeat.dot,
            self.heartbeat.text,
            ft.Container(expand=True),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=SIZE["gap_tight"])

        self.page.add(
            header,
            ft.Container(height=SIZE["gap_card"]),
            kpi_row,
            ft.Container(height=SIZE["gap_card"]),
            btn_row,
            ft.Container(height=SIZE["gap_card"]),
            mid,
            ft.Container(height=SIZE["gap_card"]),
            self.device.card,
            ft.Container(height=SIZE["gap_card"]),
            self.log_view.card,
        )
        # 进度卡标题行插到最前（先于 chip 行）
        self.progress.card.content.controls.insert(0, header_row)

    # ---------- 主循环 ----------

    async def _refresh_loop(self):
        while True:
            try:
                self._refresh_once()
            except Exception as e:
                self._log_line("ERROR [GUI] 刷新失败：%s" % e)
            await asyncio_sleep_1s()

    def _refresh_once(self):
        colors = self.colors
        progress = read_progress()
        state, files, chunks = index_state()
        self.kpi_files.set_value(str(files) if files else "0")
        self.kpi_chunks.set_value(str(chunks) if chunks else "0")
        elapsed = last_elapsed(progress)
        self.kpi_time.set_value(("%.1fs" % elapsed) if elapsed else "—")
        self.status_card.set_state(state, colors)
        ratio = progress_ratio(progress)
        self.progress.update(progress, ratio, colors)
        self.heartbeat.set_state(heartbeat_state(progress), colors)
        self.heartbeat.tick()
        self.device.update(progress.get("device"), MODEL_NAME, files, chunks)
        self.search.set_banner(progress.get("running"))
        busy = index_busy()
        self.btn_inc.disabled = busy
        self.btn_full.disabled = busy
        new_lines, self._log_cursor = self.worker.lines_since(self._log_cursor)
        for line in new_lines:
            self._log_line(line)
        self.page.update()

    def _on_worker_line(self, line):
        pass  # 仅缓冲，UI 由刷新循环统一提交

    def _on_worker_exit(self, rc):
        if rc != 0:
            self._snack("重建失败，请查看底部日志", is_error=True)

    def _log_line(self, line):
        self.log_view.append(line)

    # ---------- 交互 ----------

    def _start_index(self, full):
        if index_busy():
            self._snack("已有索引任务在运行", is_error=True)
            return
        if self.worker.start(full=full):
            self._snack("已开始全量重建（约 40 秒）" if full else "已开始增量重建（约 10 秒）")
        else:
            self._snack("索引任务已在运行")

    def _confirm_full(self, e):
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认全量重建？", size=18, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Text(
                "将删除并重新嵌入全部约 %d 个块，预计耗时约 40 秒。\n重建期间旧结果仍可查看，但可能不是最新。"
                % meta_stats()[1],
                size=14, font_family=FONT_UI,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton(
                    "确认重建",
                    style=ft.ButtonStyle(bgcolor=self.colors["danger"],
                                         color="#FFFFFF",
                                         shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"])),
                    on_click=lambda _: (self._close_dialog(dlg), self._start_index(True)),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _close_dialog(self, dlg):
        dlg.open = False
        dlg.update()

    def _toggle_theme(self, e):
        self.colors = LIGHT if self.colors is DARK else DARK
        self.page.theme_mode = (ft.ThemeMode.LIGHT if self.colors is LIGHT
                                else ft.ThemeMode.DARK)
        for w in (self.kpi_files, self.kpi_chunks, self.kpi_time,
                  self.status_card, self.heartbeat, self.progress,
                  self.search, self.log_view, self.device):
            w.apply(self.colors)
        self.theme_btn.icon = (ft.Icons.LIGHT_MODE if self.colors is LIGHT
                               else ft.Icons.DARK_MODE)
        self.page.update()

    def _do_search(self, query):
        if not query or not query.strip():
            self._snack("请输入问题")
            return
        if self._searching:
            return
        self._searching = True
        self.search.btn.disabled = True
        self.page.update()

        def run():
            from retriever import hybrid_search
            return hybrid_search(query.strip(), include_body=True)

        def done(fut):
            self._searching = False
            self.search.btn.disabled = False
            try:
                result = fut.result()
                self.search.show_results(result, self.colors)
            except Exception as ex:
                self._log_line("ERROR [GUI] 搜索失败：%s" % ex)
                self._snack("搜索失败：%s" % ex, is_error=True)
            self.page.update()

        self.executor.submit(run).add_done_callback(lambda f: self.page.run_task(lambda: done(f)))

    # ---------- 工具 ----------

    def _open_dir(self, path):
        try:
            os.startfile(path)
        except OSError as ex:
            self._snack("无法打开文件夹：%s" % ex, is_error=True)
            self._log_line("ERROR [GUI] 打开目录失败：%s" % ex)

    def _snack(self, msg, is_error=False):
        self.page.show_dialog(ft.SnackBar(
            content=ft.Text(msg, font_family=FONT_UI),
            bgcolor=self.colors["error_bg"] if is_error else self.colors["surface_high"],
        ))


async def asyncio_sleep_1s():
    import asyncio
    await asyncio.sleep(1.0)


def main(page: ft.Page):
    App(page)


if __name__ == "__main__":
    ft.run(main)
