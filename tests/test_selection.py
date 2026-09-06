"""test_selection.py — 库内路径级勾选建模（问题44）纯逻辑单元测试。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_selection.py
覆盖：norm_sel_path / resolve_selection（最近显式赢）/ set_selection /
format_selection_bulk（格式批量语义）/ bulk_for_extensions（extensions 变更收口）/
collect_md_files（扫描漏斗勾选过滤）/ selection_gate（MCP 硬门禁）/ bridge。
不加载模型、不碰真实 Chroma、不写真实 libraries.json / config.json。
"""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui"))

import library  # noqa: E402
import selection_gate  # noqa: E402
from index import collect_md_files  # noqa: E402

PASS = 0
FAIL = 0


def _check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s" % name)
    else:
        FAIL += 1
        print("FAIL %s %s" % (name, detail))


def make_isolated(tmp):
    """注册表/数据目录隔离到临时目录 + 注入全局默认值（对齐 library_registry_test）。"""
    library.LIBRARIES_FILE = Path(tmp) / "libraries.json"
    library.DATA_DIR = Path(tmp)
    library.CFG = {
        "vault": "", "collection_name": "obsidian_kb",
        "exclude_dirs": [], "exclude_files": [], "exclude_patterns": [],
        "chunk_char_limit": 600, "short_doc_char_limit": 200,
    }


def make_vault(tmp):
    """构造测试库：课件/{青苹果菜单.pdf,锅包肉配方.pdf,小明账单.pdf}、私人/账单.pdf、笔记.md"""
    v = Path(tmp) / "vault"
    (v / "课件").mkdir(parents=True)
    for n in ("青苹果菜单.pdf", "锅包肉配方.pdf", "小明账单.pdf"):
        (v / "课件" / n).write_text("x", encoding="utf-8")
    (v / "私人").mkdir()
    (v / "私人" / "账单.pdf").write_text("x", encoding="utf-8")
    (v / "笔记.md").write_text("x", encoding="utf-8")
    return v


def register(tmp, vault, name="t"):
    library.save_registry([library._blank_entry(name, str(vault))])
    return name


def rels(vault, exts, selection=None, default="follow"):
    return sorted(p.name for p in collect_md_files(
        str(vault), [], [], (), exts, selection=selection, selection_default=default))


# ---------------------------------------------------------------------------
# A. norm_sel_path
# ---------------------------------------------------------------------------
def test_norm_sel_path():
    _check("norm: 反斜杠与尾斜杠归一",
           library.norm_sel_path("课件\\青苹果.pdf/") == "课件/青苹果.pdf")
    for bad in ("", "  ", "/abs/path", "C:\\x", "..\\x", "a/../../b", "a//b", "a/./b"):
        try:
            library.norm_sel_path(bad)
            _check("norm: 拒绝 %r" % bad, False, "未抛异常")
        except ValueError:
            _check("norm: 拒绝 %r" % bad, True)


# ---------------------------------------------------------------------------
# B. resolve_selection：最近显式赢
# ---------------------------------------------------------------------------
def test_resolve_nearest_wins():
    sin = ["课件/青苹果菜单.pdf", "20-Projects"]
    sout = ["课件", "私人/账单.pdf"]
    _check("resolve: 文件显式勾选 > 父文件夹排除（用户拍板①）",
           library.resolve_selection(sin, sout, "课件/青苹果菜单.pdf") == "in")
    _check("resolve: 文件夹排除命中同层",
           library.resolve_selection(sin, sout, "课件/锅包肉配方.pdf") == "out")
    _check("resolve: 文件夹级勾选 > 更远的排除",
           library.resolve_selection(sin, sout, "20-Projects/x/深度.md") == "in")
    _check("resolve: 文件显式排除",
           library.resolve_selection(sin, sout, "私人/账单.pdf") == "out")
    _check("resolve: 中性返回 None",
           library.resolve_selection(sin, sout, "笔记.md") is None)
    _check("resolve: 空表全中性",
           library.resolve_selection([], [], "任何/文件.md") is None)
    _check("resolve: 同层冲突 out 优先（宁可少索引）",
           library.resolve_selection(["a"], ["a"], "a") == "out")


