"""app.py — guiweb 入口：单例守卫 + pywebview 窗口 + 1 秒状态推送线程。

用法：python guiweb/app.py（建议 pythonw）。
与原 Flet GUI（python gui/app.py）可并存：各自独立的锁文件。
架构红线遵守 AGENTS.md：本进程是零侵入观察者，不加载模型做索引、
不写 Chroma；索引/试验台均为子进程。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUI_DIR = ROOT / "gui"
UI_DIR = Path(__file__).resolve().parent / "ui"
for _p in (str(ROOT), str(GUI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---- 标准流兜底（必须在导入 store/library 等会 print 的模块之前）----
# pythonw 下 stdout/stderr 是 None：任何 print 直接 AttributeError；
# 部分终端代码页是 charmap 家族：中文 print 直接 UnicodeEncodeError。
# 两者都会炸穿 js_api 调用。统一重定向到 utf-8 日志文件。
_LOG_DIR = ROOT / "data"
try:
    _LOG_DIR.mkdir(exist_ok=True)
    _stream = open(_LOG_DIR / "guiweb_stdio.log", "a", buffering=1,
                   encoding="utf-8", errors="replace")
    sys.stdout = _stream
    sys.stderr = _stream
except OSError:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

GUI_LOCK_FILE = ROOT / "data" / "guiweb_instance.lock"
GUI_PID_FILE = ROOT / "data" / "guiweb_instance.pid"
_IS_WINDOWS = sys.platform == "win32"

_GUI_LOCK_FD = None


def _acquire_singleton():
    """单例守卫（移植 gui/app.py 同款文件字节锁，锁文件名区分两套 GUI）。"""
    import atexit

    def _err(msg):
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass

    try:
        GUI_PID_FILE.parent.mkdir(exist_ok=True)
        GUI_PID_FILE.write_text(str(__import__("os").getpid()), encoding="utf-8")
        atexit.register(_release_singleton)
    except OSError as e:
        _err("PID 文件写入失败（忽略）：%s" % e)
    try:
        f = open(GUI_LOCK_FILE, "a+b")
    except OSError as e:
        _err("锁文件打开失败（忽略，可能重复实例）：%s" % e)
        return
    try:
        if _IS_WINDOWS:
            import msvcrt
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                _err("检测到已有 guiweb 实例运行，本实例退出（单例守卫）。")
                sys.exit(0)
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _err("检测到已有 guiweb 实例运行，本实例退出（单例守卫）。")
                sys.exit(0)
        global _GUI_LOCK_FD
        _GUI_LOCK_FD = f
    except OSError as e:
        _err("单例守卫失败（继续启动）：%s" % e)


def _release_singleton():
    try:
        if GUI_PID_FILE.exists():
            cur = int(GUI_PID_FILE.read_text(encoding="utf-8").strip().split()[0])
            if cur == __import__("os").getpid():
                GUI_PID_FILE.unlink()
    except (ValueError, OSError):
        pass


def _push_loop(bridge, wnd, stop):
    """1 秒状态推送线程：snapshot 全量 + DEAD 告警由 bridge 内部触发。"""
    while not stop.is_set():
        try:
            snap = bridge.get_snapshot()
            if "error" not in snap:
                wnd.evaluate_js("window.__push && window.__push('snapshot', %s)"
                                % json.dumps(snap, ensure_ascii=False))
        except Exception:
            break  # 窗口已关闭
        stop.wait(1.0)


def main():
    _acquire_singleton()
    import webview
    from bridge import Bridge
    bridge = Bridge()
    wnd = webview.create_window(
        "Obsidian RAG",
        str(UI_DIR / "index.html"),
        js_api=bridge,
        width=1440, height=900, min_size=(1080, 700),
        background_color="#050505",
    )
    bridge.bind_window(wnd)
    stop = threading.Event()
    threading.Thread(target=_push_loop, args=(bridge, wnd, stop), daemon=True).start()
    webview.start(debug=False)
    stop.set()


if __name__ == "__main__":
    main()
