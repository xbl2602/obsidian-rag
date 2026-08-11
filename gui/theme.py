"""theme.py — 语义搜索控制台的色板、字体与尺寸常量（深浅两套，Material 3）。"""

# 深色模式（默认）
DARK = {
    "background": "#121212",
    "surface": "#1E1E1E",
    "surface_high": "#2B2B2B",
    "outline": "#333333",
    "primary": "#4DB6AC",
    "primary_fill": "#00695C",
    "primary_hover": "#00796B",
    "primary_container": "#00695C",
    "on_primary": "#FFFFFF",
    "success": "#66BB6A",
    "warning": "#FFB74D",
    "danger": "#EF5350",
    "disabled": "#616161",
    "text_main": "#E0E0E0",
    "text_secondary": "#9E9E9E",
    "text_weak": "#757575",
    "log_bg": "#131313",
    "scan": "#64B5F6",
    "error_bg": "#C62828",
}

# 浅色模式
LIGHT = {
    "background": "#FAFAFA",
    "surface": "#FFFFFF",
    "surface_high": "#F1F3F4",
    "outline": "#E0E0E0",
    "primary": "#00695C",
    "primary_fill": "#00695C",
    "primary_hover": "#00796B",
    "primary_container": "#B2DFDB",
    "on_primary": "#FFFFFF",
    "success": "#2E7D32",
    "warning": "#EF6C00",
    "danger": "#C62828",
    "disabled": "#BDBDBD",
    "text_main": "#212121",
    "text_secondary": "#616161",
    "text_weak": "#9E9E9E",
    "log_bg": "#FAFAFA",
    "scan": "#1E88E5",
    "error_bg": "#C62828",
}

# 尺寸规范
SIZE = {
    "radius_card": 12,
    "radius_control": 8,
    "radius_pill": 16,
    "gap_card": 16,
    "pad_card": 16,
    "gap_element": 12,
    "gap_tight": 8,
    "kpi_height": 104,
    "btn_height": 40,
    "input_height": 44,
    "device_bar_height": 44,
    "log_min_height": 160,
    "header_height": 56,
    "dot_status": 12,
    "dot_heartbeat": 10,
    "bar_height": 8,
    "result_max_lines": 3,
    "log_max_lines": 1000,
    "window_w": 1280,
    "window_h": 800,
    "window_min_w": 1024,
    "window_min_h": 680,
}

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Cascadia Mono"
