"""smoke_gui.py — 无窗口冒烟测试：不拉起真实窗口，用假 Page 完整构造 App。

能抓出绝大多数 flet 0.86 API 兼容错误（构造期 AttributeError），
避免每次都要开窗调试。真实交互（点击/刷新/搜索）仍需手动验证。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\smoke_gui.py
"""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui"))


class FakePage:
    """最小 Page 假体：只提供 App 构造期用到的接口。"""

    def __init__(self):
        self.title = ""
        self.window = SimpleNamespace()
        self.theme_mode = None
        self.theme = None
        self.padding = 16
        self.spacing = 0
        self.controls = []

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def run_task(self, coro):
        pass

    def show_dialog(self, dlg):
        pass

    def close(self, dlg):
        pass

    def set_clipboard(self, text):
        pass

    def launch_url(self, url):
        pass


class SimpleNamespace:
    pass


def main():
    import sys as _sys
    _sys.modules.pop("app", None)
    from flet.controls.page import Page  # noqa: F401 确保 flet 可导入
    import flet as ft

    ft.run = lambda main: None  # 防误触发

    import gui.app as appmod
    App = appmod.App

    ns = SimpleNamespace()
    page = FakePage()
    page.window = ns

    print("1/4 构造 App（全部控件实例化）...")
    app = App(page)
    print("    OK")

    print("2/4 模拟一次刷新循环...")
    app._refresh_once()
    print("    OK")

    print("3/4 模拟主题切换...")
    app._toggle_theme(None)
    app._toggle_theme(None)
    print("    OK")

    print("3b/6 模拟设置对话框...")
    app._open_settings(None)
    if not app.settings._fields or not app.settings._dlg:
        raise AssertionError("设置对话框未构建字段")
    print("    字段数 %d OK" % len(app.settings._fields))

    print("4/6 中排布局断言（搜索卡弹性、进度卡固定宽）：")
    from theme import SIZE
    if not app.mid_row.expand:
        raise AssertionError("中排应弹性撑满剩余高度")
    if not app.search.card.expand:
        raise AssertionError("搜索卡应横向+纵向 expand")
    if app.progress.card.width != SIZE["progress_w"]:
        raise AssertionError("进度卡宽度未固定 %s != %s"
                             % (app.progress.card.width, SIZE["progress_w"]))
    print("    中排 expand / 搜索 expand / 进度宽 %s OK" % SIZE["progress_w"])

    print("5/6 搜索完成/失败回调路径...")
    import asyncio
    from concurrent.futures import Future

    f_ok = Future()
    f_ok.set_result("[来源] test.md (## 小节标题) [块 1/3] [置信度 0.87]\n---\n正文内容\n---")
    asyncio.run(app._finish_search(f_ok))
    if app._searching or app.search.btn.disabled:
        raise AssertionError("完成后按钮未恢复")
    if not app.search.results.controls:
        raise AssertionError("完成路径未渲染结果")
    print("    完成路径 OK")

    f_bad = Future()
    f_bad.set_exception(RuntimeError("boom"))
    asyncio.run(app._finish_search(f_bad))
    if app._searching or app.search.btn.disabled:
        raise AssertionError("失败后按钮未恢复")
    print("    失败路径 OK")

    print("6/6 top_k / 正文开关 / 结果点击跳转...")
    if app.search.top_k.value not in ("3", "5", "8", "10", "15"):
        raise AssertionError("top_k 下拉异常: %r" % app.search.top_k.value)
    if app.search.body_switch.value is not True:
        raise AssertionError("正文开关默认应为开启")
    import gui.app as appmod
    opened = []
    orig_startfile = appmod.os.startfile
    appmod.os.startfile = lambda u: opened.append(u)
    try:
        app._open_result("docs/foo.md", "小节标题")
        app._open_result("docs/foo.md")
    finally:
        appmod.os.startfile = orig_startfile
    if not opened or "obsidian://open" not in opened[0]:
        raise AssertionError("点击结果未走 Obsidian URI: %r" % opened)
    print("    跳转 URI OK: %s" % opened[0])

    print("7/8 多库组件冒烟（库选择胶囊 / 库管理对话框 / 多库打开）：")
    from widgets import ALL_LIBRARIES
    if app.lib_picker.value != "":
        raise AssertionError("全部库默认 value 应为空串: %r" % app.lib_picker.value)
    if not app.lib_picker.is_all:
        raise AssertionError("默认应为全部库模式")
    if app.lib_picker.text.value != "全部库":
        raise AssertionError("胶囊文本应为全部库: %r" % app.lib_picker.text.value)
    if not app.lib_picker.card.visible:
        raise AssertionError("库选择胶囊不可见")
    if not app.lib_manager._list or app.lib_manager._dlg is None:
        raise AssertionError("库管理对话框未构建")
    app._open_library_manager(None)  # FakePage.show_dialog 已实现
    print("    胶囊默认值 / 库管理对话框构建 OK（库数 %d）" %
          len(app.lib_picker._names))

    # 库选择对话框：打开 → 勾选 1 个 → 确定 → 白名单并查
    app.lib_picker._page = page
    picked = []
    app.lib_picker._on_change = lambda c: picked.append(c)
    app.lib_picker._open(None)
    keys = list(app.lib_picker._boxes)
    if not keys:
        raise AssertionError("库选择对话框未构建选项")
    target = keys[0]
    for k, b in app.lib_picker._boxes.items():
        b.value = (k == target)
    app.lib_picker._sync_from_boxes()
    app.lib_picker._confirm(None)
    if app.lib_picker.value != target:
        raise AssertionError("白名单 value 错误: %r != %r" %
                             (app.lib_picker.value, target))
    if not picked or picked[0] != {target}:
        raise AssertionError("选择回调未触发: %r" % picked)
    print("    白名单并查选择/回调 OK: %s" % app.lib_picker.value)

    # 反选：全选后取消一个 → value = 其余库
    app.lib_picker._open(None)
    app.lib_picker._check_all(None)
    box0 = app.lib_picker._boxes[keys[0]]
    box0.value = False
    app.lib_picker._sync_from_boxes()
    app.lib_picker._confirm(None)
    expected = ",".join(sorted(keys[1:]))
    if app.lib_picker.value != expected:
        raise AssertionError("反选 value 错误: %r != %r" %
                             (app.lib_picker.value, expected))
    app.lib_picker._on_change = app._on_library_selected
    app.lib_picker._open(None)
    app.lib_picker._check_all(None)
    app.lib_picker._confirm(None)
    if app.lib_picker.value != "":
        raise AssertionError("恢复全部库失败: %r" % app.lib_picker.value)
    print("    反选（全选-1）/ 恢复全部库 OK: %s" % expected)

    # 多库打开：注册表内库名 + 非 Obsidian 库 → 系统打开（os.startfile 直开路径）
    fake_cfg = {"name": "Books", "path": "C:/fake/books"}
    app._lib_by_name["Books"] = fake_cfg
    opened = []
    appmod.os.startfile = lambda u: opened.append(u)
    try:
        with patch("gui.app.is_library_dir", return_value=False):
            app._open_result("Books/notes/a.md")
        if not opened or os.path.normpath(str(opened[0])) != \
                os.path.normpath("C:/fake/books/notes/a.md"):
            raise AssertionError("多库非 Obsidian 库未走系统打开: %r" % opened)
        with patch("gui.app.is_library_dir", return_value=True):
            app._open_result("Books/notes/b.md")
        if not opened or "obsidian://open" not in str(opened[-1]):
            raise AssertionError("多库 Obsidian 库未走 URI: %r" % opened)
    finally:
        appmod.os.startfile = orig_startfile
    print("    多库打开（系统/URI 两路）OK: %s" % [str(x)[:40] for x in opened])

    print("8/8 按库搜索参数 / 索引参数...")
    app.lib_picker._open(None)
    app.lib_picker._boxes = {k: b for k, b in app.lib_picker._boxes.items()}
    if len(app.lib_picker._names) < 1:
        raise AssertionError("无库可测")
    first = app.lib_picker._names[0]
    for k, b in app.lib_picker._boxes.items():
        b.value = (k == first)
    app.lib_picker._sync_from_boxes()
    app.lib_picker._confirm(None)
    if app._selected_library_arg() != first:
        raise AssertionError("选中库参数错误: %r" % app._selected_library_arg())
    app.lib_picker._open(None)
    app.lib_picker._check_all(None)
    app.lib_picker._confirm(None)
    if app._selected_library_arg() != "":
        raise AssertionError("全部库参数应为空串: %r" % app._selected_library_arg())
    print("    选库参数 OK")

    print("\n冒烟测试全部通过")


if __name__ == "__main__":
    main()
