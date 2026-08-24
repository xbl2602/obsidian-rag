"""test_gui_store.py — store.py 纯逻辑单元测试（不加载模型、不碰 Chroma）。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_gui_store.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))

from gui.store import (  # noqa: E402
    STATE_NONE, STATE_OK, STATE_STALE,
    HB_DEAD, HB_DONE, HB_IDLE, HB_RUNNING, HB_STALLED,
    heartbeat_state, progress_ratio, meta_stats_for, library_state,
    library_snapshot, is_library_dir, meta_issues_for, ISSUE_TEXT,
)
from gui.app import format_elapsed, format_mmss, App  # noqa: E402 纯函数，不触发窗口
from gui.theme import DARK  # noqa: E402
from gui.widgets import _parse_src, _conf_color, _conf_label  # noqa: E402
from unittest.mock import patch


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


# ---- 多库（v3）：不加载模型，用 mock 配置与临时 meta ----

def _fake_cfg(name, path, collection=None):
    return {
        "name": name, "path": path,
        "collection": collection or "kb_%s" % name,
        "exclude_dirs": [], "exclude_files": set(),
        "exclude_patterns": (), "extensions": ["md"],
        "chunk_char_limit": 1500, "short_doc_char_limit": 200,
    }


def _fake_meta(path, files):
    """写临时 meta：{rel: {"chunks": n, ...}}。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({f: {"chunks": n} for f, n in files.items()}),
                    encoding="utf-8")


def _empty_cfg(name, path):
    return _fake_cfg(name, path)


def test_meta_stats_for():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "index_meta_A.json"
        _fake_meta(mp, {"a.md": 3, "b.md": 2, "notdict": 0})
        cfg = _fake_cfg("A", td)
        with patch("gui.store.meta_path", return_value=mp):
            files, chunks = meta_stats_for(cfg)
        assert (files, chunks) == (3, 5)  # notdict 也是 dict 条目


def test_meta_stats_for_missing():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "nope.json"
        cfg = _fake_cfg("A", td)
        with patch("gui.store.meta_path", return_value=mp):
            files, chunks = meta_stats_for(cfg)
        assert (files, chunks) == (0, 0)


def test_library_state_none_without_meta():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _fake_cfg("A", td)
        with patch("gui.store.meta_path",
                   return_value=Path(td) / "index_meta_A.json"), \
             patch("gui.store.kb_stale", return_value=(False, {})):
            st, files, chunks = library_state(cfg)
        assert st == STATE_NONE and files == 0 and chunks == 0


def test_library_state_ok_and_stale():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "index_meta_A.json"
        _fake_meta(mp, {"a.md": 3})
        cfg = _fake_cfg("A", td)
        with patch("gui.store.meta_path", return_value=mp), \
             patch("gui.store.kb_stale", return_value=(False, {})):
            assert library_state(cfg)[0] == STATE_OK
        with patch("gui.store.meta_path", return_value=mp), \
             patch("gui.store.kb_stale", return_value=(True, {})):
            assert library_state(cfg)[0] == STATE_STALE


