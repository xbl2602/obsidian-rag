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
from gui.app import format_elapsed, format_mmss  # noqa: E402 纯函数，不触发窗口
from gui.theme import DARK  # noqa: E402
from gui.widgets import _parse_src, _conf_color, _conf_label  # noqa: E402


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


def test_format_elapsed():
    assert format_elapsed(38.7) == "38.7s"
    assert format_elapsed(0.4) == "0.4s"
    assert format_elapsed(60) == "1分0秒"
    assert format_elapsed(125) == "2分5秒"
    assert format_elapsed(None) == "—"
    assert format_elapsed(0) == "—"
    assert format_elapsed(-3) == "—"
    assert format_elapsed("x") == "—"


def test_format_mmss():
    assert format_mmss(0) == "00:00"
    assert format_mmss(59) == "00:59"
    assert format_mmss(61) == "01:01"
    assert format_mmss(3600) == "60:00"
    assert format_mmss(None) == "00:00"


def test_parse_src_full():
    rel, heading, conf = _parse_src("[来源] docs/foo.md (## 小节标题) [块 1/3]")
    assert rel == "docs/foo.md"
    assert heading == "小节标题"
    assert conf is None


def test_parse_src_with_confidence():
    rel, heading, conf = _parse_src(
        "[来源] docs/foo.md (## 小节标题) [块 1/3] [置信度 0.87]")
    assert rel == "docs/foo.md"
    assert heading == "小节标题"
    assert abs(conf - 0.87) < 1e-9


def test_parse_src_confidence_first():
    rel, heading, conf = _parse_src(
        "[来源] docs/foo.md (## 小节标题) [置信度 0.5]")
    assert rel == "docs/foo.md"
    assert heading == "小节标题"
    assert abs(conf - 0.5) < 1e-9


def test_parse_src_no_heading():
    rel, heading, conf = _parse_src("[来源] docs/foo.md [块 1/3]")
    assert rel == "docs/foo.md"
    assert heading == ""
    assert conf is None


def test_parse_src_plain():
    rel, heading, conf = _parse_src("[来源] docs/foo.md")
    assert rel == "docs/foo.md"
    assert heading == ""
    assert conf is None


def test_parse_src_spacey_path():
    rel, heading, conf = _parse_src(
        "[来源] Obsidian Vault/我的 笔记/foo.md (## 标题) [块 2/5] [置信度 0.42]")
    assert rel == "Obsidian Vault/我的 笔记/foo.md"
    assert heading == "标题"
    assert abs(conf - 0.42) < 1e-9


def test_conf_color_levels():
    assert _conf_color(0.9, DARK) == DARK["success"]
    assert _conf_color(0.75, DARK) == DARK["success"]
    assert _conf_color(0.6, DARK) == DARK["accent"]
    assert _conf_color(0.5, DARK) == DARK["accent"]
    assert _conf_color(0.3, DARK) == DARK["warning"]


def test_conf_label():
    assert _conf_label(None) == ""
    assert _conf_label(0.874) == "87%"
    assert _conf_label(0.5) == "50%"
    assert _conf_label(0.004) == "0%"


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
