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
    STALL_TIMEOUT,
    heartbeat_state, heartbeat_note, progress_ratio, meta_stats_for,
    library_state,
    library_snapshot, is_library_dir, meta_issues_for, ISSUE_TEXT,
    note_relations_for,
    file_index_rows_for, wemm_status_for, wemm_service_probe,
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


def test_note_relations_for():
    """note_relations_for：照 meta_issues_for 的模式，打桩 meta_path 指向临时
    meta.json，验证出链/入链解析正确；meta 缺失时静默返回 resolved=False。"""
    import gui.store as gstore
    meta = {
        "_version": 9,
        "a.md": {"hash": "x", "chunks": 3, "size": 1, "mtime": 1, "tbd": False,
                 "links": ["b"]},
        "b.md": {"hash": "y", "chunks": 2, "size": 1, "mtime": 1, "tbd": False,
                 "links": []},
    }
    cfg = {"name": "t"}
    with tempfile.TemporaryDirectory() as td:
        mf = Path(td) / "m.json"
        mf.write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(gstore, "meta_path", return_value=mf):
            rel_a = note_relations_for(cfg, "a.md")
            assert rel_a == {"resolved": True, "file": "a.md",
                             "outlinks": ["b.md"], "inlinks": []}
            # 不含扩展名的标题也能命中（按文件 stem 匹配）
            rel_b = note_relations_for(cfg, "b")
            assert rel_b == {"resolved": True, "file": "b.md",
                             "outlinks": [], "inlinks": ["a.md"]}
        with patch.object(gstore, "meta_path",
                          return_value=Path(td) / "nope.json"):
            missing = note_relations_for(cfg, "a.md")
            assert missing == {"resolved": False, "file": None,
                               "outlinks": [], "inlinks": []}, \
                "指纹缺失 = 未找到，不得抛异常"


def test_issue_text_covers_all_terminal_reasons():
    from gui.store import ISSUE_TEXT
    for r in ("scanned", "unreadable", "extract-failed", "empty", "tbd"):
        label, guide = ISSUE_TEXT[r]
        assert label and guide, f"reason {r} 的标签与处置指引必须齐全"


# ---------- 云端上传二次确认 + 试验台轮询竞态（flet 控件无窗口构造，仿 smoke_gui） ----------

class _RecPage:
    """记录型假 Page：只提供确认框用到的接口（对齐 tests/smoke_gui.FakePage）。"""

    def __init__(self):
        self.dialogs = []
        self.overlay = []

    def show_dialog(self, dlg):
        self.dialogs.append(dlg)

    def close(self, dlg):
        pass

    def update(self):
        pass


def _click(dlg, label):
    """触发对话框 actions 里指定文案按钮的 on_click（模拟用户点击）。"""
    for btn in dlg.actions:
        if getattr(btn, "content", None) == label:
            btn.on_click(None)
            return True
    raise AssertionError("确认框里找不到按钮：%s（实有 %s）"
                         % (label, [getattr(b, "content", None) for b in dlg.actions]))


def _make_lab():
    from gui.widgets import ExtractLabDialog
    dlg = ExtractLabDialog()
    dlg._build()
    dlg._page = _RecPage()
    dlg._file_path = "C:/tmp/x.pdf"
    started = []
    dlg._start_extraction = lambda: started.append(1)
    return dlg, started


def test_extract_lab_cloud_backend_requires_confirm():
    """试验台选云端后端：必须先弹确认框，确认后才真正启动子进程提取。"""
    dlg, started = _make_lab()
    dlg._dd_backend.value = "mineru-cloud"
    dlg._run()
    assert not started, "云端后端不得未经确认就上传文件"
    assert len(dlg._page.dialogs) == 1, "应弹出且只弹出一个确认框"
    confirm = dlg._page.dialogs[0]
    assert "MinerU" in confirm.content.value, "确认文案须点名第三方服务"
    _click(confirm, "确认上传并提取")
    assert started == [1], "确认后应启动提取，且只启动一次"


