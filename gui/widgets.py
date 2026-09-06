"""widgets.py — 控制台组件：KPI 卡、心跳胶囊、进度卡、搜索卡、日志区、设备条。

布局纪律：所有卡片都有确定高度锚点（固定 px 或父级锚定），
禁止"自适应容器内套 expand 子项"导致高度链归零。
"""
import datetime
import re
import threading
import time

import flet as ft

from theme import DARK, FONT_MONO, FONT_NUM, FONT_UI, SIZE
from store import (  # noqa: F401  零侵入数据层：只读 meta/注册表，绝不加载模型
    library_entries,
    library_snapshot,
    file_index_rows_for,
    wemm_status_for,
    wemm_backend_state,
    wemm_service_probe,
)


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

    def set_state(self, state, colors, sub=None):
        self.dot.bgcolor = colors[self.COLOR[state]]
        main, default_sub = self.TEXT[state]
        self.title.value = main
        self.sub.value = sub if sub is not None else default_sub

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

    def set_state(self, state, now, colors, note=None):
        """更新心跳胶囊。note：可选的运行中补充文案（如「文档转换中」），
        覆盖默认状态文案但不改变状态色/呼吸行为。"""
        self.text.value = self.TEXT[state] if not note else note
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


PHASES = ["scanning", "converting", "embedding", "writing", "done"]
PHASE_TEXT = {"scanning": "扫描", "converting": "转换",
              "embedding": "嵌入", "writing": "写库", "done": "完成",
              "waiting-lock": "等锁"}  # 问题32：等锁排队相位（不进 stepper，仅文案映射）
PHASE_COLOR = {"scanning": "scan", "converting": "accent",
               "embedding": "accent", "writing": "accent", "done": "success",
               "waiting-lock": "accent"}


