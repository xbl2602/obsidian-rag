"""widgets.py — 控制台组件：KPI 卡、状态卡、心跳灯、进度卡、日志区、设备条。

所有组件持有内部控件引用，通过 apply(colors) 切换深浅主题；update_* 更新数值。
"""
import re

import flet as ft

from theme import DARK, FONT_MONO, FONT_UI, SIZE


def card_container(content, colors, bg=None, radius=None):
    """统一样式的卡片容器（深色描边 / 浅色淡投影）。"""
    radius = radius if radius is not None else SIZE["radius_card"]
    if colors is DARK:
        return ft.Container(
            content=content,
            bgcolor=bg or colors["surface"],
            border=ft.Border.all(1, colors["outline"]),
            border_radius=radius,
            padding=SIZE["pad_card"],
        )
    return ft.Container(
        content=content,
        bgcolor=bg or colors["surface"],
        border_radius=radius,
        padding=SIZE["pad_card"],
        shadow=ft.BoxShadow(blur_radius=6, spread_radius=0, color="#0F000000"),
    )


class KpiCard:
    """KPI 大数字卡：上行标签 + 下行大数字（等宽字体）。"""

    def __init__(self, label, value="—"):
        self.label = ft.Text(label, size=12, color="#888888", font_family=FONT_UI)
        self.value = ft.Text(value, size=32, weight=ft.FontWeight.W_600,
                             font_family=FONT_MONO, color=DARK["text_main"])
        content = ft.Column(
            [self.label, ft.Container(height=SIZE["gap_tight"]), self.value],
            spacing=0,
            alignment=ft.MainAxisAlignment.CENTER,
        )
        self.card = card_container(content, DARK)
        self.card.height = SIZE["kpi_height"]

    def set_value(self, text, color=None):
        self.value.value = text
        if color:
            self.value.color = color

    def apply(self, colors):
        self.value.color = colors["text_main"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border.all(1, colors["outline"])
        self.card.shadow = None if colors is DARK else ft.BoxShadow(blur_radius=6, color="#0F000000")


class StatusCard:
    """索引状态卡：状态色点 + 主文案 + 副文案（三态）。"""

    TEXT = {
        "ok": ("索引最新", "所有块均可直接搜索"),
        "stale": ("有变更待索引", "检测到新/改笔记，建议增量重建"),
        "none": ("尚未索引", "点击增量重建开始首次索引（约 40 秒）"),
    }

    def __init__(self):
        self.dot = ft.Container(width=SIZE["dot_status"], height=SIZE["dot_status"],
                                border_radius=SIZE["dot_status"] // 2, bgcolor=DARK["disabled"])
        self.title = ft.Text("尚未索引", size=14, weight=ft.FontWeight.W_500,
                             color=DARK["text_main"], font_family=FONT_UI)
        self.sub = ft.Text("", size=12, color=DARK["text_secondary"], font_family=FONT_UI)
        content = ft.Row([
            self.dot,
            ft.Column([self.title, self.sub], spacing=2, expand=True),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12)
        self.card = card_container(content, DARK)
        self.card.height = SIZE["kpi_height"]

    def set_state(self, state, colors):
        self.dot.bgcolor = {
            "ok": colors["success"],
            "stale": colors["warning"],
            "none": colors["disabled"],
        }[state]
        main, sub = self.TEXT[state]
        self.title.value = main
        self.sub.value = sub

    def apply(self, colors):
        self.title.color = colors["text_main"]
        self.sub.color = colors["text_secondary"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border.all(1, colors["outline"])
        self.card.shadow = None if colors is DARK else ft.BoxShadow(blur_radius=6, color="#0F000000")


class HeartbeatDot:
    """心跳灯：圆点 + 文案。运行中呼吸（透明度摆动）。"""

    TEXT = {
        "running": "心跳正常",
        "dead": "疑似卡死，请查看日志",
        "stalled": "进度停滞，疑似卡死",
        "done": "索引完成",
        "idle": "等待任务",
    }
    COLOR = {
        "running": "success",
        "dead": "danger",
        "stalled": "warning",
        "done": "success",
        "idle": "disabled",
    }

    def __init__(self):
        self.dot = ft.Container(width=SIZE["dot_heartbeat"], height=SIZE["dot_heartbeat"],
                                border_radius=SIZE["dot_heartbeat"] // 2,
                                bgcolor=DARK["disabled"], animate_opacity=200)
        self.text = ft.Text(self.TEXT["idle"], size=12, color=DARK["text_secondary"],
                            font_family=FONT_UI)
        self._breathing = False
        self._low = False

    def set_state(self, state, colors):
        self.dot.bgcolor = colors[self.COLOR[state]]
        self.text.value = self.TEXT[state]
        if state == "running" and not self._breathing:
            self._breathing = True
            self._low = False
            self.dot.opacity = 1.0
        elif state != "running":
            self._breathing = False
            self.dot.opacity = 1.0

    def tick(self):
        """呼吸循环：运行中每 1s 摆一次透明度。"""
        if self._breathing:
            self._low = not self._low
            self.dot.opacity = 0.4 if self._low else 1.0
            return True
        return False

    def apply(self, colors):
        self.text.color = colors["text_secondary"]


PHASE_TEXT = {"scanning": "扫描", "embedding": "嵌入", "writing": "写库", "done": "完成"}
PHASE_COLOR = {"scanning": "scan", "embedding": "primary", "writing": "primary", "done": "success"}


class ProgressCard:
    """索引进度卡：阶段 chip + 进度条 + 信息行 + ETA。"""

    def __init__(self, width):
        self.phase = ft.Text("—", size=12, weight=ft.FontWeight.W_500,
                             color=DARK["primary"], font_family=FONT_UI)
        self.chip = ft.Container(
            content=self.phase,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=SIZE["radius_pill"],
            bgcolor="#114DB6AC",
        )
        self.info = ft.Text("等待索引任务", size=13, color=DARK["text_secondary"], font_family=FONT_UI)
        self.pct = ft.Text("—", size=20, weight=ft.FontWeight.W_600, color=DARK["primary"],
                           font_family=FONT_MONO)
        self.bar = ft.ProgressBar(value=0.0, height=SIZE["bar_height"],
                                  border_radius=4, color=DARK["primary"],
                                  bgcolor=DARK["surface_high"])
        self.eta = ft.Text("", size=12, color=DARK["text_weak"], font_family=FONT_UI)
        content = ft.Column([
            ft.Row([self.chip, ft.Container(expand=True), self.info], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.bar,
            ft.Row([self.eta, ft.Container(expand=True), self.pct], vertical_alignment=ft.CrossAxisAlignment.END),
        ], spacing=SIZE["gap_tight"], alignment=ft.MainAxisAlignment.CENTER, expand=True)
        self.card = card_container(content, DARK, bg=DARK["surface"])
        self.card.expand = True
        self.card.height = width if width else None

    def update(self, progress, ratio, colors):
        phase = progress.get("phase") or "—"
        self.phase.value = PHASE_TEXT.get(phase, phase)
        self.chip.bgcolor = colors[PHASE_COLOR.get(phase, "disabled")] + "1F"
        self.bar.value = ratio
        self.bar.color = colors[PHASE_COLOR.get(phase, "disabled")]
        if phase == "embedding":
            done, total = progress.get("chunks_done"), progress.get("chunks_total")
            self.info.value = "已嵌入 %s/%s 块" % (done, total) if done is not None else "嵌入中…"
        elif phase == "scanning":
            done, total = progress.get("files_done"), progress.get("files_total")
            self.info.value = "已处理 %s/%s 个文件" % (done, total) if done is not None else "扫描中…"
        else:
            self.info.value = progress.get("message") or ("完成" if phase == "done" else "准备中…")
        self.pct.value = "%d%%" % int(ratio * 100) if ratio else "—"
        eta = progress.get("eta_s")
        self.eta.value = "预计剩余 %s 秒" % int(eta) if isinstance(eta, (int, float)) else ""

    def apply(self, colors):
        self.info.color = colors["text_secondary"]
        self.pct.color = colors["primary"]
        self.eta.color = colors["text_weak"]
        self.bar.bgcolor = colors["surface_high"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border.all(1, colors["outline"])
        self.card.shadow = None if colors is DARK else ft.BoxShadow(blur_radius=6, color="#0F000000")


class SearchCard:
    """搜索测试卡：输入框 + 结果列表（排名徽章 + 块文本 + 来源路径）。"""

    def __init__(self, on_search):
        self.input = ft.TextField(
            hint_text="输入问题，如：火箭发动机的原理",
            height=SIZE["input_height"],
            border_radius=SIZE["radius_control"],
            filled=True,
            dense=True,
            on_submit=lambda e: on_search(e.control.value),
        )
        self.btn = ft.IconButton(icon=ft.Icons.SEARCH, icon_color=DARK["primary"],
                                 tooltip="搜索", on_click=lambda e: on_search(self.input.value))
        self.banner = ft.Container(
            content=ft.Text("索引进行中，结果可能基于旧数据", size=13, color=DARK["primary"],
                            font_family=FONT_UI),
            bgcolor="#114DB6AC", border_radius=SIZE["radius_control"],
            padding=10, visible=False,
        )
        self.results = ft.ListView(spacing=SIZE["gap_tight"], auto_scroll=False, expand=True)
        content = ft.Column([
            ft.Row([self.input, self.btn], spacing=SIZE["gap_tight"]),
            self.banner,
            self.results,
        ], spacing=SIZE["gap_tight"], expand=True)
        self.card = card_container(content, DARK, bg=DARK["surface"])
        self.card.expand = True

    def show_empty(self, colors):
        self.results.controls = [
            ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.SEARCH_OFF, color=colors["disabled"], size=28),
                    ft.Text("未找到相关内容", size=14, color=colors["text_main"], font_family=FONT_UI),
                    ft.Text("换个问法，或缩小问题范围", size=12, color=colors["text_secondary"], font_family=FONT_UI),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                alignment=ft.alignment.center, expand=True, padding=24,
            )
        ]

    def show_results(self, text, colors):
        """解析 retriever 输出（src 行 + 正文行 + --- 分隔），渲染结果列表。"""
        blocks = []
        cur = {"src": None, "body": []}
        for line in text.splitlines():
            if not line.strip():
                continue
            if line == "---":
                if cur["src"] is not None:
                    blocks.append(cur)
                cur = {"src": None, "body": []}
            elif cur["src"] is None:
                cur["src"] = line
            else:
                cur["body"].append(line)
        if cur["src"] is not None:
            blocks.append(cur)
        if not blocks:
            self.show_empty(colors)
            return
        controls = []
        for i, b in enumerate(blocks[:5]):
            badge = ft.Container(
                content=ft.Text(str(i + 1), size=11, weight=ft.FontWeight.W_600, color="#FFFFFF",
                                font_family=FONT_MONO),
                width=20, height=20, border_radius=10, bgcolor=colors["primary"],
                alignment=ft.alignment.center,
            )
            body = "\n".join(b["body"][:3])
            controls.append(ft.Container(
                content=ft.Row([
                    badge,
                    ft.Column([
                        ft.Text(body, size=13, font_family=FONT_UI, color=colors["text_main"],
                                height=1.5, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(b["src"], size=11, font_family=FONT_MONO, color=colors["text_weak"]),
                    ], spacing=2, expand=True),
                ], vertical_alignment=ft.CrossAxisAlignment.START, spacing=10),
                padding=12, border_radius=SIZE["radius_control"], bgcolor=colors["surface_high"],
            ))
        if len(blocks) > 5:
            controls.append(ft.Text("还有 %d 条，可缩小问题范围" % (len(blocks) - 5),
                                    size=12, color=colors["text_weak"], font_family=FONT_UI))
        self.results.controls = controls

    def set_banner(self, visible):
        self.banner.visible = visible

    def apply(self, colors):
        self.input.border_color = colors["outline"]
        self.input.fill_color = colors["surface_high"]
        self.btn.icon_color = colors["primary"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border.all(1, colors["outline"])
        self.card.shadow = None if colors is DARK else ft.BoxShadow(blur_radius=6, color="#0F000000")
        self.banner.bgcolor = colors["primary"] + "1F"


class LogView:
    """日志区：自动滚动、行着色（ERROR 红 / WARNING 橙）、复制/清空。"""

    def __init__(self):
        self.list = ft.ListView(auto_scroll=True, spacing=0, expand=True, padding=8)
        self.header = ft.Text("实时日志", size=13, weight=ft.FontWeight.W_600,
                              color=DARK["text_main"], font_family=FONT_UI)
        self.count = ft.Text("", size=12, color=DARK["text_weak"], font_family=FONT_UI)
        self.copy_btn = ft.TextButton("复制", icon=ft.Icons.COPY, on_click=self._copy)
        self.clear_btn = ft.TextButton("清空", icon=ft.Icons.DELETE_OUTLINE, on_click=self._clear)
        content = ft.Column([
            ft.Row([self.header, ft.Container(expand=True), self.count, self.copy_btn, self.clear_btn],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(content=self.list, bgcolor=DARK["log_bg"],
                         border_radius=SIZE["radius_control"],
                         border=ft.Border.all(1, DARK["outline"]), expand=True,
                         padding=ft.Padding.symmetric(horizontal=4, vertical=4)),
        ], spacing=4, expand=True)
        self.card = card_container(content, DARK, bg=DARK["surface"])
        self.card.expand = True
        self.card.height = 0  # 由外层控制剩余高度
        self._colors = DARK
        self._page = None

    def attach(self, page):
        self._page = page

    def append(self, line):
        color = DARK["text_main"]
        if "ERROR" in line:
            color = DARK["danger"]
        elif "WARNING" in line or "WARN" in line:
            color = DARK["warning"]
        self.list.controls.append(ft.Text(line, size=12, font_family=FONT_MONO,
                                          color=color, height=1.45))
        total = len(self.list.controls)
        if total > SIZE["log_max_lines"]:
            del self.list.controls[: total - SIZE["log_max_lines"]]
        self.count.value = "仅显示最近 %d 行" % SIZE["log_max_lines"]
        if self._page:
            self._page.update()

    def load_history(self, lines):
        self.list.controls = []
        for line in lines[-500:]:
            self.append(line)

    def _copy(self, e):
        if self._page:
            text = "\n".join(c.value for c in self.list.controls)
            self._page.clipboard.set_text(text)

    def _clear(self, e):
        self.list.controls = []
        if self._page:
            self._page.update()

    def apply(self, colors):
        self._colors = colors
        self.header.color = colors["text_main"]
        self.count.color = colors["text_weak"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border.all(1, colors["outline"])
        self.card.shadow = None if colors is DARK else ft.BoxShadow(blur_radius=6, color="#0F000000")


class DeviceBar:
    """设备信息条：设备 / 模型 / 数据规模。"""

    def __init__(self):
        self.items = [
            ft.Text("设备: —", size=13, color=DARK["text_secondary"], font_family=FONT_MONO),
            ft.VerticalDivider(width=1, color=DARK["outline"]),
            ft.Text("模型: —", size=13, color=DARK["text_secondary"], font_family=FONT_MONO),
            ft.VerticalDivider(width=1, color=DARK["outline"]),
            ft.Text("—", size=13, color=DARK["text_secondary"], font_family=FONT_MONO),
        ]
        content = ft.Row(self.items, spacing=SIZE["gap_tight"])
        self.card = ft.Container(
            content=content,
            padding=ft.Padding.symmetric(horizontal=SIZE["pad_card"]),
            height=SIZE["device_bar_height"],
            bgcolor=DARK["surface"],
            border=ft.Border.all(1, DARK["outline"]),
            border_radius=SIZE["radius_card"],
            alignment=ft.Alignment.CENTER_LEFT,
        )

    def update(self, device, model, files, chunks):
        self.items[0].value = "设备: %s" % (device or "—")
        self.items[2].value = "模型: %s" % (model or "—")
        self.items[4].value = "已索引 %d 文件 / %d 块" % (files, chunks)

    def apply(self, colors):
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border.all(1, colors["outline"])
        for item in self.items:
            if isinstance(item, ft.Text):
                item.color = colors["text_secondary"]
            else:
                item.color = colors["outline"]
