"""pywebview on Python 3.14 + Windows 冒烟测试：原生窗口 + WebView2 + JS↔Python 桥。"""
import threading
import time

import webview

RESULT = {}


class Bridge:
    """暴露给前端 JS 的 Python API（window.pywebview.api.xxx）。"""

    def get_info(self):
        return {"app": "Obsidian RAG", "nodes": 63, "edges": 77, "python": "3.14 OK"}

    def ping(self, payload):
        return {"echo": payload, "ts": time.time()}


def probe():
    time.sleep(1.2)
    try:
        # JS 调 Python
        r1 = RESULT["wnd"].evaluate_js("window.pywebview.api.get_info()")
        # Python 改 DOM（验证前端是活的 WebView2）
        RESULT["wnd"].evaluate_js(
            "document.body.innerHTML += '<h1 id=probe>BRIDGE-OK</h1>'"
        )
        r2 = RESULT["wnd"].evaluate_js("document.getElementById('probe').textContent")
        RESULT["result"] = {"js_call_py": r1, "py_call_js": r2, "ok": True}
    except Exception as e:  # noqa: BLE001
        RESULT["result"] = {"ok": False, "error": repr(e)}
    RESULT["wnd"].destroy()


if __name__ == "__main__":
    wnd = webview.create_window(
        "Obsidian RAG · 冒烟测试",
        html="<html><body style='font-family:sans-serif;background:#0d0e10;"
        "color:#edeef0'><h2>pywebview 窗口测试</h2>"
        "<p>若看到此窗口且 3 秒后自动关闭，即原生窗口 + WebView2 渲染正常。</p></body></html>",
        js_api=Bridge(),
        width=640,
        height=420,
    )
    RESULT["wnd"] = wnd
    t = threading.Thread(target=probe, daemon=True)
    t.start()
    webview.start(debug=False)
    print("RESULT:", RESULT.get("result"))