# ---------------------------------------------------------------------------
# C. set_selection
# ---------------------------------------------------------------------------
def test_set_selection():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        v = make_vault(td)
        register(td, v)
        library.set_selection("t", [{"path": "课件/青苹果菜单.pdf", "action": "in"},
                                    {"path": "私人", "action": "out"}])
        e = library.load_registry()[0]
        _check("set: in/out 各就各位",
               e["selection_in"] == ["课件/青苹果菜单.pdf"] and e["selection_out"] == ["私人"])
        # neutral：撤销显式选择（把 私人 从 out 摘掉）
        library.set_selection("t", [{"path": "私人", "action": "neutral"}])
        e = library.load_registry()[0]
        _check("set: neutral 摘除显式记录", e["selection_out"] == [])
        # 同一路径不能同时在两表（in 后再 out 是迁移不是并存）
        library.set_selection("t", [{"path": "课件/青苹果菜单.pdf", "action": "in"},
                                    {"path": "课件/青苹果菜单.pdf", "action": "out"}])
        e = library.load_registry()[0]
        _check("set: 后写覆盖先写（不并存）",
               e["selection_in"] == [] and e["selection_out"] == ["课件/青苹果菜单.pdf"])
        for changes, why in (
                ([{"path": "../逃逸.pdf", "action": "in"}], ".. 逃逸"),
                ([{"path": "不存在的文件.pdf", "action": "in"}], None),
                ([{"path": "课件/x.pdf", "action": "炸"}], "非法 action")):
            if why is None:
                continue
            try:
                library.set_selection("t", changes)
                _check("set: 拒绝（%s）" % why, False, "未抛异常")
            except ValueError:
                _check("set: 拒绝（%s）" % why, True)


# ---------------------------------------------------------------------------
# D. format_selection_bulk：格式批量语义（用户拍板③）
# ---------------------------------------------------------------------------
def test_format_bulk():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        v = make_vault(td)
        register(td, v)
        # 子目录里的文件同样是"文件级"：显式勾选的两个 pdf 跟着全局取消（青苹果菜单也取消）
        library.set_selection("t", [{"path": "课件/青苹果菜单.pdf", "action": "in"},
                                    {"path": "课件/锅包肉配方.pdf", "action": "in"},
                                    {"path": "课件", "action": "in"}])
        n = library.format_selection_bulk("t", "pdf", include=False)
        e = library.load_registry()[0]
        _check("bulk off: 2 个文件级条目迁移", n == 2, str(n))
        _check("bulk off: 文件条目 in→out",
               e["selection_in"] == ["课件"] and
               set(e["selection_out"]) == {"课件/青苹果菜单.pdf", "课件/锅包肉配方.pdf"})
        _check("bulk off: 文件夹级条目不被触碰", "课件" in e["selection_in"])
        n2 = library.format_selection_bulk("t", "pdf", include=True)
        e = library.load_registry()[0]
        _check("bulk on: out 中该格式文件条目移除", n2 == 2 and e["selection_out"] == [])
        _check("bulk on: in 表原样（显式勾选不凭空复活）", e["selection_in"] == ["课件"])
        _check("bulk 无匹配返回 0", library.format_selection_bulk("t", "docx", True) == 0)


# ---------------------------------------------------------------------------
# E. extensions 变更 → 批量收口（GUI/CLI/MCP 单一漏斗 set_config）
# ---------------------------------------------------------------------------
def test_extensions_change_triggers_bulk():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        v = make_vault(td)
        register(td, v)
        library.set_selection("t", [{"path": "课件/青苹果菜单.pdf", "action": "in"}])
        library.set_config("t", "extensions", "md")  # 关 pdf
        e = library.load_registry()[0]
        _check("ext 关 pdf: 显式勾选的 pdf 跟着取消",
               e["extensions"] == ["md"] and
               e["selection_out"] == ["课件/青苹果菜单.pdf"] and e["selection_in"] == [])
        library.set_config("t", "extensions", "md,pdf")  # 开 pdf
        e = library.load_registry()[0]
        _check("ext 开 pdf: out 中该格式条目移除",
               e["selection_out"] == [] and e["extensions"] == ["md", "pdf"])