def test_extract_lab_cloud_confirm_cancel_does_not_start():
    """点取消：不启动提取，也不改动忙态/按钮（判断发生在改状态之前）。"""
    dlg, started = _make_lab()
    dlg._dd_backend.value = "mineru-cloud"
    dlg._run()
    _click(dlg._page.dialogs[0], "取消")
    assert not started, "取消后绝不能启动提取"
    assert dlg._busy is False and dlg._btn_run.disabled is False, \
        "取消不得残留忙态"


def test_extract_lab_local_backend_starts_directly():
    """非云端后端（none）：不弹确认框，直接开跑。"""
    dlg, started = _make_lab()
    dlg._dd_backend.value = "none"
    dlg._run()
    assert started == [1], "本地直提应直接启动"
    assert dlg._page.dialogs == [], "本地直提不该打扰用户"


def test_extract_lab_auto_backend_confirms_when_only_text_backend_is_cloud():
    """问题30 回归：dropdown=auto 时，若只有 pdf_text_backend（而非 pdf_scan_backend）
    被设为 mineru-cloud，也必须先弹确认框。

    文件在选中前不知道是扫描件还是有文字层，若确认判断只看 pdf_scan_backend，
    会在"只给文字层 PDF 开云端结构识别、扫描件 OCR 仍关"这个组合下漏问——文件
    会在用户不知情的情况下被上传到第三方（_will_call_cloud 必须同时检查两个键）。
    """
    import config as cfgmod
    dlg, started = _make_lab()
    dlg._dd_backend.value = "auto"
    saved_scan = cfgmod.CFG.get("pdf_scan_backend")
    saved_text = cfgmod.CFG.get("pdf_text_backend")
    cfgmod.CFG["pdf_scan_backend"] = "none"
    cfgmod.CFG["pdf_text_backend"] = "mineru-cloud"
    try:
        dlg._run()
        assert not started, "只要文字层分支会送云端，auto 模式也必须先确认，不能漏问"
        assert len(dlg._page.dialogs) == 1
        confirm = dlg._page.dialogs[0]
        assert "MinerU" in confirm.content.value
        _click(confirm, "确认上传并提取")
        assert started == [1], "确认后应启动提取"
    finally:
        for k, v in (("pdf_scan_backend", saved_scan), ("pdf_text_backend", saved_text)):
            if v is None:
                cfgmod.CFG.pop(k, None)
            else:
                cfgmod.CFG[k] = v


def test_extract_lab_auto_backend_no_confirm_when_both_local():
    """对照组：dropdown=auto 且两个后端都在本地默认值（none / local）时不弹确认框——
    防止上一条回归的修复矫枉过正，把默认场景也变得需要多余确认。"""
    import config as cfgmod
    dlg, started = _make_lab()
    dlg._dd_backend.value = "auto"
    saved_scan = cfgmod.CFG.get("pdf_scan_backend")
    saved_text = cfgmod.CFG.get("pdf_text_backend")
    cfgmod.CFG["pdf_scan_backend"] = "none"
    cfgmod.CFG["pdf_text_backend"] = "local"
    try:
        dlg._run()
        assert started == [1], "两个后端都是本地默认值时应直接开跑，不打扰用户"
        assert dlg._page.dialogs == []
    finally:
        for k, v in (("pdf_scan_backend", saved_scan), ("pdf_text_backend", saved_text)):
            if v is None:
                cfgmod.CFG.pop(k, None)
            else:
                cfgmod.CFG[k] = v


def _make_settings(cur_backend, new_backend):
    """构造 SettingsDialog 并 stub _apply_updates（严禁真写 data/config.json）。"""
    from gui.widgets import SettingsDialog
    import config as cfgmod
    dlg = SettingsDialog(on_saved=lambda *_: None)
    dlg._page = _RecPage()
    applied = []
    dlg._apply_updates = lambda updates: applied.append(updates)
    dlg._fields["pdf_scan_backend"][0].value = new_backend
    saved = cfgmod.CFG.get("pdf_scan_backend")
    cfgmod.CFG["pdf_scan_backend"] = cur_backend
    return dlg, applied, cfgmod, saved