def test_library_snapshot_aggregation():
    """聚合规则：任一 stale → stale；全 none → none；否则 ok。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        def mk(name, files):
            cfg = _fake_cfg(name, str(Path(td) / name))
            (Path(td) / name).mkdir(exist_ok=True)
            mp = Path(td) / ("index_meta_%s.json" % name)
            if files:
                _fake_meta(mp, {f: 1 for f in files})
            return cfg

        cfg_ok = mk("A", {"a.md": 1})          # meta 有数据，stale=False → ok
        cfg_none = mk("B", {})                 # 无 meta → none
        entries = [cfg_ok, cfg_none]

        def fake_entries():
            return entries

        def fake_kb_stale(vault, meta_file=None, collection_name=None, **kw):
            return False, {}

        with patch("gui.store.library_entries", side_effect=fake_entries), \
             patch("gui.store.meta_path",
                   side_effect=lambda n: Path(td) / ("index_meta_%s.json" % n)), \
             patch("gui.store.kb_stale", side_effect=fake_kb_stale):
            agg, rows = library_snapshot()
        assert agg == STATE_OK
        assert {r[0] for r in rows} == {"A", "B"}
        assert rows[0][3] == 1  # A: 1 块
        assert rows[1][3] == 0  # B: 0 块


def test_library_snapshot_stale_wins():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg_ok = _fake_cfg("A", td)
        cfg_stale = _fake_cfg("B", td)
        mp = Path(td) / "m.json"
        _fake_meta(mp, {"a.md": 1})  # 有 meta 才会走到 kb_stale
        with patch("gui.store.library_entries", return_value=[cfg_ok, cfg_stale]), \
             patch("gui.store.meta_path", return_value=mp), \
             patch("gui.store.kb_stale",
                   side_effect=[(False, {}), (True, {})]):
            agg, _ = library_snapshot()
        assert agg == STATE_STALE


def test_library_snapshot_all_none():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with patch("gui.store.library_entries", return_value=[
                _fake_cfg("A", td), _fake_cfg("B", td)]), \
             patch("gui.store.meta_path", return_value=Path(td) / "m.json"):
            agg, rows = library_snapshot()
        assert agg == STATE_NONE
        assert len(rows) == 2


def test_library_snapshot_empty_registry():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with patch("gui.store.library_entries", return_value=[]):
            agg, rows = library_snapshot()
        assert agg == STATE_NONE and rows == []


def test_is_library_dir():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        assert not is_library_dir(td)
        (Path(td) / ".obsidian").mkdir()
        assert is_library_dir(td)
        assert not is_library_dir(str(Path(td) / "nope"))
    assert not is_library_dir("")


def test_split_lib_rel():
    from gui.app import App
    assert App._split_lib_rel("Obsidian Vault/docs/foo.md") == \
        ("Obsidian Vault", "docs/foo.md")
    assert App._split_lib_rel("a/b/c.md") == ("a", "b/c.md")
    assert App._split_lib_rel("nested.md") == (None, "nested.md")  # 无斜杠 = 无前缀


def test_heartbeat_converting_stall_is_not_stalled():
    """converting 相位豁免停滞告警（与 index.progress_text 口径一致），
    但心跳停止仍判 dead——豁免不掩盖真死。"""
    stale_advance = time.time() - 999
    p = make_progress(running=True, phase="converting",
                      updated_at=time.time(), last_advance_at=stale_advance)
    assert heartbeat_state(p) == HB_RUNNING
    p2 = make_progress(running=True, phase="converting", updated_at=stale_advance)
    assert heartbeat_state(p2) == HB_DEAD
    # 非 converting 相位的同等停滞照旧判 stalled（回归保护）
    p3 = make_progress(running=True, phase="scanning",
                       updated_at=time.time(), last_advance_at=stale_advance)
    assert heartbeat_state(p3) == HB_STALLED


def test_meta_issues_for_counts_xfail_by_reason():
    import gui.store as gstore
    meta = {
        "_version": 9,
        "a.md": {"hash": "x", "chunks": 3, "size": 1, "mtime": 1, "tbd": False},
        "s1.pdf": {"hash": "h", "chunks": 0, "size": 1, "mtime": 1,
                   "tbd": False, "xfail": True, "reason": "scanned"},
        "s2.pdf": {"hash": "h", "chunks": 0, "size": 1, "mtime": 1,
                   "tbd": False, "xfail": True, "reason": "scanned"},
        "b.docx": {"hash": "h", "chunks": 0, "size": 1, "mtime": 1,
                   "tbd": False, "xfail": True, "reason": "extract-failed"},
    }
    cfg = {"name": "t"}
    with tempfile.TemporaryDirectory() as td:
        mf = Path(td) / "m.json"
        mf.write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(gstore, "meta_path", return_value=mf):
            assert meta_issues_for(cfg) == {"scanned": 2, "extract-failed": 1}
        with patch.object(gstore, "meta_path",
                          return_value=Path(td) / "nope.json"):
            assert meta_issues_for(cfg) == {}, "指纹缺失 = 无问题，不得抛异常"


def test_issue_text_covers_all_terminal_reasons():
    from gui.store import ISSUE_TEXT
    for r in ("scanned", "unreadable", "extract-failed", "empty", "tbd"):
        label, guide = ISSUE_TEXT[r]
        assert label and guide, f"reason {r} 的标签与处置指引必须齐全"


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
