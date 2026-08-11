"""test_gui_store.py — store.py 纯逻辑单元测试（不加载模型、不碰 Chroma）。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_gui_store.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))

from gui.store import (  # noqa: E402
    STATE_NONE, STATE_OK, STATE_STALE,
    HB_DEAD, HB_DONE, HB_IDLE, HB_RUNNING, HB_STALLED,
    heartbeat_state, progress_ratio,
)


def make_progress(**kw):
    base = {
        "running": False, "phase": "done", "message": "",
        "files_total": 162, "files_done": 162,
        "chunks_total": 85, "chunks_done": 85,
        "pid": 12345, "updated_at": time.time(), "last_advance_at": time.time(),
        "elapsed_s": 38.7, "eta_s": None, "device": "cuda",
    }
    base.update(kw)
    return base


def test_heartbeat_done_and_idle():
    assert heartbeat_state(make_progress()) == HB_DONE
    assert heartbeat_state(make_progress(running=False, phase="scanning", pid=None)) == HB_IDLE
    assert heartbeat_state({}) == HB_IDLE


def test_heartbeat_running_fresh():
    now = time.time()
    assert heartbeat_state(make_progress(running=True, phase="embedding",
                                         updated_at=now, last_advance_at=now)) == HB_RUNNING


def test_heartbeat_dead_when_stale():
    now = time.time()
    assert heartbeat_state(make_progress(running=True, phase="embedding",
                                         updated_at=now - 30, last_advance_at=now - 30)) == HB_DEAD


def test_heartbeat_stalled_when_advance_stale():
    now = time.time()
    assert heartbeat_state(make_progress(running=True, phase="embedding",
                                         updated_at=now, last_advance_at=now - 60)) == HB_STALLED


def test_progress_ratio_files_and_chunks():
    p = make_progress(running=True, phase="scanning", files_done=81, files_total=162,
                      chunks_done=0, chunks_total=85)
    assert abs(progress_ratio(p) - 0.5) < 1e-9
    p = make_progress(running=True, phase="embedding", chunks_done=17, chunks_total=85,
                      files_done=162, files_total=162)
    assert abs(progress_ratio(p) - 0.2) < 1e-9


def test_progress_ratio_guards():
    assert progress_ratio({}) == 0.0
    assert progress_ratio(make_progress(files_total=0, files_done=0)) == 0.0
    assert progress_ratio(make_progress(running=True, phase="scanning",
                                        files_done=999, files_total=162)) <= 1.0


def test_state_constants():
    assert (STATE_OK, STATE_STALE, STATE_NONE) == ("ok", "stale", "none")
    assert (HB_RUNNING, HB_DEAD, HB_STALLED, HB_DONE, HB_IDLE) == \
        ("running", "dead", "stalled", "done", "idle")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS  %s" % name)
            except AssertionError as e:
                failures += 1
                print("FAIL  %s: %s" % (name, e))
    print("\n%d failures" % failures)
    sys.exit(1 if failures else 0)
