"""smoke_gui.py — 无窗口冒烟测试：不拉起真实窗口，用假 Page 完整构造 App。

能抓出绝大多数 flet 0.86 API 兼容错误（构造期 AttributeError），
避免每次都要开窗调试。真实交互（点击/刷新/搜索）仍需手动验证。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\smoke_gui.py
"""
import sys
import time
from pathlib import Path

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
    from flet.controls.page import Page  # noqa: F401 确保 flet 可导入
    import flet as ft

    ft.run = lambda main: None  # 防误触发

    from app import App

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

    print("4/4 模拟确认框...")
    app._confirm_full(None)
    print("    OK")

    print("\n冒烟测试全部通过")


if __name__ == "__main__":
    main()