def test_settings_cloud_backend_switch_requires_confirm():
    """设置页 none → mineru-cloud：必须先确认（存量扫描件会被批量外传）。"""
    dlg, applied, cfgmod, saved = _make_settings("none", "mineru-cloud")
    try:
        dlg._save(None)
        assert not applied, "切云端后端不得未经确认就落盘"
        assert len(dlg._page.dialogs) == 1
        confirm = dlg._page.dialogs[0]
        assert "MinerU" in confirm.content.value
        _click(confirm, "确认启用")
        assert len(applied) == 1, "确认后才真正 apply_updates"
        assert applied[0]["pdf_scan_backend"][1] == "mineru-cloud"
    finally:
        if saved is None:
            cfgmod.CFG.pop("pdf_scan_backend", None)
        else:
            cfgmod.CFG["pdf_scan_backend"] = saved


def test_settings_same_backend_no_confirm():
    """本来就是 mineru-cloud：改别的字段保存不再打扰用户，直接落盘。"""
    dlg, applied, cfgmod, saved = _make_settings("mineru-cloud", "mineru-cloud")
    try:
        dlg._save(None)
        assert len(applied) == 1, "无后端切换应直接 apply"
        assert dlg._page.dialogs == [], "无变化不该弹确认框"
    finally:
        if saved is None:
            cfgmod.CFG.pop("pdf_scan_backend", None)
        else:
            cfgmod.CFG["pdf_scan_backend"] = saved


def test_settings_text_backend_switch_requires_confirm():
    """问题30：设置页仅切 pdf_text_backend（none/local → mineru-cloud）同样必须先
    确认——这是新增的键，与既有 pdf_scan_backend 共用同一套确认机制
    （SettingsDialog._save 的 newly_cloud 检查两个键），不能因为是新键就绕过。
    """
    from gui.widgets import SettingsDialog
    import config as cfgmod
    dlg = SettingsDialog(on_saved=lambda *_: None)
    dlg._page = _RecPage()
    applied = []
    dlg._apply_updates = lambda updates: applied.append(updates)
    dlg._fields["pdf_text_backend"][0].value = "mineru-cloud"
    saved = cfgmod.CFG.get("pdf_text_backend")
    cfgmod.CFG["pdf_text_backend"] = "local"
    try:
        dlg._save(None)
        assert not applied, "切云端文字层后端不得未经确认就落盘"
        assert len(dlg._page.dialogs) == 1
        confirm = dlg._page.dialogs[0]
        assert "MinerU" in confirm.content.value
        assert "文字层" in confirm.content.value, "确认文案须点名这次触发的是文字层分支"
        _click(confirm, "确认启用")
        assert len(applied) == 1, "确认后才真正 apply_updates"
        assert applied[0]["pdf_text_backend"][1] == "mineru-cloud"
    finally:
        if saved is None:
            cfgmod.CFG.pop("pdf_text_backend", None)
        else:
            cfgmod.CFG["pdf_text_backend"] = saved


class _FakeProc:
    """duck-type 子进程：只需 is_alive/terminate/join，记录是否被强杀。"""

    def __init__(self, alive=True):
        self.alive = alive
        self.terminated = False
        self.joined = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout=None):
        self.joined = True


