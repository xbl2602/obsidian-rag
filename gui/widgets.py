"""widgets.py — 控制台组件：KPI 卡、心跳胶囊、进度卡、搜索卡、日志区、设备条。

布局纪律：所有卡片都有确定高度锚点（固定 px 或父级锚定），
禁止"自适应容器内套 expand 子项"导致高度链归零。
"""
import datetime
import re

import flet as ft

from theme import DARK, FONT_MONO, FONT_NUM, FONT_UI, SIZE


def card_container(content, colors, bg=None, radius=None, pad=None):
    """面板卡：圆角 14 + 1px 描边 + 上缘高光 + 柔和投影。"""
    radius = radius if radius is not None else SIZE["radius_panel"]
    return ft.Container(
        content=content,
        bgcolor=bg or colors["surface"],
        border=ft.Border(
            top=ft.BorderSide(1, colors["border_light"]),
            bottom=ft.BorderSide(1, colors["border"]),
            left=ft.BorderSide(1, colors["border"]),
            right=ft.BorderSide(1, colors["border"]),
        ),
        border_radius=radius,
        padding=pad if pad is not None else SIZE["pad"],
        shadow=ft.BoxShadow(blur_radius=20, offset=ft.Offset(0, 4),
                            color=ft.Colors.with_opacity(0.25, "#000000")),
    )