# ---------------------------------------------------------------------------
# F. effective_config：读侧规范化 + 中性默认
# ---------------------------------------------------------------------------
def test_effective_config_selection():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        v = make_vault(td)
        entry = library._blank_entry("t", str(v))
        entry["selection_in"] = ["课件\\青苹果菜单.pdf", "../坏路径", 42]
        entry["selection_out"] = ["私人"]
        library.save_registry([entry])
        cfg = library.effective_config(library.load_registry()[0])
        _check("eff: 路径规范化 + 非法元素静默丢弃",
               cfg["selection_in"] == ["课件/青苹果菜单.pdf"], str(cfg["selection_in"]))
        _check("eff: out 表原样保留", cfg["selection_out"] == ["私人"])
        _check("eff: selection_default 兜底 follow",
               cfg.get("selection_default", "follow") == "follow")
        library.CFG["selection_new_files"] = "exclude"
        cfg = library.effective_config(library.load_registry()[0])
        _check("eff: 读到 config 的中性默认", cfg["selection_default"] == "exclude")
        library.CFG["selection_new_files"] = "follow"


# ---------------------------------------------------------------------------
# G. collect_md_files：扫描漏斗勾选过滤
# ---------------------------------------------------------------------------
def test_collect_with_selection():
    with tempfile.TemporaryDirectory() as td:
        v = make_vault(td)
        sin = ["课件/青苹果菜单.pdf"]
        sout = ["课件/小明账单.pdf"]
        sel = (sin, sout)
        # 无 selection 时行为不变（扩展名白名单）
        _check("collect: selection=None 旧行为",
               rels(v, ["md"]) == ["笔记.md"])
        # 全格式：显式 out 一票排除、显式 in 无感
        _check("collect: 显式排除生效",
               "小明账单.pdf" not in rels(v, ["md", "pdf"], sel))
        # 显式 in 穿透白名单：extensions=md 时 pdf 仍入库（用户例子核心）
        _check("collect: 显式勾选穿透格式白名单",
               rels(v, ["md"], sel) == ["笔记.md", "青苹果菜单.pdf"])
        # 中性 follow：格式关掉就排除
        _check("collect: 中性 follow 跟随格式",
               rels(v, ["md"], sel) == ["笔记.md", "青苹果菜单.pdf"])
        # 中性默认 include：可穿透白名单（仅受支持格式；私人/账单.pdf 也是中性）
        _check("collect: 默认 include 穿透白名单",
               set(rels(v, ["md"], sel, default="include")) ==
               {"笔记.md", "青苹果菜单.pdf", "锅包肉配方.pdf", "账单.pdf"})
        # 中性默认 exclude：一律排除
        _check("collect: 默认 exclude 一律排除",
               rels(v, ["md", "pdf"], sel, default="exclude") == ["青苹果菜单.pdf"])
        # 最近显式赢：排除文件夹 + 勾选其中文件
        sel2 = (["课件/青苹果菜单.pdf"], ["课件"])
        _check("collect: 文件夹排除 + 文件勾选（近端赢）",
               set(rels(v, ["md", "pdf"], sel2)) ==
               {"笔记.md", "青苹果菜单.pdf", "账单.pdf"})