def _parse_src(src):
    """从检索源行解析 (相对路径, 标题, 置信度)。

    源行格式：[来源] docs/foo.md (## 小节标题) [块 1/3] [置信度 0.87·高相关]
    （置信度标记可带·分档词后缀，问题43；也可无后缀，兼容旧输出）
    - rel：Vault 内相对路径（正斜杠），用于 obsidian://open 定位；
    - heading：标题（若有），用于 # 锚点跳转；无法解析时回退整行作 rel；
    - conf：0-1 归一化置信度（无则 None）。
    """
    s = src.strip()
    if s.startswith("[来源] "):
        s = s[len("[来源] "):]
    conf = None
    m = re.search(r"\[置信度 ([\d.]+)(?:·[^\]]*)?\]", s)
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

    def update(self, progress, ratio, colors, elapsed_txt, eta_txt, idle_summary=""):
        phase = progress.get("phase") or "idle"
        lib = progress.get("library")
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
        if not progress.get("running"):
            # 空闲/完成态：显示全库汇总，而不是上一次任务的统计
            # （否则会把 148/148 这类任务数字误读成全库总量）
            self.count.value = idle_summary or ""
            return
        if phase == "embedding":
            done, total = progress.get("chunks_done"), progress.get("chunks_total")
            self.count.value = "块 %s/%s" % (done, total) if done is not None else ""
        else:
            done, total = progress.get("files_done"), progress.get("files_total")
            f = progress.get("chunks_done"), progress.get("chunks_total")
            if phase == "converting":
                # 文档转换相位：进度按文件计；大文件（页数多）单文件可能较久
                self.count.value = ("文档转换 %s/%s · PDF/DOCX→Markdown"
                                    % (done, total) if done is not None else "文档转换中...")
            elif phase == "scanning" and done is not None:
                self.count.value = "文件 %s/%s" % (done, total)
            elif f[0] is not None and total:
                self.count.value = "文件 %s/%s · 块 %s/%s" % (done, total, f[0], f[1])
            else:
                self.count.value = "文件 %s/%s" % (done, total) if done is not None else ""
        if lib:
            self.count.value = (self.count.value + " ｜ 库：%s" % lib
                                if self.count.value else "库：%s" % lib)

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
    on_relations(rel)：点击"关联笔记"按钮时回调（查双链出链/入链），返回
    resolve_note_relations 的结果字典；与正文展开是两个独立开关。
    """

    TOP_K_CHOICES = (3, 5, 8, 10, 15)

    def __init__(self, on_search, on_open=None, on_relations=None, colors=DARK):
        self._on_open = on_open
        self._on_relations = on_relations
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
                                   expand=True, padding=0,
                                   build_controls_on_demand=False)
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
        self._render_state = {"text": text, "colors": colors,
                              "show_body": show_body, "expanded": set(),
                              "relations_shown": set(), "relations_cache": {}}
        self._render_results()

    def _toggle_relations(self, i, rel):
        """展开/收起第 i 条结果的"关联笔记"区。首次展开才查询，之后走缓存
        （同一条结果反复展开/收起不重复调用 on_relations）。与正文展开
        （_toggle/expanded）是独立开关，互不干扰。"""
        shown = self._render_state["relations_shown"]
        if i in shown:
            shown.discard(i)
        else:
            shown.add(i)
            cache = self._render_state["relations_cache"]
            if i not in cache and self._on_relations:
                cache[i] = self._on_relations(rel)
        self._render_results()

    def _render_results(self):
        st = self._render_state
        text, colors, show_body = st["text"], st["colors"], st["show_body"]
        expanded = st["expanded"]
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
            rel, heading, conf = _parse_src(b["src"])
            conf_txt = _conf_label(conf)
            conf_color = _conf_color(conf, colors) if conf is not None else colors["t3"]
            # 收起态 = 前 3 行预览；展开态 = 完整正文（表格/长块不再被 GUI 砍断）
            body_full = "\n".join(b["body"]) if show_body else ""
            body_preview = "\n".join(b["body"][:3]) if show_body else ""
            src = b["src"] if show_body else rel
            is_open = i in expanded and bool(body_full)
            rel_open = i in st["relations_shown"]

            body_text = ft.Text(body_full if is_open else body_preview,
                                size=13, font_family=FONT_UI, color=colors["t1"],
                                max_lines=None if is_open else 6,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                selectable=True)
            body_box = ft.Container(content=body_text,
                                    padding=ft.Padding.only(top=6))

            def make_rel_toggle(i=i, rel=rel):
                def _toggle_rel(e):
                    self._toggle_relations(i, rel)
                    e.control.page.update()
                return _toggle_rel

            open_btn = ft.IconButton(
                icon=ft.Icons.OPEN_IN_NEW, icon_size=14,
                icon_color=colors["t3"], padding=2, width=24, height=24,
                tooltip="在 Obsidian 中打开源文件",
                on_click=(lambda e, r=rel, h=heading: self._fire_open(r, h))
                         if self._on_open else None,
            )
            rel_btn = ft.IconButton(
                icon=ft.Icons.HUB, icon_size=14,
                icon_color=colors["accent"] if rel_open else colors["t3"],
                padding=2, width=24, height=24,
                tooltip="查看关联笔记（双链）",
                on_click=make_rel_toggle() if self._on_relations else None,
            )
            chevron = ft.Icon(ft.Icons.EXPAND_MORE if is_open
                              else ft.Icons.CHEVRON_RIGHT,
                              size=14, color=colors["t3"],
                              visible=bool(body_full))
            header = ft.Row([
                chevron,
                ft.Text(src, size=11, font_family=FONT_MONO, color=colors["t3"],
                        expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Container(
                    content=ft.Text(conf_txt, size=11, weight=ft.FontWeight.W_700,
                                    color=conf_color, font_family=FONT_MONO),
                    padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                    border_radius=SIZE["radius_badge"],
                    bgcolor=ft.Colors.with_opacity(0.14, conf_color),
                    visible=bool(conf_txt),
                ),
                rel_btn,
                open_btn,
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

            def make_toggle(i=i, expanded=expanded, body=bool(body_full),
                            colors=colors, chevron=chevron):
                def _toggle(e):
                    if not body:
                        return
                    if i in expanded:
                        expanded.discard(i)
                    else:
                        expanded.add(i)
                    self._render_results()
                    e.control.page.update()
                return _toggle

            head_container = ft.Container(
                content=header, padding=0,
                on_click=make_toggle() if body_full else None,
            )
            card_controls = [head_container]
            if is_open:
                card_controls.append(body_box)
            if rel_open:
                rel_data = st["relations_cache"].get(i) or {}
                if not rel_data.get("resolved"):
                    rel_txt = "（未找到该笔记的索引记录，可能已重命名或移动）"
                else:
                    out = "、".join(rel_data.get("outlinks") or []) or "（无）"
                    inn = "、".join(rel_data.get("inlinks") or []) or "（无）"
                    rel_txt = "出链（本文链接到）：%s\n入链（谁链接到本文）：%s" % (out, inn)
                card_controls.append(ft.Container(
                    content=ft.Text(rel_txt, size=12, font_family=FONT_UI,
                                    color=colors["t2"], selectable=True),
                    padding=ft.Padding.only(top=6),
                ))
            controls.append(ft.Container(
                content=ft.Column(card_controls, spacing=0),
                padding=ft.Padding.all(10),
                border_radius=SIZE["radius_control"],
                border=ft.Border.all(1, colors["border_faint"]),
                bgcolor=colors["surface"],
                on_hover=self._make_hover(colors["surface"], colors["hover"]),
                tooltip="点击标题行展开/收起命中片段" if body_full else None,
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
    """置信度标签配色：≥0.65 绿 / ≥0.55 青 / 其余橙。

    2026-09-06 按实测分布重校准（问题43）：重排 sigmoid 绝对分挤在 0.50~0.73，
    0.65+ 即为确定命中档（与 retriever.CONF_TIER_STRONG 一致），
    0.55 与 warn 阈值对齐；旧边界 0.75/0.5 会把大部分好命中标成中间色。"""
    if conf >= 0.65:
        return colors["success"]
    if conf >= 0.55:
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

    def update(self, device, model, files, chunks, library=""):
        lib_txt = (" ｜ 库：%s" % library) if library and library != ALL_LIBRARIES else ""
        self.info.value = "%s · %s%s ｜ 已索引 %d 文件 / %d 块" % (
            device or "—", model or "—", lib_txt, files, chunks)

    def apply(self, colors):
        self.btn_vault.icon_color = colors["t2"]
        self.btn_logs.icon_color = colors["t2"]
        self.info.color = colors["t3"]
        self.card.bgcolor = ft.Colors.with_opacity(0.85, colors["surface"])
        self.card.border = ft.Border.all(1, colors["border_faint"])


class SettingsDialog:
    """设置对话框（问题31 重构）：左侧导航 + 右侧当前分组详情。

    导航分「常用」「开发者」两小节（config_editor.GROUPS 的 level 字段），
    每组一页，解决 44 个字段平铺难定位的问题。字段控件按 FIELD_META 渲染：
    bool→Switch、choices→Dropdown（不再手输 mineru-cloud 这类魔法字符串）、
    suggest→输入框 + 推荐模型芯片、secret→密码框，其余→TextField。
    _fields[key]=(输入控件, kind) 是对外契约：_save 统一收集其 .value，
    tests/test_gui_store.py 的云端确认用例直接改它来模拟用户输入。
    on_saved(errors)：点保存后回调，errors 为空 dict 表示成功。
    """

    def __init__(self, on_saved, colors=DARK):
        self._on_saved = on_saved
        self._cols = colors
        self._fields = {}       # key -> (输入控件, kind)
        self._nav_btns = {}     # 组序号 -> 导航按钮
        self._group_children = []  # 每组的右侧面板子控件列表
        self._detail_col = None
        self._cur_group = -1
        self._ok = ft.FilledButton(
            "保存", style=ft.ButtonStyle(
                bgcolor=colors["accent"], color=colors["on_accent"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600),
            ),
            on_click=self._save,
        )
        self._cancel = ft.OutlinedButton(
            "取消", style=ft.ButtonStyle(
                color=colors["t2"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                text_style=ft.TextStyle(font_family=FONT_UI),
            ),
            on_click=self._cancel_click,
        )
        self._status = ft.Text("", size=12, color=colors["warning"],
                               font_family=FONT_UI, visible=False, expand=True)
        self._page = None
        self._dlg = None
        self._build()

    # ---- 构建 ----

    def _caption(self, text):
        return ft.Text(text or "", size=11, color=self._cols["t4"],
                       font_family=FONT_UI, height=1.35)

    def _make_chip(self, ctrl, val, text):
        def _fill(_e):
            ctrl.value = val
            try:
                ctrl.update()
            except (RuntimeError, AttributeError, AssertionError):
                pass  # 未挂载（构造期/冒烟环境）
        return ft.OutlinedButton(
            "%s · %s" % (text, val),
            style=ft.ButtonStyle(
                text_style=ft.TextStyle(size=11, font_family=FONT_UI),
                color=self._cols["t2"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_pill"]),
            ),
            height=30,
            on_click=_fill,
        )

    def _make_field(self, key, kind):
        """渲染单个字段的整块控件，返回 (块控件, 输入控件)。"""
        import config_editor as ce
        from config import CFG
        c = self._cols
        meta = ce.FIELD_META.get(key, {})
        label = meta.get("label", key)
        hint = meta.get("hint", "")
        if meta.get("rebuild"):
            hint = "⟳ 改后需全量重建 · " + hint if hint else "⟳ 改后需全量重建"
        cur = CFG.get(key)

        if kind == "bool":
            ctrl = ft.Switch(
                value=bool(cur), label=label,
                label_text_style=ft.TextStyle(size=13, font_family=FONT_UI),
                active_color=c["accent"], inactive_thumb_color=c["t3"],
            )
            return ft.Column([
                ft.Row([ctrl], spacing=0), self._caption(hint),
            ], spacing=2), ctrl

        if meta.get("choices"):
            opts = [ft.DropdownOption(key=k2, text=t2) for k2, t2 in meta["choices"]]
            val = "" if cur is None else str(cur)
            known = {k2 for k2, _ in meta["choices"]}
            if val and val not in known:
                # 用户手改过 config.json：保真展示原值，保存时不静默改写
                opts.append(ft.DropdownOption(key=val, text="%s（当前配置值）" % val))
            elif not val:
                val = meta["choices"][0][0]
            ctrl = ft.Dropdown(
                value=val, options=opts, label=label,
                height=SIZE["input_h"], dense=True,
                filled=True, fill_color=c["sunken"],
                border_color=c["border"], border_radius=SIZE["radius_control"],
                label_style=ft.TextStyle(size=12, font_family=FONT_UI),
                text_style=ft.TextStyle(size=13, font_family=FONT_UI),
                expand=True,
            )
            return ft.Column([
                ft.Row([ctrl], spacing=0), self._caption(hint),
            ], spacing=2), ctrl

        # 文本类：list 以逗号拼接展示，写回时由 config_editor 拆回
        if kind == "list":
            val = ", ".join(str(x) for x in cur) if cur else ""
        elif cur is None:
            val = ""
        else:
            val = str(cur)
        ctrl = ft.TextField(
            label=label, value=val,
            height=None if hint else 46, dense=True,
            border_radius=SIZE["radius_control"],
            filled=True, fill_color=c["sunken"],
            border_color=c["border"],
            text_size=13,
            text_style=ft.TextStyle(font_family=FONT_UI),
            label_style=ft.TextStyle(size=12, font_family=FONT_UI),
            helper=hint or None,
            helper_style=ft.TextStyle(size=11, font_family=FONT_UI),
            password=bool(meta.get("secret")),
            can_reveal_password=bool(meta.get("secret")),
            expand=True,
        )
        block = ctrl
        if meta.get("suggest"):
            # 芯片是推荐候选而非"本机已装清单"——点选填入后首次使用才下载；
            # 与当前配置值相同的芯片标「✓ 使用中」，让用户一眼看清现状。
            cur_txt = "" if cur is None else str(cur)
            chips = ft.Row([], spacing=8, wrap=True)
            for mval, mtext in meta["suggest"]:
                in_use = (mval == cur_txt)
                chip = self._make_chip(
                    ctrl, mval,
                    "✓ 使用中 · %s" % mtext if in_use else mtext)
                if in_use:
                    chip.style = ft.ButtonStyle(
                        text_style=ft.TextStyle(size=11, font_family=FONT_UI),
                        color=c["accent"],
                        side=ft.BorderSide(1, c["accent"]),
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_pill"]),
                    )
                chips.controls.append(chip)
            block = ft.Column([
                ctrl,
                self._caption("推荐候选（HuggingFace 模型标识，点击填入；"
                              "未下载过的会在首次使用时自动拉取）："),
                chips,
            ], spacing=4)
        return block, ctrl

    def _build(self):
        import config_editor as ce

        c = self._cols
        self._fields.clear()
        nav_controls = []
        self._group_children = []
        self._nav_btns = {}
        last_level = None
        for gi, g in enumerate(ce.GROUPS):
            if g["level"] != last_level:
                last_level = g["level"]
                nav_controls.append(ft.Container(
                    content=ft.Text("常用" if g["level"] == "basic" else "开发者",
                                    size=11, weight=ft.FontWeight.W_700,
                                    color=c["t4"], font_family=FONT_UI),
                    padding=ft.Padding(left=10, top=6, bottom=2),
                ))
            needs_rebuild = any(ce.FIELD_META.get(k, {}).get("rebuild")
                                for k, _ in g["fields"])
            children = [
                ft.Row([
                    ft.Icon(getattr(ft.Icons, g["icon"]), size=16,
                            color=c["accent"]),
                    ft.Text(g["title"], size=14, weight=ft.FontWeight.W_600,
                            color=c["t1"], font_family=FONT_UI),
                ], spacing=8),
                ft.Text(g["desc"], size=11, color=c["t3"],
                        font_family=FONT_UI, height=1.4),
            ]
            for key, kind in g["fields"]:
                block, ctrl = self._make_field(key, kind)
                self._fields[key] = (ctrl, kind)
                children.append(block)
            self._group_children.append(children)
            btn = ft.Container(
                content=ft.Row([ft.Text(
                    g["title"] + (" ⟳" if needs_rebuild else ""),
                    size=12, color=c["t2"], font_family=FONT_UI)], spacing=6),
                height=32, border_radius=SIZE["radius_control"],
                padding=ft.Padding.symmetric(horizontal=10),
                alignment=ft.alignment.Alignment.CENTER_LEFT, ink=True,
                on_click=lambda _e, i=gi: self._select(i),
            )
            self._nav_btns[gi] = btn
            nav_controls.append(btn)

        self._detail_col = ft.Column(spacing=14, scroll=ft.ScrollMode.AUTO,
                                     expand=True)
        body = ft.Row([
            ft.Container(width=196, content=ft.Column(
                nav_controls, spacing=2, scroll=ft.ScrollMode.AUTO)),
            ft.VerticalDivider(width=1, color=c["border_faint"]),
            self._detail_col,
        ], spacing=14, expand=True,
           vertical_alignment=ft.CrossAxisAlignment.STRETCH)

        self._dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.SETTINGS_OUTLINED, size=18, color=c["accent"]),
                ft.Text("设置", size=18, weight=ft.FontWeight.W_600,
                        font_family=FONT_UI),
                ft.Container(expand=True),
                ft.Text("⟳ = 改动需 python index.py --full 全量重建",
                        size=11, color=c["t4"], font_family=FONT_UI),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column([
                    body,
                    ft.Row([self._status, ft.Container(expand=True),
                            self._cancel, self._ok],
                           spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=12, expand=True),
                width=800, height=560,
            ),
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        self._select(0)

    def _select(self, idx):
        """切换导航选中态与右栏内容；未挂载时静默（构造期/冒烟环境）。"""
        c = self._cols
        self._cur_group = idx
        for j, btn in self._nav_btns.items():
            sel = (j == idx)
            btn.bgcolor = c["active"] if sel else None
            t = btn.content.controls[0]
            t.color = c["t1"] if sel else c["t2"]
            t.weight = ft.FontWeight.W_600 if sel else ft.FontWeight.W_400
        if self._detail_col is not None and \
                0 <= idx < len(self._group_children):
            self._detail_col.controls = self._group_children[idx]
        try:
            if self._dlg is not None:
                self._dlg.update()
        except (RuntimeError, AttributeError, AssertionError):
            pass  # 对话框尚未挂载到页面

    def _refresh_values(self):
        """每次打开时从最新 CFG 回填控件值（外部改过 config.json 也能看到新值）。"""
        import config_editor as ce
        from config import CFG
        for key, (ctrl, kind) in self._fields.items():
            cur = CFG.get(key)
            if kind == "bool":
                ctrl.value = bool(cur)
                continue
            meta = ce.FIELD_META.get(key, {})
            if meta.get("choices"):
                opts = [ft.DropdownOption(key=k2, text=t2)
                        for k2, t2 in meta["choices"]]
                val = "" if cur is None else str(cur)
                known = {k2 for k2, _ in meta["choices"]}
                if val and val not in known:
                    opts.append(ft.DropdownOption(
                        key=val, text="%s（当前配置值）" % val))
                elif not val:
                    val = meta["choices"][0][0]
                ctrl.options = opts
                ctrl.value = val
                continue
            if kind == "list":
                ctrl.value = ", ".join(str(x) for x in cur) if cur else ""
            else:
                ctrl.value = "" if cur is None else str(cur)

    # ---- 交互 ----

    def open(self, page):
        self._refresh_values()
        self._select(0)
        self.show_status("", None)
        self._page = page
        page.show_dialog(self._dlg)

    def _save(self, e):
        from config import CFG
        updates = {}
        for key, (inp, kind) in self._fields.items():
            updates[key] = (kind, inp.value)
        # 从"非云端"切到 mineru-cloud = 相应文件的原始字节会被上传到第三方，属于
        # 不可撤销的外传决定，必须先取得明示同意。两个键各自独立控制不同的文件
        # 类别（pdf_scan_backend=库里全部存量扫描件，下次索引即批量上传；
        # pdf_text_backend=今后每份有文字层的 PDF），任一新切到 mineru-cloud 都要
        # 确认；本来就是 mineru-cloud 的键改别的字段不再打扰用户。
        newly_cloud = [k for k in ("pdf_scan_backend", "pdf_text_backend")
                       if self._switches_to_cloud(updates, CFG, k)]
        if newly_cloud:
            self._confirm_cloud_backend(updates, newly_cloud)
            return
        self._apply_updates(updates)

    @staticmethod
    def _switches_to_cloud(updates, CFG, key):
        raw = updates.get(key)
        new_val = str((raw[1] if raw else "") or "").strip().lower()
        cur_val = str(CFG.get(key) or "").strip().lower()
        return new_val == "mineru-cloud" and cur_val != "mineru-cloud"

    def _confirm_cloud_backend(self, updates, keys):
        """启用云端处理前的二次确认框。

        keys：本次保存里新切到 mineru-cloud 的配置键集合（"pdf_scan_backend"
        和/或 "pdf_text_backend"），文案按实际涉及的键列出对应文件类别，
        不管哪个键触发都点名 MinerU 与"原始文件上传"这个核心事实。
        """
        c = self._cols
        lines = ["开启后，以下文件的原始内容会被上传至第三方服务 MinerU（mineru.net）："]
        if "pdf_scan_backend" in keys:
            lines.append("· 扫描件 PDF——库中已有的存量扫描件也会在下次索引时被自动批量上传")
        if "pdf_text_backend" in keys:
            lines.append("· 有文字层的 PDF——今后每次索引新增/变更的此类文件都会被上传"
                         "（只做版面/表格结构识别，is_ocr=False 不重复计费 OCR）")
        lines.append("请确认知识库中没有你不希望上传的敏感文件。")
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认启用云端处理", size=16, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Text(
                "\n".join(lines),
                size=13, font_family=FONT_UI, height=1.6, color=c["t2"]),
            actions=[
                ft.TextButton(
                    "取消",
                    on_click=lambda _e: self._cancel_cloud_backend(dlg)),
                ft.FilledButton(
                    "确认启用",
                    style=ft.ButtonStyle(
                        bgcolor=c["accent"], color=c["on_accent"],
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI,
                                                weight=ft.FontWeight.W_600)),
                    on_click=lambda _e: self._confirmed_apply(dlg, updates),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        try:
            if self._page is not None:
                self._page.show_dialog(dlg)
        except Exception:
            pass  # 冒烟/无窗口环境：不真实挂载

    @staticmethod
    def _close_confirm(dlg):
        try:
            dlg.open = False
            dlg.update()
        except (RuntimeError, AttributeError):
            pass

    def _cancel_cloud_backend(self, dlg):
        self._close_confirm(dlg)
        self.show_status("已取消（未保存）", None)

    def _confirmed_apply(self, dlg, updates):
        self._close_confirm(dlg)
        self._apply_updates(updates)

    def _apply_updates(self, updates):
        import config_editor as ce
        errors = ce.apply_updates(updates)
        if errors:
            first = next(iter(errors))
            msg = errors[first]
            self.show_status("保存失败：%s" % msg, "error")
        else:
            self.show_status("已保存到 data/config.json（检索类即时生效；结构类需 --full 重建）", "ok")

    def _cancel_click(self, e):
        if self._dlg:
            self._dlg.open = False
            self._dlg.update()

    def show_status(self, msg, kind):
        self._status.value = msg
        self._status.visible = bool(msg)
        self._status.color = {"ok": self._cols["success"],
                              "error": self._cols["danger"]}.get(kind,
                                                                 self._cols["warning"])
        try:
            if self._dlg:
                self._dlg.update()
        except RuntimeError:
            pass  # 对话框尚未挂载到页面（冒烟/构造期）

    def apply(self, colors):
        self._cols = colors


# ---- 库选择下拉 ----

ALL_LIBRARIES = "<全部库>"


class LibraryPicker:
    """库选择胶囊（header）：多选库范围，点击弹勾选对话框。

    - `checked`：None = 全部库（libraries=""，新增库自动纳入）；set = 白名单
      并查（libraries="A,B"）；反选 = 全勾后取消想排除的库（同一机制）。
    - `value`：返回可直接传给 hybrid_search 的 libraries 参数字符串。
    不用 Dropdown（flet 0.86 桌面端 dense 下拉渲染不可靠）；
    胶囊外观与心跳胶囊同构（已验证可渲染），选择走 AlertDialog + Checkbox。
    """

    def __init__(self, on_change, colors=DARK):
        self._on_change = on_change
        self._colors = colors
        self._names = []
        self._checked = None        # None = 全部库；set = 白名单
        self._boxes = {}            # key -> Checkbox
        self._page = None
        self._dlg = None
        self.icon = ft.Icon(ft.Icons.LIBRARY_BOOKS_OUTLINED, size=14,
                            color=colors["accent"])
        self.text = ft.Text("全部库", size=12, weight=ft.FontWeight.W_600,
                            color=colors["t1"], font_family=FONT_UI)
        self.chevron = ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=18,
                               color=colors["t3"])
        self.card = ft.Container(
            content=ft.Row([self.icon, self.text, self.chevron], spacing=6,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            height=SIZE["pill_h"],
            padding=ft.Padding.symmetric(horizontal=SIZE["pill_pad_x"]),
            border_radius=SIZE["radius_pill"],
            bgcolor=ft.Colors.with_opacity(0.08, colors["t4"]),
            border=ft.Border.all(1, colors["border_faint"]),
            on_click=self._open,
            tooltip="检索与重建目标：全部库 / 多库并查 / 反选",
        )

    @property
    def value(self):
        """libraries 参数字符串：全部库 → ''；白名单 → 'A,B'（排序）。"""
        if self._checked is None or not self._names:
            return ""
        return ",".join(sorted(self._checked))

    @property
    def selected_names(self):
        """当前选中的库名集合（全部库时 = 全部注册库）。"""
        if self._checked is None:
            return set(self._names)
        return set(self._checked)

    @property
    def is_all(self):
        return self._checked is None or not self._names

    def _summary_text(self):
        if self.is_all:
            return "全部库"
        checked = self._checked or set()
        n = len(checked)
        if n == 0:
            return "未选库"
        if n == 1:
            return next(iter(checked))
        return "%d 个库" % n

    def _open(self, e):
        colors = self._colors
        self._boxes = {}
        body = ft.Column(spacing=2)

        def make_box(key, title, sub):
            box = ft.Checkbox(
                value=(self._checked is None or key in self._checked),
                active_color=colors["accent"],
                check_color=colors["on_accent"],
                on_change=self._box_changed,
                data=key,
            )
            row = ft.Container(
                content=ft.Row([
                    box,
                    ft.Column([
                        ft.Text(title, size=13, color=colors["t1"],
                                font_family=FONT_UI),
                        ft.Text(sub, size=11, color=colors["t3"],
                                font_family=FONT_UI),
                    ], spacing=1, expand=True),
                ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                on_click=lambda _e, k=key: self._toggle(k),
                padding=ft.Padding.symmetric(horizontal=6, vertical=4),
                border_radius=SIZE["radius_control"],
            )
            self._boxes[key] = box
            body.controls.append(row)

        for n in self._names:
            make_box(n, n, "勾选 = 纳入检索与重建范围")
        self._hint = ft.Text("提示：全选 = 全部库（新增库自动纳入）；取消勾选某库 = 反选排除。",
                             size=11, color=colors["t4"], font_family=FONT_UI,
                             height=1.4)
        self._dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("选择库范围", size=16, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.TextButton("全选", icon=ft.Icons.SELECT_ALL,
                                      on_click=self._check_all),
                        ft.TextButton("反选", icon=ft.Icons.FLIP,
                                      on_click=self._invert),
                        ft.Container(expand=True),
                    ]),
                    ft.Container(
                        content=ft.ListView([body], spacing=2, padding=0),
                        height=240, expand=False,
                    ),
                    self._hint,
                ], spacing=6),
                width=400,
            ),
            actions=[
                ft.TextButton("取消", on_click=self._cancel),
                ft.FilledButton(
                    "确定", style=ft.ButtonStyle(
                        bgcolor=colors["accent"], color=colors["on_accent"],
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI,
                                                weight=ft.FontWeight.W_600)),
                    on_click=self._confirm,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        if self._page is not None:
            self._page.show_dialog(self._dlg)

    def _box_changed(self, e):
        self._sync_from_boxes()

    def _toggle(self, key):
        box = self._boxes.get(key)
        if box is None:
            return
        box.value = not box.value
        self._sync_from_boxes()
        try:
            box.update()
        except RuntimeError:
            pass

    def _sync_from_boxes(self):
        checked = {k for k, b in self._boxes.items() if b.value}
        self._checked = None if checked == set(self._names) else checked

    def _check_all(self, e):
        for b in self._boxes.values():
            b.value = True
        self._checked = None
        self._update_dlg()

    def _invert(self, e):
        for b in self._boxes.values():
            b.value = not b.value
        self._sync_from_boxes()
        self._update_dlg()

    def _update_dlg(self):
        try:
            if self._dlg:
                self._dlg.update()
        except RuntimeError:
            pass

    def _confirm(self, e):
        self._close_dlg()
        self.text.value = self._summary_text()
        if self._on_change:
            self._on_change(self._checked)

    def _cancel(self, e):
        self._close_dlg()

    def _close_dlg(self):
        if self._dlg:
            self._dlg.open = False
            try:
                self._dlg.update()
            except RuntimeError:
                pass

    def set_names(self, names):
        """刷新库名列表（全部库模式保持；白名单模式剔除已注销的库）。"""
        self._names = list(names)
        if self._checked is not None:
            self._checked = set(self._checked) & set(names)
            if self._checked == set(names):
                self._checked = None
            self.text.value = self._summary_text()
        self.card.disabled = not self._names
        self.card.visible = True

    def apply(self, colors):
        self._colors = colors
        self.icon.color = colors["accent"]
        self.text.color = colors["t1"]
        self.chevron.color = colors["t3"]
        self.card.bgcolor = ft.Colors.with_opacity(0.08, colors["t4"])
        self.card.border = ft.Border.all(1, colors["border_faint"])


# ---- 库管理对话框 ----

_LIB_CONFIG_NAMES = {
    "exclude_dirs": "排除目录（逗号分隔，相对库路径）",
    "exclude_files": "排除文件（逗号分隔）",
    "exclude_patterns": "排除前缀（逗号分隔）",
    "chunk_char_limit": "单块最大字符",
    "short_doc_char_limit": "短文档整篇阈值",
    "extensions": "可索引扩展名（逗号分隔）",
    "collection": "Chroma collection 名",
}
_LIB_LIST_KEYS = ("exclude_dirs", "exclude_files", "exclude_patterns", "extensions")
_LIB_INT_KEYS = ("chunk_char_limit", "short_doc_char_limit")


class LibraryManagerDialog:
    """库管理对话框：列库（块数/最近索引/覆盖项）、添加、配置、移除、打开文件夹。

    on_changed(rows)：注册表变化后回调（App 刷新下拉与 KPI）。
    调用 library.py 的既有函数（add_library/remove_library/set_config/unset_config），
    与 CLI 行为完全一致；块数取自 list_summary()（不加载模型）。
    """

    def __init__(self, on_changed, colors=DARK):
        self._on_changed = on_changed
        self._cols = colors
        self._page = None
        self._rows = []            # list_summary() 行
        self._dlg = None
        self._list = ft.ListView(spacing=SIZE["gap_tight"], expand=True, padding=0)
        self._status = ft.Text("", size=12, color=colors["warning"],
                               font_family=FONT_UI, visible=False, expand=True)
        self._btn_add = ft.FilledButton(
            "添加库", icon=ft.Icons.ADD, height=36,
            style=ft.ButtonStyle(
                bgcolor=colors["accent"], color=colors["on_accent"],
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600),
            ),
            on_click=self._open_add,
        )
        self._btn_close = ft.TextButton("关闭", on_click=self._close)
        self._btn_lab = ft.OutlinedButton(
            "提取试验台", icon=ft.Icons.SCIENCE, height=36,
            tooltip="选一个文件试跑提取管线，预览转译效果与质量",
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                text_style=ft.TextStyle(font_family=FONT_UI)),
            on_click=self._open_lab)
        self._lab = None
        self._build()

    # ---- 构建 ----

    def _build(self):
        body = ft.Column([
            self._list,
            ft.Row([self._status, ft.Container(expand=True),
                    self._btn_close, self._btn_lab, self._btn_add],
                   spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=SIZE["gap_tight"], expand=True)
        self._dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("库管理", size=18, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Container(content=body, width=760, height=460),
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        self._build_rows([])

    def _build_rows(self, rows):
        colors = self._cols
        controls = []
        if not rows:
            controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.LIBRARY_ADD_OUTLINED, color=colors["t4"], size=36),
                    ft.Text("尚未注册任何库。点击右上『添加库』注册一个文件夹。",
                            size=13, color=colors["t2"], font_family=FONT_UI),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                alignment=ft.Alignment.CENTER, expand=True, padding=40,
            ))
        for r in rows:
            over = r["overrides"] or "全部继承全局"
            controls.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.FOLDER_OUTLINED, size=16, color=colors["accent"]),
                        ft.Text(r["name"], size=14, weight=ft.FontWeight.W_700,
                                color=colors["t1"], font_family=FONT_UI, expand=True),
                        ft.Text("块 %s" % r["blocks"], size=12,
                                font_family=FONT_MONO, color=colors["t3"]),
                    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Text(r["path"], size=11, font_family=FONT_MONO,
                            color=colors["t3"], max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Row([
                        ft.Text("最近索引：%s" % _fmt_ts(r["last_indexed"]),
                                size=11, color=colors["t4"], font_family=FONT_MONO),
                        ft.Text("·", size=11, color=colors["t4"]),
                        ft.Text("覆盖：%s" % over, size=11, color=colors["t4"],
                                font_family=FONT_MONO, expand=True, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.TextButton("打开文件夹", icon=ft.Icons.FOLDER_OPEN,
                                      style=ft.ButtonStyle(
                                          text_style=ft.TextStyle(size=11, font_family=FONT_UI)),
                                      on_click=lambda e, p=r["path"]: self._open_dir(p)),
                        ft.TextButton("配置", icon=ft.Icons.TUNE,
                                      style=ft.ButtonStyle(
                                          text_style=ft.TextStyle(size=11, font_family=FONT_UI)),
                                      on_click=lambda e, n=r["name"]: self._open_config(n)),
                        ft.TextButton("移除", icon=ft.Icons.DELETE_OUTLINE,
                                      style=ft.ButtonStyle(
                                          text_style=ft.TextStyle(size=11, font_family=FONT_UI),
                                          color=colors["danger"]),
                                      on_click=lambda e, n=r["name"]: self._open_remove(n)),
                    ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=4),
                padding=ft.Padding.all(10),
                border_radius=SIZE["radius_control"],
                border=ft.Border.all(1, colors["border_faint"]),
                bgcolor=colors["surface"],
            ))
        self._list.controls = controls

    def _refresh(self):
        from library import list_summary
        self._rows = list_summary()
        self._build_rows(self._rows)
        self._set_status("")
        if self._on_changed:
            self._on_changed([r["name"] for r in self._rows])
        self._update()

    # ---- 打开 / 关闭 ----

    def open(self, page):
        self._page = page
        self._refresh()
        page.show_dialog(self._dlg)

    def _close(self, e):
        if self._dlg:
            self._dlg.open = False
            self._dlg.update()

    def _update(self):
        try:
            if self._dlg:
                self._dlg.update()
        except RuntimeError:
            pass

    def _set_status(self, msg, kind=None):
        self._status.value = msg
        self._status.visible = bool(msg)
        if kind == "ok":
            self._status.color = self._cols["success"]
        elif kind == "error":
            self._status.color = self._cols["danger"]
        else:
            self._status.color = self._cols["warning"]

    # ---- 添加 ----

    def _open_add(self, e):
        colors = self._cols
        self._path_input = ft.TextField(
            label="文件夹路径（必填）", hint_text="D:\\...\\笔记文件夹",
            height=46, dense=True, border_radius=SIZE["radius_control"],
            filled=True, fill_color=colors["sunken"],
            border_color=colors["border"], text_size=13, expand=True,
            text_style=ft.TextStyle(font_family=FONT_UI),
            label_style=ft.TextStyle(size=12, font_family=FONT_UI),
        )
        self._name_input = ft.TextField(
            label="库名（可空 = 文件夹名）", height=46, dense=True,
            border_radius=SIZE["radius_control"],
            filled=True, fill_color=colors["sunken"],
            border_color=colors["border"], text_size=13, expand=True,
            text_style=ft.TextStyle(font_family=FONT_UI),
            label_style=ft.TextStyle(size=12, font_family=FONT_UI),
        )
        self._add_status = ft.Text("", size=12, visible=False, font_family=FONT_UI)
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("添加库", size=16, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Container(
                content=ft.Column([
                    self._path_input,
                    self._name_input,
                    self._add_status,
                ], spacing=SIZE["gap_tight"], width=480),
                padding=ft.Padding.only(top=4),
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_sub(dlg)),
                ft.FilledButton(
                    "注册", style=ft.ButtonStyle(
                        bgcolor=colors["accent"], color=colors["on_accent"],
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600)),
                    on_click=lambda _: self._do_add(dlg),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        self._page_ref = self._current_page()
        self._show_sub(dlg)

    def _do_add(self, dlg):
        from library import add_library
        path = (self._path_input.value or "").strip().strip('"')
        name = (self._name_input.value or "").strip() or None
        try:
            entry = add_library(path, name)
        except (ValueError, OSError) as ex:
            self._add_status.value = "添加失败：%s" % ex
            self._add_status.color = self._cols["danger"]
            self._add_status.visible = True
            self._update()
            return
        self._close_sub(dlg)
        self._refresh()
        self._set_status("已注册库：%s。点击上方『增量重建』开始建索引。" % entry["name"], "ok")

    # ---- 配置 ----

    def _open_lab(self, _e=None):
        """打开提取试验台（复用实例，每次绑定当前窗口）。"""
        if self._lab is None:
            self._lab = ExtractLabDialog(colors=self._cols)
        self._lab.open(self._page)

    def _open_config(self, name):
        from library import effective_config, load_registry
        colors = self._cols
        entry = next((e for e in load_registry() if e["name"] == name), None)
        if entry is None:
            return
        cfg = effective_config(entry)
        self._cfg_name = name
        self._cfg_fields = {}
        body = ft.ListView(spacing=10, expand=True, padding=0)

        # ---- 索引格式与 Agent 授权（勾选块，替代自由文本）----
        # extensions = 用户要索引的格式（人机通用）；agent_formats = 是否允许
        # AI Agent 自动索引其中"需要转换"的非文本格式（单一开关，一次授权长期
        # 有效，取消即收回）。存储仍是列表：开=全部已启用二进制格式，关=空。
        cur_exts = set(cfg["extensions"])
        cur_agent = set(cfg["agent_formats"])
        self._fmt_boxes = {}
        self._agent_switch = None

        def _refresh_agent_switch():
            has_binary = any(self._fmt_boxes[f].value for f in ("pdf", "docx"))
            self._agent_switch.disabled = not has_binary
            if not has_binary:
                self._agent_switch.value = False

        def _fmt_toggle(_fmt):
            def handler(_e):
                _refresh_agent_switch()
            return handler

        fmt_row = ft.Row([], spacing=14, wrap=True)
        for fmt in ("md", "txt", "pdf", "docx"):
            cb = ft.Checkbox(label=fmt, value=fmt in cur_exts,
                             on_change=_fmt_toggle(fmt))
            self._fmt_boxes[fmt] = cb
            fmt_row.controls.append(cb)
        self._agent_switch = ft.Checkbox(
            label="允许 AI Agent 自动索引非文本格式（pdf / docx）",
            value=bool(cur_agent),
            disabled=not any(f in cur_exts for f in ("pdf", "docx")))
        body.controls.append(ft.Column([
            ft.Text("索引文件格式", size=12, weight=ft.FontWeight.W_600,
                    color=colors["t2"], font_family=FONT_UI),
            fmt_row,
            ft.Text("AI Agent 权限（关 = 非文本格式仅由你手动索引入库；"
                    "开 = 一次授权长期有效，取消勾选即收回；"
                    "扫描件 PDF 的 OCR 处理取决于设置中的 pdf_scan_backend）",
                    size=11, color=colors["t4"], font_family=FONT_UI, height=1.4),
            self._agent_switch,
        ], spacing=6))

        for key in ("exclude_dirs", "exclude_files", "exclude_patterns",
                    "chunk_char_limit", "short_doc_char_limit",
                    "collection"):
            raw = entry.get(key)
            if key in _LIB_LIST_KEYS:
                val = ", ".join(str(x) for x in cfg[key]) if cfg[key] else ""
            elif raw is None:
                val = ""
            else:
                val = str(raw)
            if raw is None:
                helper = "（留空 = 继承全局）"
            elif key != "collection":
                helper = "当前覆盖值，清空后保存恢复继承全局"
            else:
                helper = "当前覆盖值（仅 collection 不可恢复继承）"
            inp = ft.TextField(
                label=_LIB_CONFIG_NAMES[key], value=val,
                height=46, dense=True, border_radius=SIZE["radius_control"],
                filled=True, fill_color=colors["sunken"],
                border_color=colors["border"], text_size=13, expand=True,
                text_style=ft.TextStyle(font_family=FONT_UI),
                label_style=ft.TextStyle(size=12, font_family=FONT_UI),
                helper=helper,
            )
            self._cfg_fields[key] = inp
            body.controls.append(inp)
        body.controls.append(ft.Text(
            "提示：改动在下次索引时生效；collection 改动需先删除旧向量库再全量重建。",
            size=11, color=colors["t4"], font_family=FONT_UI, height=1.4))
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("库配置 · %s" % name, size=16, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Container(content=ft.Column([body]), width=520, height=430),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_sub(dlg)),
                ft.FilledButton(
                    "保存", style=ft.ButtonStyle(
                        bgcolor=colors["accent"], color=colors["on_accent"],
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600)),
                    on_click=lambda _: self._do_config(dlg),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        self._show_sub(dlg)

    def _do_config(self, dlg):
        from library import set_config, unset_config
        errors = []
        # 1) 格式勾选块：extensions + agent_formats（开关关/格式取消 = 授权清空）
        sel_exts = [f for f in ("md", "txt", "pdf", "docx")
                    if self._fmt_boxes[f].value]
        sel_agent = ([f for f in ("pdf", "docx") if f in sel_exts]
                     if self._agent_switch.value else [])
        try:
            if sel_exts:
                set_config(self._cfg_name, "extensions", ",".join(sel_exts))
            else:
                unset_config(self._cfg_name, "extensions")
            if sel_agent:
                set_config(self._cfg_name, "agent_formats", ",".join(sel_agent))
            else:
                unset_config(self._cfg_name, "agent_formats")
        except ValueError as ex:
            self._set_status("保存失败：%s" % ex, "error")
            return
        # 2) 其余文本字段
        for key, inp in self._cfg_fields.items():
            val = (inp.value or "").strip()
            try:
                if val:
                    set_config(self._cfg_name, key, val)
                elif key != "collection":
                    unset_config(self._cfg_name, key)
            except ValueError as ex:
                errors.append("%s：%s" % (_LIB_CONFIG_NAMES[key], ex))
        if errors:
            self._set_status("保存失败：%s" % "；".join(errors), "error")
            return
        self._close_sub(dlg)
        self._refresh()
        self._set_status("已保存库 %s 的配置（下次索引生效）" % self._cfg_name, "ok")

    # ---- 移除 ----

    def _open_remove(self, name):
        from library import remove_library
        colors = self._cols
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("移除库 · %s" % name, size=16, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Text(
                "从注册表移除该库。\n- 仅注销：保留已建索引与指纹数据（重新注册同路径可恢复）；\n- 删除数据：同时删除该库的全部向量与指纹（不可恢复）。",
                size=13, font_family=FONT_UI, height=1.6, color=self._cols["t2"]),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_sub(dlg)),
                ft.TextButton("仅注销", on_click=lambda _: self._do_remove(dlg, name, False)),
                ft.FilledButton(
                    "注销并删除数据",
                    style=ft.ButtonStyle(
                        bgcolor=colors["danger"], color="#FFFFFF",
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI, weight=ft.FontWeight.W_600)),
                    on_click=lambda _: self._do_remove(dlg, name, True),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        self._show_sub(dlg)

    def _do_remove(self, dlg, name, drop):
        from library import remove_library
        try:
            remove_library(name, drop=drop, yes=True)
        except (ValueError, RuntimeError, OSError) as ex:
            self._set_status("移除失败：%s" % ex, "error")
            return
        self._close_sub(dlg)
        self._refresh()
        self._set_status("已移除库：%s%s" % (name, "（数据已删除）" if drop else "（数据保留）"),
                         "ok")

    # ---- 子对话框 / 工具 ----

    def _show_sub(self, dlg):
        """挂载子对话框（添加/配置/移除确认）。真实窗口走 page.show_dialog。"""
        try:
            if self._page is not None:
                self._page.show_dialog(dlg)
        except Exception:
            # 冒烟/无窗口环境：不真实挂载
            pass

    def _close_sub(self, dlg):
        try:
            dlg.open = False
            dlg.update()
        except RuntimeError:
            pass

    def _current_page(self):
        return None

    @staticmethod
    def _open_dir(path):
        try:
            import os
            os.startfile(path)
        except OSError:
            pass

    def apply(self, colors):
        self._cols = colors


def _fmt_ts(ts):
    if not ts:
        return "从未"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


# ---- 文件生效明细：逐文件看"哪些真进来了"（问题39 用户要求的手动确认手段） ----

class FileStatusDialog:
    """文件生效明细对话框（零侵入只读）：不给"382 页"这种数字，逐文件列出——

    文字索引区：正常 N 份 + 每个失败/跳过文件一行（人话原因 + 是否下轮自动重试）；
    WEMM 页级导航区：后端/服务状态 + 每份 PDF 一行（页向量数 / 渲染失败原因），
    点行直接打开那份 PDF——配合 navigate_knowledge 返回的页码翻页对答案，眼见为实。
    数据全部来自 meta 指纹文件与 wemm meta（不加载模型、不碰 Chroma，红线4）。
    """

    _REASON_TEXT = {
        "scanned": "扫描件待云端 OCR（设置里开启 MinerU 云端后，下轮索引自动重试）",
        "unreadable": "不可读（文件被占用/权限不足，解除后重新索引自动重试）",
        "extract-failed": "提取失败（损坏/加密/云端失败，下轮自动重试或修复源文件）",
        "empty": "空文件（无正文，补全内容后自动入索引）",
        "tbd": "TBD 占位过多（正文写完后自动恢复）",
        "unknown": "未知终态（请截图反馈排查）",
    }

    def __init__(self, on_open_file=None, colors=DARK):
        self._on_open_file = on_open_file or (lambda path: None)
        self._cols = colors
        self._page = None
        self._dlg = None
        self._cfg = None
        self._lib_dd = ft.Dropdown(
            label="选择库", width=280, dense=True, text_size=13,
            border_radius=SIZE["radius_control"],
        )
        # flet 0.86：Dropdown 事件名为 on_select（构造器不接受 on_change）
        self._lib_dd.on_select = lambda e: self._load(e.control.value)
        self._body = ft.Column(spacing=SIZE["gap_tight"], expand=True,
                               scroll=ft.ScrollMode.AUTO)
        self._summary = ft.Text("", size=12, color=colors["t2"], font_family=FONT_UI)
        self._build()

    # ---- 构建 ----

    def _build(self):
        body = ft.Column([
            ft.Row([self._lib_dd], spacing=10),
            self._summary,
            self._body,
        ], spacing=SIZE["gap_tight"], expand=True)
        self._dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("文件生效明细", size=18, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Container(content=body, width=780, height=520),
            actions=[ft.TextButton("关闭", on_click=self._close)],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )

    def open(self, page):
        self._page = page
        names = [r[0] for r in library_snapshot()[1]]
        self._lib_dd.options = [ft.dropdown.Option(n) for n in names]
        self._lib_dd.value = names[0] if names else None
        page.show_dialog(self._dlg)
        if names:
            self._load(names[0])
        else:
            self._summary.value = "尚未注册库，先到库管理添加。"
            self._body.controls = []
            self._safe_update()

    def _close(self, e=None):
        if self._dlg:
            self._dlg.open = False
            self._safe_update()

    def _safe_update(self):
        try:
            if self._page:
                self._page.update()
        except RuntimeError:
            pass  # 会话已销毁（GUI 关闭）：静默退出

    def apply(self, colors):
        self._cols = colors
        self._summary.color = colors["t2"]
        if self._lib_dd.value:
            self._load(self._lib_dd.value)
        self._safe_update()

    # ---- 数据加载与渲染 ----

    def _load(self, lib_name):
        colors = self._cols
        cfg = None
        for c in library_entries():
            if c["name"] == lib_name:
                cfg = c
                break
        if cfg is None:
            self._summary.value = "库不存在：%s" % lib_name
            self._body.controls = []
            self._safe_update()
            return
        self._cfg = cfg
        text = file_index_rows_for(cfg)
        wemm = wemm_status_for(cfg)
        n_fail = len(text["rows"])
        self._summary.value = (
            "库「%s」：文字索引正常 %d 份、未入索引 %d 份；页级导航 %s" %
            (lib_name, text["total"], n_fail,
             ("%d 页向量" % wemm["total_pages"]) if wemm["exists"] else "尚未建页索引"))
        controls = [self._section_header("文字索引（哪些文件没进来、为什么）",
                                         ft.Icons.FACT_CHECK_OUTLINED)]
        for rel, reason, will in text["rows"]:
            missing = not (Path(cfg["path"]) / rel).exists()
            note = "" if not missing else                 "｜⚠ 文件已不在原位置（被移动/删除，下轮索引自动清理这条记录）"
            controls.append(self._row(
                icon=ft.Icons.ERROR_OUTLINE if not missing
                else ft.Icons.REMOVE_CIRCLE_OUTLINE,
                icon_color=colors["warning"] if not missing else colors["t3"],
                title=rel,
                sub=self._REASON_TEXT.get(reason, reason) +
                    ("　✅ 下轮索引将自动重试" if will else "") + note,
                missing=missing))
        else:
            controls.append(self._plain("✔ 全部文件正常入索引，没有失败或跳过。",
                                        colors["success"]))
        controls.append(ft.Container(height=6))
        controls.append(self._section_header(
            "WEMM 页级导航（每份 PDF 建了几页向量）", ft.Icons.MAP_OUTLINED))
        backend, url = wemm_backend_state()
        if backend not in ("on", "local"):
            controls.append(self._plain(
                "页级导航未开启（wemm_backend=off）。要用：设置里开启 → "
                "命令行启动 python wemm_server.py → python wemm_indexer.py 建页库。",
                colors["t2"]))
        else:
            probe_row = self._plain("正在检测看图服务…", colors["t3"])
            controls.append(probe_row)
            self._probe_async(probe_row, url)
            if not wemm["exists"]:
                controls.append(self._plain(
                    "本库还没建页索引：跑一次索引（增量/全量）会自动建页库；"
                    "也可立即命令行运行 python wemm_indexer.py --library %s "
                    "--backend on（每页渲染成图编码入库，耗时随页数与 DPI 增长）。"
                    % lib_name, colors["t2"]))
            else:
                for rel, pages, failed, reason in wemm["rows"]:
                    missing = not (Path(cfg["path"]) / rel).exists()
                    note = "" if not missing else                         "｜⚠ 文件已不在原位置（下轮页索引自动清理这条记录）"
                    if failed:
                        controls.append(self._row(
                            icon=ft.Icons.ERROR_OUTLINE if not missing
                            else ft.Icons.REMOVE_CIRCLE_OUTLINE,
                            icon_color=colors["danger"] if not missing
                            else colors["t3"],
                            title=rel,
                            sub="渲染失败：%s（下一轮页索引自动重试）%s" % (reason, note),
                            missing=missing))
                    else:
                        controls.append(self._row(
                            icon=ft.Icons.CHECK_CIRCLE if not missing
                            else ft.Icons.REMOVE_CIRCLE_OUTLINE,
                            icon_color=colors["success"] if not missing
                            else colors["t3"],
                            title=rel,
                            sub="已建 %d 页向量%s" % (pages, note),
                            missing=missing))
                if not wemm["rows"]:
                    controls.append(self._plain("页索引 meta 为空。", colors["t2"]))
                controls.append(self._plain(
                    "手动验证：点任意一行直接打开那份 PDF，配合 navigate_knowledge "
                    "返回的页码翻到对应页对内容。", colors["t3"]))
        self._body.controls = controls
        self._safe_update()

    def _probe_async(self, row, url):
        """服务存活探测放后台线程（本机回环 HTTP，最多等 5s），完成后回 UI 更新。"""
        import threading
        colors = self._cols

        def run():
            alive, detail = wemm_service_probe(url)
            row.controls[0].icon = ft.Icons.CHECK_CIRCLE if alive \
                else ft.Icons.CANCEL_OUTLINED
            row.controls[0].icon_color = colors["success"] if alive \
                else colors["danger"]
            row.controls[1].value = "看图服务：%s" % detail
            row.controls[1].color = colors["t2"] if alive else colors["danger"]
            self._safe_update()

        threading.Thread(target=run, daemon=True, name="wemm-probe").start()

    # ---- 行渲染 ----

    def _section_header(self, text, icon):
        colors = self._cols
        return ft.Row([
            ft.Icon(icon, size=16, color=colors["accent_soft"]),
            ft.Text(text, size=13, weight=ft.FontWeight.W_600, color=colors["t1"],
                    font_family=FONT_UI),
        ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _plain(self, text, color):
        return ft.Container(
            content=ft.Text(text, size=12, color=color, font_family=FONT_UI),
            padding=ft.Padding(left=24, right=24, top=2, bottom=2))

    def _row(self, icon, icon_color, title, sub, missing=False):
        """单文件行：✔/✗ 图标 + 文件名 + 人话状态；点击打开源文件（手动确认）。

        missing=True：源文件已不在原位置（旧记录）——不可点击，行内直接说明。
        """
        colors = self._cols
        abs_path = self._rel_path(title)

        def _click(e):
            self._on_open_file(abs_path)

        # flet 0.86：Container 只有 on_hover 一个事件，e.data=='true' 为悬入
        def _on_hover(e):
            e.control.bgcolor = colors["hover"] if e.data == "true" else colors["surface"]

        return ft.Container(
            content=ft.Row([
                ft.Icon(icon, size=16, color=icon_color),
                ft.Column([
                    ft.Text(title, size=13, color=colors["t1"], font_family=FONT_UI,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(sub, size=11, color=colors["t2"], font_family=FONT_UI,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=1, expand=True),
                ft.Icon(ft.Icons.OPEN_IN_NEW, size=13, color=colors["t3"])
                if not missing else ft.Container(width=13),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=colors["surface"], border_radius=SIZE["radius_control"],
            padding=ft.Padding(left=10, right=10, top=6, bottom=6),
            on_click=None if missing else _click,
            on_hover=_on_hover,
            tooltip=abs_path,
        )

    def _rel_path(self, rel):
        """行内 rel → 磁盘绝对路径（打开回调用）。"""
        try:
            return str(Path(self._cfg["path"]) / rel)
        except Exception:
            return rel


# ---- 提取试验台：单文件转译效果预览（选文件 → 实时看 Markdown 产出） ----

class ExtractLabDialog:
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
        self._run_id = 0  # 自增运行代号：轮询线程据此认领"这是不是我那一轮"

    # ---- 打开 / 构建 ----

    @staticmethod
    def _sweep_orphan_preview_dirs():
        """清扫 %TEMP% 下 >24h 的孤儿 extract_preview_*（父进程自己也崩过的残留）。

        对齐 extractors._sweep_orphan_tmp 的定位：纯卫生措施，失败一律静默。
        24h 阈值保证绝不会碰到任何正在跑的预览。
        """
        import shutil
        import tempfile
        from pathlib import Path as _P
        try:
            cutoff = time.time() - 24 * 3600
            for d in _P(tempfile.gettempdir()).glob("extract_preview_*"):
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass

    def open(self, page):
        if self._dlg is None:
            self._build()
        self._sweep_orphan_preview_dirs()
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
        """扫描件分支下，本次运行实际生效的后端（dropdown 覆盖，或跟随全局
        pdf_scan_backend）。"""
        val = getattr(self, "_dd_backend", None)
        sel = val.value if val is not None else "auto"
        if sel == "auto":
            import extractors as ex
            return ex.get_scan_backend()
        return sel

    def _effective_text_backend(self):
        """有文字层 PDF 分支下，本次运行实际生效的后端（同一个下拉覆盖，或跟随
        全局 pdf_text_backend）——与 `_effective_backend()` 对应扫描件分支是
        两个独立的全局配置键，dropdown=auto 时可能给出不同答案（如只给文字层
        开了云端、扫描件仍关）。dropdown 为具体值时两个分支共用同一个覆盖值
        （extractors._extract_pdf 对文字层分支的判断只认 "是不是 mineru-cloud"，
        "none" 这类扫描件专用取值在文字层分支天然落回本地，见该函数注释）。
        """
        val = getattr(self, "_dd_backend", None)
        sel = val.value if val is not None else "auto"
        if sel == "auto":
            import extractors as ex
            return ex.get_text_backend()
        return sel

    def _will_call_cloud(self):
        """本次提取是否可能把文件送 MinerU 云端——上传前二次确认弹窗据此判断。

        文件在真正打开前不知道是扫描件还是有文字层，两者分别跟随不同的全局配置键；
        dropdown=auto 时二者可能不一致（如用户只为文字层 PDF 开了云端换结构识别，
        扫描件 OCR 仍关）。只要有一个分支会送云端就必须先问，宁可多问一次也不能
        漏问——否则文件会在用户不知情的情况下被上传到第三方（AGENTS.md 架构红线 7）。
        """
        return (self._effective_backend() == "mineru-cloud"
                or self._effective_text_backend() == "mineru-cloud")

    def _hint_text(self):
        base = ("与索引用同一条提取逻辑；预览使用独立临时缓存，"
                "不写入/读取生产缓存，也不写入索引。")
        tail = {"none": "当前后端：本地直提——扫描件将被跳过并说明原因。",
                "mineru-cloud": "当前后端：MinerU 云端——每个扫描件可能需要数十秒。",
                "mineru-local": "本地部署属 R3b 尚未支持，扫描件将跳过。",
                }.get(self._effective_backend(), "")
        # 上面这段只讲扫描件；有文字层的 PDF 走另一个独立配置键（pdf_text_backend），
        # 同一个下拉现在对它也生效（详见 _effective_text_backend），必须一并说明，
        # 否则用户拿一份文字层文件预览时读不到任何与它实际相关的说明。
        text_tail = {
            "local": "有文字层的 PDF 本地直提。",
            "none": "有文字层的 PDF 本地直提。",
            "mineru-cloud": "有文字层的 PDF 会送 MinerU 云端换版面/表格识别"
                            "（不重复计费 OCR），可能需要数十秒。",
            "mineru-local": "本地部署模型入口占位、尚未实现，"
                            "有文字层的 PDF 会自动退化为本地直提。",
        }.get(self._effective_text_backend(), "")
        return base + tail + ("　" + text_tail if text_tail else "")

    def _refresh_hint(self):
        if getattr(self, "_hint", None) is not None:
            self._hint.value = self._hint_text()
            self._safe_update(self._hint)
        self._refresh_backend_ui()

    def _refresh_backend_ui(self):
        if getattr(self, "_backend_chip", None) is not None:
            self._backend_chip.content.value = \
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
        # 云端后端 = 真实文件将离开本机上传第三方，必须先取得用户明示同意。
        # 扫描件（pdf_scan_backend）与文字层 PDF（pdf_text_backend）任一会送云端
        # 都要确认——文件类型要打开后才知道，此刻不能只看其中一个分支。
        # 此处尚未改动 _busy / 按钮状态，取消分支无需复位任何东西。
        if self._will_call_cloud():
            self._confirm_cloud()
            return
        self._start_extraction()

    def _confirm_cloud(self):
        """云端 OCR 上传前的二次确认框（确认后才真正启动提取）。"""
        c = self._cols
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认上传到云端", size=16, weight=ft.FontWeight.W_600,
                          font_family=FONT_UI),
            content=ft.Text(
                "该文件将被上传至第三方服务 MinerU（mineru.net）进行版面/OCR 识别"
                "（具体处理方式取决于文件是否已有文字层：扫描件走 OCR，已有文字层的"
                "PDF 只做版面/表格结构识别）。\n"
                "上传的是原始文件本身，请确认它不含你不希望外传的敏感内容。\n"
                "是否继续？",
                size=13, font_family=FONT_UI, height=1.6, color=c["t2"]),
            actions=[
                ft.TextButton("取消", on_click=lambda _e: self._close_confirm(dlg)),
                ft.FilledButton(
                    "确认上传并提取",
                    style=ft.ButtonStyle(
                        bgcolor=c["accent"], color=c["on_accent"],
                        shape=ft.RoundedRectangleBorder(radius=SIZE["radius_control"]),
                        text_style=ft.TextStyle(font_family=FONT_UI,
                                                weight=ft.FontWeight.W_600)),
                    on_click=lambda _e: self._confirmed_start(dlg),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=SIZE["radius_panel"]),
        )
        try:
            if self._page is not None:
                self._page.show_dialog(dlg)
        except Exception:
            pass  # 冒烟/无窗口环境：不真实挂载

    @staticmethod
    def _close_confirm(dlg):
        try:
            dlg.open = False
            dlg.update()
        except (RuntimeError, AttributeError):
            pass

    def _confirmed_start(self, dlg):
        self._close_confirm(dlg)
        self._start_extraction()

    def _start_extraction(self):
        """真正启动子进程提取（"直接开始"与"确认后开始"共用同一条路径）。

        防重入下沉到这里，不再只靠调用方：_run 的入口检查管不到确认框那条路径
        （弹框后 _run 直接 return，_busy 仍是 False），确认按钮被派发两次
        （双击 / 触屏重复事件）就会起两个子进程、把同一份文件重复上传第三方。
        "检查 + 置位"必须紧挨着放在唯一的启动入口里，以后新增调用路径也漏不掉。
        """
        import multiprocessing as mp
        import tempfile
        if self._busy:
            return
        self._busy = True
        self._btn_run.disabled = True
        self._btn_run.content = "提取中…"
        self._btn_pick.disabled = True  # 运行中锁定文件选择，防状态错乱
        # 每一轮 run 发一个自增代号 + 一个全新的取消事件：轮询线程只认自己那一轮，
        # 关闭对话框后快速重开再点提取，旧线程不会误杀新进程、不会串改新 UI 状态。
        self._run_id += 1
        run_id = self._run_id
        stop_event = threading.Event()
        self._stop = stop_event
        self._t0 = time.monotonic()
        backend = self._dd_backend.value
        budget = self._budget_for(backend)
        self._progress.visible = True
        self._live_row.visible = True
        self._live_text.value = self._live_label(budget)
        self._set_chips([])

        q = mp.Queue()
        from extractors import _preview_job
        # 临时缓存目录由父进程创建、父进程清理：子进程会被 terminate() 硬杀，
        # 杀掉的进程执行不到任何 Python 收尾，自删承诺必然落空（见 _preview_job）。
        try:
            tmp_dir = tempfile.mkdtemp(prefix="extract_preview_")
        except OSError:
            tmp_dir = None  # 建不出来就退回子进程自建自清的老路径
        proc = mp.Process(target=_preview_job,
                          args=(q, str(self._file_path),
                                None if backend == "auto" else backend,
                                tmp_dir),
                          daemon=True)
        self._proc = proc
        proc.start()
        threading.Thread(target=self._poll,
                         args=(q, budget, run_id, proc, stop_event, tmp_dir),
                         daemon=True, name="extract-lab-poll").start()
        self._safe_update(self._btn_run, self._btn_pick,
                          self._live_row, self._chips)

    def _live_label(self, budget):
        return (f"⏱ {time.monotonic() - self._t0:.0f}s 运行中"
                f" · 超时预算 ~{int(budget)}s")

    def _poll(self, q, budget, run_id, proc, stop_event, tmp_dir=None):
        """轮询子进程结果 + 秒表心跳；超时/取消即 terminate 强杀。

        proc / stop_event / run_id 一律走参数，绝不事后从 self.* 读取——
        那些共享属性随时可能已被下一轮 run 覆盖（误杀新进程 + 串改新 UI 状态）。

        tmp_dir（父进程建的预览缓存目录）在 finally 里无条件清理：done / timeout /
        cancelled、子进程善终还是被强杀，都必然走到这一点，绝不留残留。
        """
        try:
            self._poll_loop(q, budget, run_id, proc, stop_event)
        finally:
            if tmp_dir:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)  # 纯卫生，失败不打扰

    def _poll_loop(self, q, budget, run_id, proc, stop_event):
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
            if stop_event.is_set():
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
        # 先清理"自己那一轮"的子进程——无论这一轮是否已被新一轮取代，
        # 这个进程都只有本线程持有引用，不清理就成孤儿。
        if proc is not None and proc.is_alive():
            try:
                proc.terminate()
                proc.join(timeout=5)
            except Exception:
                pass
        if run_id != self._run_id:
            return  # 用户已开新一轮：旧线程只负责清进程，绝不碰任何共享 UI 状态
        if outcome == "cancelled":
            return  # 本轮被 _close 主动取消：_close 已复位 UI，无需重复
        stop_event.set()
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
            if self._picker is not None and self._page is not None \
                    and self._picker in getattr(self._page, "overlay", []):
                self._page.overlay.remove(self._picker)
                self._page.update()
        except RuntimeError:
            pass
