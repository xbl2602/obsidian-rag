"""theme.py — 控制台主题 token 体系（深空灰 + 薄荷青，深浅两套）。

所有组件只引用这里的色值/尺寸，不直接写裸 hex。
透明度统一用 ft.Colors.with_opacity 生成。
"""

# ---- 深色（默认）：深空灰三层底 + 薄荷青 ----
DARK = {
    "base": "#0E1117",          # 窗口底色
    "surface": "#161B23",       # 卡片
    "sunken": "#11151C",        # 输入框 / 日志区内底
    "hover": "#1C222C",         # 悬停
    "active": "#212936",        # 按下 / 选中
    "border": "#232B37",        # 默认 1px 描边
    "border_light": "#2D3744",  # 卡片上缘高光边
    "border_faint": "#1A212B",  # 弱分隔 / 结果卡
    "accent": "#4BCEB8",        # 主色 mint-teal
    "accent_hover": "#5FDCC7",
    "accent_soft": "#8CE5D6",   # 强调图标 / 强调文字
    "on_accent": "#04261F",     # 主按钮上的深字
    "success": "#3DD68C",
    "warning": "#F0B24B",
    "danger": "#F2555A",
    "scan": "#4FA8E8",          # 扫描阶段
    "t1": "#E9EDF3",            # 主文字
    "t2": "#A3ADBD",            # 次级文字
    "t3": "#667180",            # 弱信息
    "t4": "#465163",            # 极弱（时间戳/占位）
    "error_bg": "#F2555A",
    "tooltip_bg": "#1D222B",
}

# ---- 浅色：同结构换色 ----
LIGHT = {
    "base": "#F4F6F8",
    "surface": "#FFFFFF",
    "sunken": "#EEF1F5",
    "hover": "#F0F3F7",
    "active": "#E7EBF1",
    "border": "#D9DEE6",
    "border_light": "#FFFFFF",
    "border_faint": "#E4E8EE",
    "accent": "#2FA896",
    "accent_hover": "#25947F",
    "accent_soft": "#0E7A66",
    "on_accent": "#FFFFFF",
    "success": "#1E9E6A",
    "warning": "#C77E10",
    "danger": "#D94045",
    "scan": "#2F7FD0",
    "t1": "#1B212B",
    "t2": "#57606E",
    "t3": "#8B94A2",
    "t4": "#ABB3BF",
    "error_bg": "#D94045",
    "tooltip_bg": "#1D222B",
}

# ---- 尺寸（新版布局：所有可见区块都有固定高度锚点） ----
SIZE = {
    "pad": 16,                  # 内容区 padding
    "gap": 12,                  # 区块间距
    "gap_tight": 8,

    "radius_panel": 14,         # 面板卡
    "radius_kpi": 16,           # KPI 卡
    "radius_control": 10,       # 按钮 / 输入框
    "radius_badge": 7,
    "radius_pill": 999,

    "header_h": 48,
    "kpi_h": 92,
    "mid_h": 280,               # 中排参考高度（保留，防止组件极端塌陷）
    "progress_w": 460,          # 进度卡固定宽度；其余横向空间全给搜索卡
    "device_h": 40,
    "log_h": 260,               # 日志区固定高度（搜索卡改为弹性后日志不再独占剩余）

    "kpi_num": 30,
    "pct_num": 36,
    "bar_h": 8,
    "input_h": 44,

    "dot": 8,
    "pill_h": 30,
    "pill_pad_x": 10,
    "chip_h": 22,
    "log_line_h": 22,

    "window_w": 1280,
    "window_h": 800,
    "window_min_w": 1024,
    "window_min_h": 680,
}

FONT_UI = "Microsoft YaHei UI"
FONT_NUM = "Bahnschrift"
FONT_MONO = "Consolas"