# ---------------------------------------------------------------------------
# H. selection_gate：MCP 硬门禁
# ---------------------------------------------------------------------------
def test_selection_gate():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        selection_gate.PENDING_FILE = Path(td) / "pending.json"
        selection_gate.DATA_DIR = Path(td)
        v = make_vault(td)
        register(td, v)
        cfg = library.effective_config(library.load_registry()[0])

        # 变更校验：越界拒绝（norm_sel_path 抛的 ValueError 同样算拒绝——
        # server 工具层统一 except ValueError 兜底）
        for bad_changes in ([{"path": "../x.pdf", "action": "in"}],
                            [{"path": "课件/x.pdf", "action": "炸"}]):
            try:
                selection_gate.normalize_changes(cfg, bad_changes)
                _check("gate: 非法变更拒绝", False)
            except (selection_gate.GateError, ValueError):
                _check("gate: 非法变更拒绝", True)
        try:
            selection_gate.normalize_changes(cfg, [])
            _check("gate: 空变更拒绝", False)
        except selection_gate.GateError:
            _check("gate: 空变更拒绝", True)

        # propose：提案落盘、返回 diff + 6 位码、盘上只有哈希
        pid, code, diff = selection_gate.make_proposal(
            "t", cfg, [{"path": "课件/青苹果菜单.pdf", "action": "out"}])
        _check("gate: 提案号格式", pid.startswith("sel-"))
        _check("gate: 确认码 6 位数字", len(code) == 6 and code.isdigit())
        _check("gate: diff 含路径与状态迁移",
               "青苹果菜单.pdf" in diff and "→" in diff)
        pend = json.loads(selection_gate.PENDING_FILE.read_text(encoding="utf-8"))
        _check("gate: 盘上只存哈希不存明文码",
               pend["code_sha256"] != code and len(pend["code_sha256"]) == 64)

        # apply：错码拒绝（可重试）
        try:
            selection_gate.consume_proposal("t", pid, "000000" if code != "000000" else "111111")
            _check("gate: 错码拒绝", False)
        except selection_gate.GateError:
            _check("gate: 错码拒绝", True)
        # apply：库名不匹配
        try:
            selection_gate.consume_proposal("其他库", pid, code)
            _check("gate: 库名不匹配拒绝", False)
        except selection_gate.GateError:
            _check("gate: 库名不匹配拒绝", True)
        # apply：正确 → 一次性消费
        changes = selection_gate.consume_proposal("t", pid, code)
        _check("gate: 正确码放行并返回变更",
               changes == [{"path": "课件/青苹果菜单.pdf", "action": "out"}])
        # apply：重放拒绝
        try:
            selection_gate.consume_proposal("t", pid, code)
            _check("gate: 一次性消费（重放拒绝）", False)
        except selection_gate.GateError:
            _check("gate: 一次性消费（重放拒绝）", True)
        # apply：过期
        pid2, code2, _ = selection_gate.make_proposal(
            "t", cfg, [{"path": "课件/青苹果菜单.pdf", "action": "in"}])
        pend2 = json.loads(selection_gate.PENDING_FILE.read_text(encoding="utf-8"))
        pend2["created"] = time.time() - selection_gate.TTL_S - 5
        selection_gate._pending_save(pend2)
        try:
            selection_gate.consume_proposal("t", pid2, code2)
            _check("gate: 过期拒绝", False)
        except selection_gate.GateError:
            _check("gate: 过期拒绝", True)


# ---------------------------------------------------------------------------
# I. guiweb bridge：selection_tree / selection_update
# ---------------------------------------------------------------------------
def test_bridge_selection():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        v = make_vault(td)
        register(td, v)
        library.set_selection("t", [{"path": "课件/青苹果菜单.pdf", "action": "in"},
                                    {"path": "私人", "action": "out"}])
        from guiweb.bridge import Bridge
        b = Bridge()
        r = b.selection_tree("t", "")
        _check("bridge: 根目录列举",
               r["error"] is None and
               sorted(d["name"] for d in r["dirs"]) == ["私人", "课件"] and
               [f["name"] for f in r["files"]] == ["笔记.md"])
        qing = next(f for f in [] ) if False else None
        sub = b.selection_tree("t", "课件")
        apple = next(f for f in sub["files"] if f["name"] == "青苹果菜单.pdf")
        guo = next(f for f in sub["files"] if f["name"] == "锅包肉配方.pdf")
        _check("bridge: 显式勾选标记 explicit=in",
               apple["explicit"] == "in" and apple["state"] == "in")
        _check("bridge: 中性文件 auto 态（extensions 含 pdf）",
               guo["explicit"] is None and guo["state"] == "auto_in")
        _check("bridge: payload 带 extensions/default",
               r["extensions"] and r["default"] == "follow")
        # 整棵目录树（左栏一次拉取；跳过隐藏目录；root 哨兵打头）
        fp = [f["path"] for f in r["folders"]]
        _check("bridge: folders 目录树（root + 课件 + 私人）",
               fp[0] == "" and set(fp) == {"", "课件", "私人"} and
               all(f["depth"] == 1 for f in r["folders"] if f["path"]))
        _check("bridge: 越界拒绝",
               "error" in b.selection_tree("t", "../../etc") and
               b.selection_tree("t", "../../etc")["error"])
        # extensions 关 pdf 后，中性 pdf 变 auto_out
        library.set_config("t", "extensions", "md")
        sub2 = b.selection_tree("t", "课件")
        guo2 = next(f for f in sub2["files"] if f["name"] == "锅包肉配方.pdf")
        _check("bridge: 格式关闭后中性 pdf 转 auto_out", guo2["state"] == "auto_out")
        # selection_update 直改 + 校验
        up = b.selection_update("t", [{"path": "课件/锅包肉配方.pdf", "action": "out"}])
        _check("bridge: update 直改生效", up["ok"] and
               "课件/锅包肉配方.pdf" in up["selection_out"])
        up2 = b.selection_update("t", [{"path": "../../x", "action": "in"}])
        _check("bridge: update 越界拒绝", not up2["ok"] and up2["error"])
        _check("bridge: 不存在的库报错", b.selection_tree("没有的库", "")["error"])
        # exclude_* 硬排除优先于显式勾选，徽章显示与扫描漏斗一致
        library.set_selection("t", [{"path": "笔记.md", "action": "in"}])
        library.set_config("t", "exclude_files", "笔记.md")
        r3 = b.selection_tree("t", "")
        note = next(f for f in r3["files"] if f["name"] == "笔记.md")
        _check("bridge: exclude_files 命中显示排除（不可勾选穿透）",
               note["state"] == "out" and note["explicit"] is None and
               note["state_text"] == "已排除（排除名单）")
        # 中性文件夹 = 跟随子内容（无扩展名不能落格式判定，否则全显示"排除"）
        kechen = next(d for d in r["dirs"] if d["name"] == "课件")
        _check("bridge: 中性文件夹 auto_in（跟随子内容）",
               kechen["state"] == "auto_in" and kechen["explicit"] is None and
               kechen["state_text"] == "入库（跟随子内容）")