class _Stub:
    """轻量控件桩：_safe_update 捕获全部异常，属性读写够用即可。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_extract_lab_poll_stale_run_only_cleans_up():
    """旧轮次的轮询线程：只清理自己的子进程，绝不碰已属于新一轮的共享 UI 状态。

    竞态复现（修复前）：关对话框 → _close 提前把 _busy 清空 → 用户秒开再点提取
    （run2）→ 旧线程 T1 读 self._proc 读到 proc2，误杀刚启动的新进程，
    真正该杀的 proc1 成孤儿，且 T1 继续复位按钮/渲染假失败覆盖新一轮 UI。
    """
    import queue as _q
    import threading as _th
    from gui.widgets import ExtractLabDialog
    dlg = ExtractLabDialog()           # 不 _build/_open：__init__ 只有普通赋值
    dlg._busy = True
    dlg._run_id = 2                    # 已经有更新的一轮在跑
    proc1 = _FakeProc(alive=True)
    ev = _th.Event()
    ev.set()                           # 让 _poll 立刻走 cancelled 分支跳出循环
    dlg._poll(_q.Queue(), 30, 1, proc1, ev)
    assert proc1.terminated, "旧轮次的子进程必须被清理，不得成孤儿"
    assert dlg._busy is True, "run_id 失配时不得改动共享忙态（会污染新一轮）"


def test_extract_lab_poll_current_run_finishes():
    """当前轮次：正常走到底，复位忙态并渲染结果。"""
    import queue as _q
    import threading as _th
    from gui.widgets import ExtractLabDialog
    dlg = ExtractLabDialog()
    dlg._busy = True
    dlg._run_id = 1
    for name in ("_btn_run", "_btn_pick", "_progress", "_live_row",
                 "_md_view", "_src_view"):
        setattr(dlg, name, _Stub(disabled=False, visible=True, value="",
                                 content=""))
    dlg._chips = _Stub(controls=[])
    q = _q.Queue()
    q.put({"ok": True, "info": {"md": "# 结果\n正文", "reason": "",
                                "route": "local", "cached": False,
                                "elapsed": 0.1, "chars": 6}})
    dlg._poll(q, 30, 1, _FakeProc(alive=False), _th.Event())
    assert dlg._busy is False, "当前轮次结束必须复位忙态"
    assert dlg._btn_run.disabled is False and dlg._progress.visible is False


def test_extract_lab_start_extraction_is_reentrant_safe():
    """确认按钮被派发两次也只起一次提取：防重入下沉在 _start_extraction 内部。

    _run 的入口检查管不到确认框那条路径（弹框后 _run 直接 return，_busy 仍是
    False），双击/触屏重复事件会让同一份文件被重复上传第三方 OCR。
    """
    import gui.widgets as W
    from gui.widgets import ExtractLabDialog
    dlg = ExtractLabDialog()
    dlg._file_path = "C:/tmp/x.pdf"
    dlg._dd_backend = _Stub(value="none")
    for name in ("_btn_run", "_btn_pick", "_progress", "_live_row",
                 "_live_text"):
        setattr(dlg, name, _Stub(disabled=False, visible=False, value="",
                                 content=""))
    dlg._chips = _Stub(controls=[])
    spawned = []

    class _P:
        def __init__(self, **kw):
            spawned.append(kw)

        def start(self):
            pass

    class _T:
        def __init__(self, **kw):
            pass

        def start(self):
            pass

    orig_mp = W.threading.Thread
    import multiprocessing as mp
    orig_proc = mp.Process
    try:
        mp.Process = lambda **kw: _P(**kw)
        W.threading.Thread = lambda **kw: _T(**kw)
        dlg._start_extraction()
        dlg._start_extraction()          # 模拟确认按钮被触发两次
    finally:
        mp.Process = orig_proc
        W.threading.Thread = orig_mp
    assert len(spawned) == 1, "重复触发只能真正启动一次提取（不得重复上传）"
    assert dlg._run_id == 1, "第二次调用不得再推进运行代号"
    tmp_dir = spawned[0]["args"][3]
    if tmp_dir:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_extract_lab_poll_always_cleans_tmp_dir():
    """无论 done / timeout / cancelled，_poll 收尾都必须删掉父进程建的临时目录。

    子进程被 terminate()（Windows 上是无条件 TerminateProcess）时执行不到任何
    Python 收尾，装着云端 OCR 文字产物的目录只能由必然活着的父进程回收。
    """
    import os
    import queue as _q
    import tempfile as _tf
    import threading as _th
    from gui.widgets import ExtractLabDialog

    def _fresh_dlg():
        dlg = ExtractLabDialog()
        dlg._busy = True
        dlg._run_id = 1
        for name in ("_btn_run", "_btn_pick", "_progress", "_live_row",
                     "_md_view", "_src_view"):
            setattr(dlg, name, _Stub(disabled=False, visible=True, value="",
                                     content=""))
        dlg._chips = _Stub(controls=[])
        return dlg

    # done：正常拿到结果
    d1 = _tf.mkdtemp(prefix="extract_preview_")
    open(os.path.join(d1, "leak.md"), "w", encoding="utf-8").write("ocr 敏感内容")
    q = _q.Queue()
    q.put({"ok": True, "info": {"md": "# x", "reason": "", "route": "local",
                                "cached": False, "elapsed": 0.1, "chars": 3}})
    _fresh_dlg()._poll(q, 30, 1, _FakeProc(alive=False), _th.Event(), d1)
    assert not os.path.exists(d1), "done 收尾必须清掉临时目录"

    # cancelled：用户关闭对话框，子进程被强杀
    d2 = _tf.mkdtemp(prefix="extract_preview_")
    ev = _th.Event()
    ev.set()
    _fresh_dlg()._poll(_q.Queue(), 30, 1, _FakeProc(alive=True), ev, d2)
    assert not os.path.exists(d2), "取消（子进程被强杀）同样必须清掉临时目录"

    # timeout：预算耗尽被强杀（快进时钟，不真等 45s）
    import gui.widgets as W
    d3 = _tf.mkdtemp(prefix="extract_preview_")
    orig_mono = W.time.monotonic
    clock = [orig_mono()]

    def _fast():
        clock[0] += 100_000.0
        return clock[0]

    try:
        W.time.monotonic = _fast
        _fresh_dlg()._poll(_q.Queue(), 30, 1, _FakeProc(alive=True),
                           _th.Event(), d3)
    finally:
        W.time.monotonic = orig_mono
    assert not os.path.exists(d3), "超时（子进程被强杀）同样必须清掉临时目录"

    # 旧轮次（run_id 失配）提前 return 的路径也不能漏
    d4 = _tf.mkdtemp(prefix="extract_preview_")
    stale = _fresh_dlg()
    stale._run_id = 2
    ev2 = _th.Event()
    ev2.set()
    stale._poll(_q.Queue(), 30, 1, _FakeProc(alive=True), ev2, d4)
    assert not os.path.exists(d4), "旧轮次提前 return 也必须清掉自己那份临时目录"


# ---------- SearchCard 双链关系内联展开（不搭真实 flet Page） ----------


def _collect_texts(controls):
    """递归收集控件树里所有 ft.Text 的 value（用于断言关系区是否渲染出文字，
    不依赖真实 Page，_render_results() 本身就不碰 page）。"""
    from gui.widgets import ft
    out = []
    stack = list(controls or [])
    while stack:
        c = stack.pop()
        if isinstance(c, ft.Text):
            out.append(c.value or "")
            continue
        content = getattr(c, "content", None)
        if content is not None:
            stack.append(content)
        inner = getattr(c, "controls", None)
        if inner:
            stack.extend(inner)
    return out


def _sample_result_text(rel="测试库/docs/foo.md", heading="小节标题", conf=0.87):
    """构造一条最小的模拟检索结果文本，格式对齐 _parse_src 的 docstring：
    [来源] <rel> (## <heading>) [置信度 <conf>]，随后是正文，"---" 结尾分隔。"""
    return ("[来源] %s (## %s) [置信度 %s]\n"
            "这是命中片段正文第一行。\n"
            "---\n" % (rel, heading, conf))


def test_search_card_relations_toggle_queries_once_and_caches():
    """展开关联笔记区首次应查询一次；收起不重查；再展开应命中缓存不重查。"""
    from gui.widgets import SearchCard
    from gui.theme import DARK

    calls = []

    def fake_on_relations(rel):
        calls.append(rel)
        return {"resolved": True, "file": "docs/foo.md",
                "outlinks": ["docs/bar.md"], "inlinks": ["docs/baz.md"]}

    card = SearchCard(on_search=lambda q: None, on_relations=fake_on_relations)
    card.show_results(_sample_result_text(), DARK)

    card._toggle_relations(0, "测试库/docs/foo.md")
    assert calls == ["测试库/docs/foo.md"], "首次展开应查询一次，参数为传入的 rel"
    assert 0 in card._render_state["relations_shown"]

    card._toggle_relations(0, "测试库/docs/foo.md")
    assert calls == ["测试库/docs/foo.md"], "收起不应重新查询"
    assert 0 not in card._render_state["relations_shown"]

    card._toggle_relations(0, "测试库/docs/foo.md")
    assert calls == ["测试库/docs/foo.md"], "再次展开应命中缓存，不重复查询"
    assert 0 in card._render_state["relations_shown"]

    # 关系区展开不应影响正文展开（两个开关互相独立）
    assert card._render_state["expanded"] == set()

    # 展开态下结果卡片应渲染出出链/入链文字
    joined = "\n".join(_collect_texts(card.results.controls))
    assert "docs/bar.md" in joined, "应渲染出链内容"
    assert "docs/baz.md" in joined, "应渲染入链内容"


def test_search_card_relations_unresolved_shows_fallback_text():
    """笔记未在 meta 中命中（resolved=False）时展示友好提示，不抛异常。"""
    from gui.widgets import SearchCard
    from gui.theme import DARK

    def fake_on_relations(rel):
        return {"resolved": False, "file": None, "outlinks": [], "inlinks": []}

    card = SearchCard(on_search=lambda q: None, on_relations=fake_on_relations)
    card.show_results(_sample_result_text(), DARK)
    card._toggle_relations(0, "测试库/docs/foo.md")

    joined = "\n".join(_collect_texts(card.results.controls))
    assert "未找到" in joined, "未命中应展示友好提示而不是空白/报错"


def test_search_card_without_on_relations_toggle_is_inert_but_safe():
    """未接 on_relations 时（默认 None）：按钮不挂 on_click，直接调用
    _toggle_relations 也不应抛异常（缓存位置留空，展示兜底文案）。"""
    from gui.widgets import SearchCard
    from gui.theme import DARK

    card = SearchCard(on_search=lambda q: None)
    card.show_results(_sample_result_text(), DARK)
    card._toggle_relations(0, "测试库/docs/foo.md")  # 不应抛异常
    assert 0 in card._render_state["relations_shown"]
    assert card._render_state["relations_cache"] == {}, "无回调不应产生缓存条目"


# ---------- 问题32：停滞宽限（GUI 判定侧 / C2） ----------

def test_heartbeat_grace_running_then_expired_then_dead():
    """C2 核心：宽限内不误报 stalled / 过期恢复告警 / 心跳冻结仍 DEAD 优先。"""
    now = time.time()
    p = make_progress(running=True, phase="scanning", updated_at=now,
                      last_advance_at=now - 60, stall_grace_until=now + 120)
    assert heartbeat_state(p) == HB_RUNNING, "宽限内的合法静默不得判 stalled"
    p2 = make_progress(running=True, phase="scanning", updated_at=now,
                       last_advance_at=now - 60, stall_grace_until=now - 5)
    assert heartbeat_state(p2) == HB_STALLED, "宽限过期应恢复停滞判定"
    p3 = make_progress(running=True, phase="scanning", updated_at=now - 30,
                       last_advance_at=now - 60, stall_grace_until=now + 300)
    assert heartbeat_state(p3) == HB_DEAD, "DEAD 先于一切豁免，宽限绝不掩盖真死"


def test_heartbeat_grace_invalid_types_fail_closed():
    """非法 stall_grace_until 一律视为无宽限（fail-closed），照常判 stalled。"""
    now = time.time()
    for junk in ("abc", None, [], {}, True):
        p = make_progress(running=True, phase="scanning", updated_at=now,
                          last_advance_at=now - 60, stall_grace_until=junk)
        assert heartbeat_state(p) == HB_STALLED, repr(junk)


def test_heartbeat_note_grace_vs_converting():
    """heartbeat_note 纯函数：converting 文案保留；宽限内给合法长静默提示
    （含已安静秒数）；无宽限/非运行/心跳冻结（DEAD-first）一律 None。"""
    now = time.time()
    conv = make_progress(running=True, phase="converting", updated_at=now,
                         last_advance_at=now - 99)
    assert heartbeat_note(conv) == "文档转换中（大文件耗时属预期）"
    grace = make_progress(running=True, phase="scanning", updated_at=now,
                          last_advance_at=now - 40, stall_grace_until=now + 120)
    note = heartbeat_note(grace)
    assert note and "已安静 40s" in note and "宽限" in note, note
    plain = make_progress(running=True, phase="scanning", updated_at=now,
                          last_advance_at=now)
    assert heartbeat_note(plain) is None
    expired = make_progress(running=True, phase="scanning", updated_at=now,
                            last_advance_at=now - 99, stall_grace_until=now - 1)
    assert heartbeat_note(expired) is None
    deadish = make_progress(running=True, phase="scanning", updated_at=now - 30,
                            last_advance_at=now - 40,
                            stall_grace_until=now + 300)
    assert heartbeat_note(deadish) is None, "红 DEAD 胶囊绝不能配「宽限内」文案"
    assert heartbeat_note(make_progress()) is None


def test_dual_watchdog_consistency_on_grace_samples():
    """双看门狗一致性：同一组宽限样本下，gui.heartbeat_state 与
    index.progress_text 的结论必须一致（防两份镜像表达式漂移）。"""
    import index as _index
    now = time.time()
    samples = [
        # (标签, progress, 期望四态)
        ("grace-active",
         dict(running=True, phase="scanning", pid=9, updated_at=now,
              last_advance_at=now - 60, stall_grace_until=now + 120),
         HB_RUNNING),
        ("converting-no-field",
         dict(running=True, phase="converting", pid=9, updated_at=now,
              last_advance_at=now - 999),
         HB_RUNNING),
        ("grace-expired",
         dict(running=True, phase="scanning", pid=9, updated_at=now,
              last_advance_at=now - 60, stall_grace_until=now - 5),
         HB_STALLED),
        ("plain-stalled",
         dict(running=True, phase="scanning", pid=9, updated_at=now,
              last_advance_at=now - 60),
         HB_STALLED),
        ("dead-beats-grace",
         dict(running=True, phase="scanning", pid=9, updated_at=now - 30,
              last_advance_at=now - 60, stall_grace_until=now + 300),
         HB_DEAD),
        ("fresh-running",
         dict(running=True, phase="scanning", pid=9, updated_at=now,
              last_advance_at=now),
         HB_RUNNING),
    ]
    for label, p, expect in samples:
        got = heartbeat_state(dict(p))
        assert got == expect, f"{label}: gui={got} 期望 {expect}"
        txt = _index.progress_text(dict(p))
        if expect == HB_DEAD:
            assert "疑似卡死" in txt, label
        elif expect == HB_STALLED:
            assert "进度停滞" in txt, label
        elif now - p["last_advance_at"] > STALL_TIMEOUT:
            # 运行中且进度久未推进：两侧都必须按「合法静默」处理而非告警
            assert "⚠" not in txt and "进度停滞" not in txt, label
            assert "文档转换中" in txt or "宽限剩余" in txt, label



# ---- 问题39：逐文件生效明细（store 层纯逻辑） ----

def test_file_index_rows_for_lists_terminals_and_retry():
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / 'index_meta_A.json'
        meta = {
            'ok.md': {'chunks': 3, 'size': 1, 'mtime': 1},
            'bad.pdf': {'hash': 'x', 'chunks': 0, 'size': 1, 'mtime': 1,
                        'tbd': False, 'xfail': True, 'reason': 'extract-failed',
                        'xsrc': 'backend:key/nokey-OLD'},
            'scan.pdf': {'hash': 'y', 'chunks': 0, 'size': 1, 'mtime': 1,
                         'tbd': False, 'xfail': True, 'reason': 'scanned',
                         'xsrc': 'backend:key/nokey'},
        }
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(meta), encoding='utf-8')
        cfg = _fake_cfg('A', td)
        with patch('gui.store.meta_path', return_value=mp),              patch('gui.store.current_backend_sig',
                   return_value='backend:key/nokey'):
            out = file_index_rows_for(cfg)
        assert out['total'] == 1, out['total']
        rows = {r[0]: r for r in out['rows']}
        assert set(rows) == {'bad.pdf', 'scan.pdf'}, sorted(rows)
        # xsrc 与当前签名不符 → 下轮真会重试；相符 → 不重试（判定与 index 一致）
        assert rows['bad.pdf'][1] == 'extract-failed' and rows['bad.pdf'][2] is True
        assert rows['scan.pdf'][1] == 'scanned' and rows['scan.pdf'][2] is False