class KpiCard:
    """KPI 卡：图标+标签行 / 30px 大数字 / accent 短横线 / 可选副行。"""

    def __init__(self, label, icon, colors=DARK):
        self.icon = ft.Icon(icon, size=14, color=colors["t3"])
        self.label = ft.Text(label, size=12, color=colors["t3"], font_family=FONT_UI)
        self.value = ft.Text("—", size=SIZE["kpi_num"], weight=ft.FontWeight.W_700,
                             font_family=FONT_NUM, color=colors["t1"], height=1.0)
        self.rule = ft.Container(width=24, height=2, border_radius=1,
                                 bgcolor=ft.Colors.with_opacity(0.25, colors["accent"]))
        self.sub = ft.Text("", size=11, color=colors["t4"], font_family=FONT_UI,
                           visible=False)
        content = ft.Column([
            ft.Row([self.icon, self.label], spacing=6),
            ft.Container(height=6),
            self.value,
            ft.Container(height=5),
            self.rule,
            ft.Container(height=4),
            self.sub,
        ], spacing=0, expand=True, horizontal_alignment=ft.CrossAxisAlignment.START)
        self.card = card_container(content, colors, radius=SIZE["radius_kpi"])
        self.card.height = SIZE["kpi_h"]
        self.card.on_hover = self._on_hover
        self._colors = colors

    def _on_hover(self, e):
        if e.data == "true":
            self.card.bgcolor = self._colors["hover"]
        else:
            self.card.bgcolor = self._colors["surface"]

    def set_value(self, text, color=None):
        self.value.value = text
        if color:
            self.value.color = color

    def set_sub(self, text):
        self.sub.value = text
        self.sub.visible = bool(text)

    def apply(self, colors):
        self._colors = colors
        self.icon.color = colors["t3"]
        self.label.color = colors["t3"]
        self.value.color = colors["t1"]
        self.rule.bgcolor = ft.Colors.with_opacity(0.25, colors["accent"])
        self.sub.color = colors["t4"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border(
            top=ft.BorderSide(1, colors["border_light"]),
            bottom=ft.BorderSide(1, colors["border"]),
            left=ft.BorderSide(1, colors["border"]),
            right=ft.BorderSide(1, colors["border"]),
        )


class StatusCard:
    """KPI 状态卡：状态色点 + 主文案 + 副文案（三态）。"""

    TEXT = {
        "ok": ("索引最新", "所有块均可直接搜索"),
        "stale": ("有变更待索引", "检测到新/改笔记，建议重建"),
        "none": ("尚未索引", "点击增量重建开始首次索引"),
    }
    COLOR = {"ok": "success", "stale": "warning", "none": "t4"}

    def __init__(self, colors=DARK):
        self.dot = ft.Container(width=SIZE["dot"], height=SIZE["dot"],
                                border_radius=SIZE["dot"] // 2, bgcolor=colors["t4"])
        self.title = ft.Text(self.TEXT["none"][0], size=14, weight=ft.FontWeight.W_600,
                             color=colors["t1"], font_family=FONT_UI)
        self.sub = ft.Text("", size=11, color=colors["t4"], font_family=FONT_UI)
        self.rule = ft.Container(width=24, height=2, border_radius=1,
                                 bgcolor=ft.Colors.with_opacity(0.25, colors["accent"]))
        content = ft.Column([
            ft.Row([self.dot, self.title], spacing=8),
            ft.Container(height=8),
            self.sub,
            ft.Container(expand=True),
            self.rule,
        ], spacing=0, expand=True)
        self.card = card_container(content, colors, radius=SIZE["radius_kpi"])
        self.card.height = SIZE["kpi_h"]
        self._colors = colors

    def set_state(self, state, colors):
        self.dot.bgcolor = colors[self.COLOR[state]]
        main, sub = self.TEXT[state]
        self.title.value = main
        self.sub.value = sub

    def apply(self, colors):
        self._colors = colors
        self.title.color = colors["t1"]
        self.sub.color = colors["t4"]
        self.rule.bgcolor = ft.Colors.with_opacity(0.25, colors["accent"])
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border(
            top=ft.BorderSide(1, colors["border_light"]),
            bottom=ft.BorderSide(1, colors["border"]),
            left=ft.BorderSide(1, colors["border"]),
            right=ft.BorderSide(1, colors["border"]),
        )


class HeartbeatPill:
    """心跳胶囊（header 右上）：圆点 + 状态文案 + 时间戳，运行中呼吸。"""

    TEXT = {
        "running": "心跳正常",
        "dead": "心跳中断",
        "stalled": "心跳异常 · 进度未动",
        "done": "索引完成",
        "idle": "空闲 · 等待任务",
    }
    COLOR = {
        "running": "accent",
        "dead": "danger",
        "stalled": "warning",
        "done": "success",
        "idle": "t4",
    }

    def __init__(self, colors=DARK):
        self.dot = ft.Container(width=SIZE["dot"], height=SIZE["dot"],
                                border_radius=SIZE["dot"] // 2, bgcolor=colors["t4"])
        self.text = ft.Text(self.TEXT["idle"], size=12, weight=ft.FontWeight.W_600,
                            color=colors["t2"], font_family=FONT_UI)
        self.ts = ft.Text("", size=11, color=colors["t3"], font_family=FONT_MONO)
        content = ft.Row([self.dot, self.text, self.ts], spacing=8,
                         vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.card = ft.Container(
            content=content,
            height=SIZE["pill_h"],
            padding=ft.Padding.symmetric(horizontal=SIZE["pill_pad_x"]),
            border_radius=SIZE["radius_pill"],
            bgcolor=ft.Colors.with_opacity(0.08, colors["t4"]),
            border=ft.Border.all(1, colors["border_faint"]),
        )
        self._breathing = False
        self._low = False
        self._colors = colors

    def set_state(self, state, now, colors):
        self.text.value = self.TEXT[state]
        if state == "running":
            self.ts.value = now.strftime("%H:%M:%S")
        elif state in ("done", "dead", "stalled"):
            self.ts.value = now.strftime("%H:%M:%S")
        else:
            self.ts.value = ""
        self.dot.bgcolor = colors[self.COLOR[state]]
        c = colors[self.COLOR[state]]
        if state == "idle":
            self.card.bgcolor = ft.Colors.with_opacity(0.08, colors["t4"])
            self.card.border = ft.Border.all(1, colors["border_faint"])
            self._breathing = False
            self.dot.opacity = 1.0
        else:
            self.card.bgcolor = ft.Colors.with_opacity(0.12, c)
            self.card.border = ft.Border.all(1, c)
            if state == "running" and not self._breathing:
                self._breathing = True
                self._low = False
                self.dot.opacity = 1.0
            elif state != "running":
                self._breathing = False
                self.dot.opacity = 1.0

    def tick(self):
        """呼吸循环（由 1s 主循环驱动）：运行中摆一次透明度。"""
        if self._breathing:
            self._low = not self._low
            self.dot.opacity = 0.4 if self._low else 1.0
            return True
        return False

    def apply(self, colors):
        self.text.color = colors["t2"]
        self.ts.color = colors["t3"]
        self.card.bgcolor = ft.Colors.with_opacity(0.08, colors["t4"])
        self.card.border = ft.Border.all(1, colors["border_faint"])


PHASES = ["scanning", "embedding", "writing", "done"]
PHASE_TEXT = {"scanning": "扫描", "embedding": "嵌入", "writing": "写库", "done": "完成"}
PHASE_COLOR = {"scanning": "scan", "embedding": "accent", "writing": "accent", "done": "success"}


def _parse_src(src):
    """从检索源行解析 (相对路径, 标题, 置信度)。

    源行格式：[来源] docs/foo.md (## 小节标题) [块 1/3] [置信度 0.87]
    - rel：Vault 内相对路径（正斜杠），用于 obsidian://open 定位；
    - heading：标题（若有），用于 # 锚点跳转；无法解析时回退整行作 rel；
    - conf：0-1 归一化置信度（无则 None）。
    """
    s = src.strip()
    if s.startswith("[来源] "):
        s = s[len("[来源] "):]
    conf = None
    m = re.search(r"\[置信度 ([\d.]+)\]", s)
    if m:
        try:
            conf = float(m.group(1))
        except ValueError:
            conf = None
        s = s[:m.start()].rstrip()
    body = s
    heading = ""
    sep = body.find(" (## ")
    if sep != -1:
        rel = body[:sep].strip()
        rest = body[sep:]
        close = rest.rfind(")")
        if close != -1:
            heading = rest[4:close].strip()
        return rel, heading, conf
    if " [" in body:
        rel = body.split(" [")[0].strip()
        return rel, heading, conf
    return body.split(" (##")[0].strip(), heading, conf


class ProgressCard:
    """索引构建卡：阶段 stepper + 36px 百分比 + 进度条 + 双行计数/计时 + 操作按钮。"""

    def __init__(self, colors=DARK):
        self.title = ft.Text("索引构建", size=15, weight=ft.FontWeight.W_600,
                             color=colors["t1"], font_family=FONT_UI)
        self.steppers = []
        stepper_row = ft.Row([], spacing=6)
        for ph in PHASES:
            chip = self._make_chip(ph, colors)
            self.steppers.append(chip)
            stepper_row.controls.append(chip)

        self.pct = ft.Text("—", size=SIZE["pct_num"], weight=ft.FontWeight.W_700,
                           font_family=FONT_NUM, color=colors["t3"], height=1.0)
        self.timer = ft.Text("", size=11, color=colors["t3"], font_family=FONT_MONO)
        self.eta = ft.Text("", size=11, color=colors["t2"], font_family=FONT_MONO)
        self.bar = ft.ProgressBar(value=0.0, height=SIZE["bar_h"], border_radius=4,
                                  color=colors["accent"], bgcolor=colors["sunken"])
        self.count = ft.Text("", size=11, color=colors["t3"], font_family=FONT_MONO)

        self.btn_inc = ft.FilledButton(
            "增量重建", icon=ft.Icons.REFRESH, height=SIZE["input_h"],
            style=ft.ButtonStyle(
                bgcolor=colors["accent"], color=colors["on_accent"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                padding=ft.Padding.symmetric(horizontal=18),
                text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600),
            ),
        )
        self.btn_full = ft.OutlinedButton(
            "全量重建", icon=ft.Icons.RESTART_ALT, height=SIZE["input_h"],
            style=ft.ButtonStyle(
                color=colors["danger"],
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, colors["danger"])),
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                padding=ft.Padding.symmetric(horizontal=18),
                text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600),
            ),
        )
        self.last_chip = ft.Container(
            content=ft.Text("", size=11, color=colors["t3"], font_family=FONT_MONO),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=SIZE["radius_pill"],
            bgcolor=ft.Colors.with_opacity(0.08, colors["t4"]),
        )

        content = ft.Column([
            self.title,
            stepper_row,
            ft.Row([
                self.pct,
                ft.Column([self.timer, self.eta], spacing=2, expand=True),
            ], vertical_alignment=ft.CrossAxisAlignment.END),
            self.bar,
            self.count,
            ft.Container(expand=True),
            ft.Row([self.btn_inc, self.btn_full, ft.Container(expand=True),
                    self.last_chip], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=SIZE["gap_tight"], expand=True)
        self.card = card_container(content, colors)
        self.card.expand = True
        self._colors = colors

    @staticmethod
    def _make_chip(ph, colors):
        return ft.Container(
            content=ft.Row([
                ft.Text(PHASE_TEXT[ph], size=11, weight=ft.FontWeight.W_600,
                        color=colors["t3"], font_family=FONT_UI),
            ], spacing=4),
            height=SIZE["chip_h"],
            padding=ft.Padding.symmetric(horizontal=8),
            border_radius=SIZE["radius_badge"],
            bgcolor=ft.Colors.with_opacity(0.06, colors["t4"]),
        )

    def set_phase(self, phase, colors):
        idx = PHASES.index(phase) if phase in PHASES else -1
        for i, chip in enumerate(self.steppers):
            if i < idx:
                self._style_chip(chip, colors, "success", done=True)
            elif i == idx:
                self._style_chip(chip, colors, PHASE_COLOR[phase], current=True)
            else:
                self._style_chip(chip, colors, "t4", pending=True)

    @staticmethod
    def _style_chip(chip, colors, key, done=False, current=False, pending=False):
        text = chip.content.controls[0]
        if done:
            text.color = colors["success"]
            chip.bgcolor = ft.Colors.with_opacity(0.12, colors["success"])
        elif current:
            text.color = colors["on_accent"]
            chip.bgcolor = colors[key]
        else:
            text.color = colors["t3"]
            chip.bgcolor = ft.Colors.with_opacity(0.06, colors["t4"])

    def update(self, progress, ratio, colors, elapsed_txt, eta_txt):
        phase = progress.get("phase") or "idle"
        self.set_phase(phase, colors)
        self.bar.value = ratio
        self.bar.color = colors[PHASE_COLOR.get(phase, "accent")]
        if progress.get("running"):
            self.pct.value = "%d%%" % int(ratio * 100) if ratio else "…"
            self.pct.color = colors[PHASE_COLOR.get(phase, "accent")]
        elif phase == "done":
            self.pct.value = "%d%%" % int(ratio * 100)
            self.pct.color = colors["success"]
        else:
            self.pct.value = "—"
            self.pct.color = colors["t3"]
        self.timer.value = elapsed_txt
        self.eta.value = eta_txt
        if phase == "embedding":
            done, total = progress.get("chunks_done"), progress.get("chunks_total")
            self.count.value = "块 %s/%s" % (done, total) if done is not None else ""
        else:
            done, total = progress.get("files_done"), progress.get("files_total")
            f = progress.get("chunks_done"), progress.get("chunks_total")
            if phase == "scanning" and done is not None:
                self.count.value = "文件 %s/%s" % (done, total)
            elif f[0] is not None and total:
                self.count.value = "文件 %s/%s · 块 %s/%s" % (done, total, f[0], f[1])
            else:
                self.count.value = "文件 %s/%s" % (done, total) if done is not None else ""

    def set_last(self, text, colors, running=False):
        self.last_chip.content.value = text
        self.last_chip.bgcolor = (ft.Colors.with_opacity(0.12, colors["accent"])
                                  if running else ft.Colors.with_opacity(0.08, colors["t4"]))

    def set_busy(self, busy, colors):
        self.btn_inc.disabled = busy
        self.btn_full.disabled = busy

    def apply(self, colors):
        self.title.color = colors["t1"]
        self.timer.color = colors["t3"]
        self.eta.color = colors["t2"]
        self.count.color = colors["t3"]
        self.bar.bgcolor = colors["sunken"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border(
            top=ft.BorderSide(1, colors["border_light"]),
            bottom=ft.BorderSide(1, colors["border"]),
            left=ft.BorderSide(1, colors["border"]),
            right=ft.BorderSide(1, colors["border"]),
        )


class SearchCard:
    """语义检索卡：输入行(44) + 状态条(20) + 结果列表（三层，父卡高锚定）。

    on_open(rel, heading)：点击结果项时回调（跳转源文件）。
    """

    TOP_K_CHOICES = (3, 5, 8, 10, 15)

    def __init__(self, on_search, on_open=None, colors=DARK):
        self._on_open = on_open
        self.title = ft.Text("语义检索", size=15, weight=ft.FontWeight.W_600,
                             color=colors["t1"], font_family=FONT_UI)
        self.input = ft.TextField(
            hint_text="输入问题，如：如何配置笔记同步插件？",
            height=SIZE["input_h"],
            border_radius=SIZE["radius_control"],
            filled=True,
            fill_color=colors["sunken"],
            border_color=colors["border"],
            dense=True,
            on_submit=lambda e: on_search(e.control.value),
        )
        self.btn = ft.FilledButton(
            "搜索", icon=ft.Icons.SEARCH, height=SIZE["input_h"],
            style=ft.ButtonStyle(
                bgcolor=colors["accent"], color=colors["on_accent"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600),
            ),
            on_click=lambda e: on_search(self.input.value),
        )
        self.top_k = ft.Dropdown(
            value="5",
            options=[ft.DropdownOption(key=str(k), text=str(k))
                     for k in self.TOP_K_CHOICES],
            width=80, height=SIZE["input_h"],
            label="Top K",
            label_style=ft.TextStyle(size=11, font_family=FONT_UI),
            filled=True, fill_color=colors["sunken"],
            border_color=colors["border"],
            dense=True,
            text_style=ft.TextStyle(size=13, font_family=FONT_UI),
        )
        self.body_switch = ft.Switch(
            value=True,
            label="展开正文",
            label_text_style=ft.TextStyle(size=12, font_family=FONT_UI),
            active_color=colors["accent"],
            inactive_thumb_color=colors["t3"],
        )
        self.status = ft.Text("输入问题开始测试检索", size=11, color=colors["t4"],
                              font_family=FONT_UI)
        self.ring = ft.ProgressRing(width=12, height=12, stroke_width=2,
                                    color=colors["accent"], visible=False)
        self.status_row = ft.Row([self.status, self.ring], spacing=6,
                                 height=SIZE["chip_h"])
        self.results = ft.ListView(spacing=SIZE["gap_tight"], auto_scroll=False,
                                   expand=True, padding=0)
        content = ft.Column([
            self.title,
            ft.Row([self.input, self.top_k, self.body_switch, self.btn],
                   spacing=SIZE["gap_tight"], height=SIZE["input_h"]),
            self.status_row,
            self.results,
        ], spacing=SIZE["gap_tight"], expand=True)
        self.card = card_container(content, colors)
        self.card.expand = True
        self._colors = colors
        self._t0 = None

    # ---- 状态条四态 ----

    def set_status(self, kind, extra=""):
        """kind: ready / loading / ok / empty。"""
        if kind == "ready":
            self.status.value = "输入问题开始测试检索"
            self.status.color = self._colors["t4"]
            self.ring.visible = False
        elif kind == "loading":
            self.status.value = "正在检索（首次需加载模型，约 30–60 秒）…"
            self.status.color = self._colors["accent"]
            self.ring.visible = True
        elif kind == "ok":
            self.status.value = "Top%s%s" % (extra or "5", "")
            self.status.color = self._colors["t3"]
            self.ring.visible = False
        elif kind == "empty":
            self.status.value = "未找到相关内容，换个问法试试"
            self.status.color = self._colors["t3"]
            self.ring.visible = False

    # ---- 结果渲染 ----

    def show_empty(self, colors):
        self.results.controls = [
            ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.SEARCH_OFF, color=colors["t4"], size=32),
                    ft.Text("未找到相关内容", size=13, color=colors["t2"],
                            font_family=FONT_UI),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                alignment=ft.Alignment.CENTER, expand=True,
            )
        ]

    def show_results(self, text, colors, elapsed_s=None, show_body=True):
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
            self.set_status("empty")
            return
        controls = []
        for i, b in enumerate(blocks):
            badge = ft.Container(
                content=ft.Text(str(i + 1), size=11, weight=ft.FontWeight.W_700,
                                color=colors["accent"], font_family=FONT_MONO),
                width=22, height=22, border_radius=SIZE["radius_badge"],
                bgcolor=ft.Colors.with_opacity(0.12, colors["accent"]),
                alignment=ft.Alignment.CENTER,
            )
            rel, heading, conf = _parse_src(b["src"])
            conf_txt = _conf_label(conf)
            conf_color = _conf_color(conf, colors) if conf is not None else colors["t3"]
            body = "\n".join(b["body"][:3]) if show_body else ""
            src = b["src"] if show_body else rel
            controls.append(ft.Container(
                content=ft.Row([
                    badge,
                    ft.Column([
                        ft.Text(body, size=13, font_family=FONT_UI, color=colors["t1"],
                                height=1.35, max_lines=3,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                visible=bool(body)),
                        ft.Row([
                            ft.Text(src, size=11, font_family=FONT_MONO,
                                    color=colors["t3"], expand=True, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Container(
                                content=ft.Text(conf_txt, size=11,
                                                weight=ft.FontWeight.W_700,
                                                color=conf_color,
                                                font_family=FONT_MONO),
                                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                                border_radius=SIZE["radius_badge"],
                                bgcolor=ft.Colors.with_opacity(0.14, conf_color),
                                visible=bool(conf_txt),
                            ),
                        ], spacing=8),
                    ], spacing=3, expand=True),
                ], vertical_alignment=ft.CrossAxisAlignment.START, spacing=10),
                padding=ft.Padding.all(10),
                border_radius=SIZE["radius_control"],
                border=ft.Border.all(1, colors["border_faint"]),
                bgcolor=colors["surface"],
                on_click=(lambda e, r=rel, h=heading: self._fire_open(r, h))
                         if self._on_open else None,
                on_hover=self._make_hover(colors["surface"], colors["hover"]),
                tooltip="点击打开源文件%s" % (("（定位：「%s」）" % heading) if heading else ""),
            ))
        self.results.controls = controls
        self.set_status("ok", str(len(controls)))

    @staticmethod
    def _make_hover(base, hover):
        def _on_hover(e):
            e.control.bgcolor = hover if e.data == "true" else base
        return _on_hover

    def _fire_open(self, rel, heading):
        if self._on_open:
            self._on_open(rel, heading)

    def set_banner(self, visible, colors):
        pass  # 索引中提示并入状态条，不单独横幅

    def apply(self, colors):
        self._colors = colors
        self.title.color = colors["t1"]
        self.input.fill_color = colors["sunken"]
        self.input.border_color = colors["border"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border(
            top=ft.BorderSide(1, colors["border_light"]),
            bottom=ft.BorderSide(1, colors["border"]),
            left=ft.BorderSide(1, colors["border"]),
            right=ft.BorderSide(1, colors["border"]),
        )


def _conf_color(conf, colors):
    """置信度标签配色：≥0.75 绿 / ≥0.5 青 / 其余橙。"""
    if conf >= 0.75:
        return colors["success"]
    if conf >= 0.5:
        return colors["accent"]
    return colors["warning"]


def _conf_label(conf):
    if conf is None:
        return ""
    return "%d%%" % int(round(conf * 100))


class LogView:
    """日志区：顶栏计数 + 固定行高自动滚动列表，ERROR/WARNING 着色。"""

    def __init__(self, colors=DARK):
        self.title = ft.Text("运行日志", size=13, weight=ft.FontWeight.W_600,
                             color=colors["t1"], font_family=FONT_UI)
        self.count = ft.Text("", size=11, color=colors["t4"], font_family=FONT_MONO)
        self.clear_btn = ft.TextButton("清空", icon=ft.Icons.DELETE_OUTLINE,
                                       on_click=self._clear)
        self.list = ft.ListView(auto_scroll=True, spacing=0, expand=True, padding=0)
        self._err = 0
        self._warn = 0
        self._colors = colors
        self._page = None
        content = ft.Column([
            ft.Row([self.title, ft.Container(expand=True), self.count,
                    self.clear_btn], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(
                content=self.list,
                bgcolor=colors["sunken"],
                border_radius=SIZE["radius_control"],
                border=ft.Border.all(1, colors["border_faint"]),
                expand=True,
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            ),
        ], spacing=4, expand=True)
        self.card = card_container(content, colors)
        self.card.expand = True

    def attach(self, page):
        self._page = page

    def append(self, line):
        if "ERROR" in line:
            color = self._colors["danger"]
            self._err += 1
            row = ft.Container(
                content=ft.Text(line, size=12, font_family=FONT_MONO,
                                color=color, height=1.3),
                bgcolor=ft.Colors.with_opacity(0.08, self._colors["danger"]),
                border=ft.Border(left=ft.BorderSide(2, self._colors["danger"])),
                padding=ft.Padding.symmetric(horizontal=6),
                height=SIZE["log_line_h"] + 4,
            )
        elif "WARNING" in line or "WARN" in line:
            color = self._colors["warning"]
            self._warn += 1
            row = ft.Text(line, size=12, font_family=FONT_MONO, color=color, height=1.3)
        else:
            row = ft.Text(line, size=12, font_family=FONT_MONO,
                          color=self._colors["t2"], height=1.3)
        self.list.controls.append(row)
        total = len(self.list.controls)
        if total > 1000:
            del self.list.controls[: total - 1000]
        self.count.value = "ERR %d · WARN %d" % (self._err, self._warn)
        if self._page:
            self._page.update()

    def load_history(self, lines):
        self._err = 0
        self._warn = 0
        self.list.controls = []
        for line in lines[-400:]:
            self._append_direct(line)
        self.count.value = "ERR %d · WARN %d" % (self._err, self._warn)

    def _append_direct(self, line):
        """无 page 回调的批量追加（load_history 内部用）。"""
        if "ERROR" in line:
            self._err += 1
            self.list.controls.append(ft.Container(
                content=ft.Text(line, size=12, font_family=FONT_MONO,
                                color=self._colors["danger"], height=1.3),
                bgcolor=ft.Colors.with_opacity(0.08, self._colors["danger"]),
                border=ft.Border(left=ft.BorderSide(2, self._colors["danger"])),
                padding=ft.Padding.symmetric(horizontal=6),
                height=SIZE["log_line_h"] + 4,
            ))
        elif "WARNING" in line or "WARN" in line:
            self._warn += 1
            self.list.controls.append(ft.Text(line, size=12, font_family=FONT_MONO,
                                              color=self._colors["warning"], height=1.3))
        else:
            self.list.controls.append(ft.Text(line, size=12, font_family=FONT_MONO,
                                              color=self._colors["t2"], height=1.3))

    def _clear(self, e):
        self.list.controls = []
        self._err = 0
        self._warn = 0
        self.count.value = "ERR 0 · WARN 0"
        if self._page:
            self._page.update()

    def apply(self, colors):
        self._colors = colors
        self.title.color = colors["t1"]
        self.count.color = colors["t4"]
        self.card.bgcolor = colors["surface"]
        self.card.border = ft.Border(
            top=ft.BorderSide(1, colors["border_light"]),
            bottom=ft.BorderSide(1, colors["border"]),
            left=ft.BorderSide(1, colors["border"]),
            right=ft.BorderSide(1, colors["border"]),
        )


class DeviceBar:
    """设备条：左操作按钮 + 右设备信息（高 40 锚定）。"""

    def __init__(self, on_vault, on_logs, colors=DARK):
        self.btn_vault = ft.TextButton("打开 Vault", icon=ft.Icons.FOLDER_OPEN,
                                       icon_color=colors["t2"],
                                       on_click=on_vault)
        self.btn_logs = ft.TextButton("打开日志目录", icon=ft.Icons.TERMINAL,
                                      icon_color=colors["t2"],
                                      on_click=on_logs)
        self.info = ft.Text("", size=11, color=colors["t3"], font_family=FONT_MONO)
        content = ft.Row([
            self.btn_vault, self.btn_logs, ft.Container(expand=True), self.info,
        ], spacing=SIZE["gap_tight"], vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.card = ft.Container(
            content=content,
            height=SIZE["device_h"],
            padding=ft.Padding.symmetric(horizontal=8),
            bgcolor=ft.Colors.with_opacity(0.85, colors["surface"]),
            border=ft.Border.all(1, colors["border_faint"]),
            border_radius=SIZE["radius_control"],
        )
        self._colors = colors

    def update(self, device, model, files, chunks):
        self.info.value = "%s · %s ｜ 已索引 %d 文件 / %d 块" % (
            device or "—", model or "—", files, chunks)

    def apply(self, colors):
        self.btn_vault.icon_color = colors["t2"]
        self.btn_logs.icon_color = colors["t2"]
        self.info.color = colors["t3"]
        self.card.bgcolor = ft.Colors.with_opacity(0.85, colors["surface"])
        self.card.border = ft.Border.all(1, colors["border_faint"])