# ---------------------------------------------------------------------------
# J. kb_stale 联动：新排除的文件按"移除"计入，触发一轮收敛
# ---------------------------------------------------------------------------
def test_kb_stale_exclusion_convergence():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        v = make_vault(td)
        register(td, v)
        import index
        index.INDEX_META = Path(td) / "meta.json"
        meta = {"课件/锅包肉配方.pdf": {"hash": "x", "chunks": 3, "size": 1,
                                       "mtime": 1, "links": []},
                "_version": index.META_VERSION}
        index.INDEX_META.write_text(json.dumps(meta), encoding="utf-8")
        library.set_selection("t", [{"path": "课件/锅包肉配方.pdf", "action": "out"}])
        cfg = library.effective_config(library.load_registry()[0])
        with patch.object(index, "current_backend_sig", return_value="sig"):
            stale, stats = index.kb_stale(
                str(v), meta_file=index.INDEX_META, collection_name="c",
                exclude_dirs=[], exclude_files=[], exclude_patterns=[],
                extensions=["md", "pdf"], selection=(cfg["selection_in"], cfg["selection_out"]),
                selection_default=cfg["selection_default"])
        _check("kb_stale: 新排除文件计入 removed 触发收敛", stale and stats["removed"] == 1,
               str(stats))
        # 全部排除后的稳态：扫描集合为空 + meta 为空 → 收敛不误报（红线 2/4）
        library.set_selection("t", [{"path": "课件/锅包肉配方.pdf", "action": "out"},
                                    {"path": "课件/青苹果菜单.pdf", "action": "out"},
                                    {"path": "课件/小明账单.pdf", "action": "out"},
                                    {"path": "私人", "action": "out"},
                                    {"path": "笔记.md", "action": "out"}])
        cfg2 = library.effective_config(library.load_registry()[0])
        index.INDEX_META.write_text(json.dumps({"_version": index.META_VERSION}),
                                    encoding="utf-8")
        with patch.object(index, "current_backend_sig", return_value="sig"):
            stale2, _ = index.kb_stale(
                str(v), meta_file=index.INDEX_META, collection_name="c",
                exclude_dirs=[], exclude_files=[], exclude_patterns=[],
                extensions=["md", "pdf"],
                selection=(cfg2["selection_in"], cfg2["selection_out"]),
                selection_default=cfg2["selection_default"])
        _check("kb_stale: 全排除后稳态不再误报", not stale2)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global FAIL
            FAIL += 1
            import traceback
            print("FAIL %s 异常：%r" % (fn.__name__, e))
            traceback.print_exc()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