def test_file_index_rows_for_missing_meta():
    with tempfile.TemporaryDirectory() as td:
        cfg = _fake_cfg('A', td)
        with patch('gui.store.meta_path',
                   return_value=Path(td) / 'nope.json'):
            out = file_index_rows_for(cfg)
        assert out == {'total': 0, 'rows': []}


def test_wemm_status_for_rows_and_missing():
    with tempfile.TemporaryDirectory() as td:
        cfg = _fake_cfg('A', td)
        # meta 不存在 → exists=False（还没建页索引）
        with patch('gui.store.wemm_meta_file',
                   return_value=Path(td) / 'nope.json'):
            out = wemm_status_for(cfg)
        assert out['exists'] is False and out['rows'] == []
        mp = Path(td) / 'wemm_meta_A.json'
        meta = {
            '_version': 1,
            'a.pdf': {'hash': 'x', 'pages': 9, 'size': 1, 'mtime': 1,
                      'tbd': False, 'xsrc': 'wemm:m:512:60'},
            'broken.pdf': {'hash': 'y', 'chunks': 0, 'size': 1, 'mtime': 1,
                           'tbd': False, 'xfail': True, 'reason': 'extract-failed'},
        }
        mp.write_text(json.dumps(meta), encoding='utf-8')
        with patch('gui.store.wemm_meta_file', return_value=mp):
            out = wemm_status_for(cfg)
        assert out['exists'] is True and out['total_pages'] == 9
        rows = {r[0]: r for r in out['rows']}
        assert rows['a.pdf'][1] == 9 and rows['a.pdf'][2] is False
        assert rows['broken.pdf'][1] is None and rows['broken.pdf'][2] is True
        assert rows['broken.pdf'][3] == 'extract-failed'


def test_file_status_dialog_constructs():
    """对话框无需窗口即可构建（捕获构造期 API/语法错误）。"""
    from gui.widgets import FileStatusDialog
    d = FileStatusDialog(on_open_file=lambda p: None)
    assert d._dlg is not None and d._lib_dd is not None
    row = d._row(icon=None, icon_color='#fff', title='a.pdf', sub='ok')
    assert row.content is not None


def test_wemm_service_probe_branches():
    with patch('wemm_retriever.health', return_value={'loaded': True,
                                                      'model': 'm', 'device': 'cuda'}):
        alive, detail = wemm_service_probe('http://127.0.0.1:9101')
    assert alive and '已进显存' in detail and 'cuda' in detail
    with patch('wemm_retriever.health', return_value={'loaded': False}):
        alive, detail = wemm_service_probe('http://127.0.0.1:9101')
    assert alive and '待首次请求' in detail
    with patch('wemm_retriever.health', side_effect=OSError('refused')):
        alive, detail = wemm_service_probe('http://127.0.0.1:9101')
    assert not alive and '未启动' in detail


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
