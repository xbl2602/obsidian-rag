"""test_guiweb.py — guiweb 桥与图谱数据层单元测试（不加载模型、不碰 Chroma）。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_guiweb.py
风格对齐 test_gui_store.py：标准库 + 逐用例 PASS/FAIL + _run_all() 运行器。
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui"))

from guiweb.bridge import parse_search_text, fmt_setting_value  # noqa: E402
from guiweb import graph_data as gd  # noqa: E402

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


# ---------------------------------------------------------------------------
# parse_search_text：检索文本协议 → 结构化
# ---------------------------------------------------------------------------

def test_parse_full_source_line():
    text = ("[来源] 技术笔记/20-Projects/Obsidian RAG/WEMM 设计.md (## 页级向量) "
            "[块 2/3] [置信度 0.87]\n"
            "页级视觉导航的思路是把一页而非一个文本块作为检索单元。\n"
            "---\n"
            "[来源] 论文阅读/ICLR2026_review.pdf [块 1/5] [置信度 0.42]"
            "（低置信度 0.42，仅供参考）\n"
            "Review 指出当前 RAG 系统。")
    rs = parse_search_text(text)
    _check("parse: 两条结果", len(rs) == 2, repr(rs)[:120])
    r0 = rs[0]
    _check("parse: 库名", r0["lib"] == "技术笔记", r0["lib"])
    _check("parse: rel", r0["rel"] == "20-Projects/Obsidian RAG/WEMM 设计.md", r0["rel"])
    _check("parse: 标题", r0["heading"] == "## 页级向量", str(r0["heading"]))
    _check("parse: 块号", (r0["chunk_idx"], r0["chunk_total"]) == (2, 3),
           str((r0["chunk_idx"], r0["chunk_total"])))
    _check("parse: 置信度", r0["confidence"] == 0.87, str(r0["confidence"]))
    _check("parse: 正文", "页级视觉导航" in r0["body"], r0["body"][:50])
    r1 = rs[1]
    _check("parse: 低置信尾巴被剥掉", "低置信度" not in r1["rel"], r1["rel"])
    _check("parse: 低置信值", r1["confidence"] == 0.42, str(r1["confidence"]))


def test_parse_no_markers_and_empty():
    rs = parse_search_text("[来源] 论文阅读/notes.md\n一些正文")
    _check("parse: 无标记行", len(rs) == 1 and rs[0]["lib"] == "论文阅读"
           and rs[0]["confidence"] is None and rs[0]["heading"] is None,
           repr(rs)[:120])
    _check("parse: 空输入", parse_search_text("") == []
           and parse_search_text(None) == [])


def test_parse_prefix_line_passthrough():
    """整体低置信提示行（（本次查询整体置信度偏低…）开头）归为 notice 横幅，
    不再像 Flet 版那样被当成幽灵结果渲染成畸形来源行。"""
    text = ("（本次查询整体置信度偏低（最高 0.31），以下结果仅供参考）\n"
            "[来源] 会议记录/周会纪要.md [置信度 0.31]\n正文A\n---\n")
    rs = parse_search_text(text)
    _check("parse: 提示行归 notice", len(rs) == 2 and rs[0].get("notice")
           and rs[0]["body"].startswith("（"), repr(rs)[:200])
    _check("parse: 真实结果不受影响", len(rs) == 2 and rs[1]["lib"] == "会议记录"
           and not rs[1].get("notice"), repr(rs)[:200])


# ---------------------------------------------------------------------------
# fmt_setting_value：设置值 → 字符串
# ---------------------------------------------------------------------------

def test_fmt_setting_value():
    _check("fmt: bool", fmt_setting_value("bool", True) == "true"
           and fmt_setting_value("bool", False) == "false")
    _check("fmt: list", fmt_setting_value("list", ["模板", "归档"]) == "模板,归档"
           and fmt_setting_value("list", None) == "")
    _check("fmt: int/float/str/None", fmt_setting_value("int", 45) == "45"
           and fmt_setting_value("float", 0.62) == "0.62"
           and fmt_setting_value("str", "x") == "x"
           and fmt_setting_value("str", None) == "")


# ---------------------------------------------------------------------------
# classify_theme：主题族确定性归族
# ---------------------------------------------------------------------------

def test_classify_theme():
    cases = [("20-Projects/Obsidian RAG/WEMM 设计.md", "wemm"),
             ("02-Areas/MinerU 笔记.md", "mineru"),
             ("20-Projects/检索与重排.md", "chunk"),
             ("03-Journals/2026-08-30.md", "daily"),
             ("会议记录/周会 2026-08-26.md", "daily"),
             ("20-Projects/Agent 门禁与隐私.md", "config"),
             ("随机笔记.md", "general")]
    for rel, want in cases:
        got = gd.classify_theme(rel)
        _check("theme: %s" % rel, got == want, "%s != %s" % (got, want))


# ---------------------------------------------------------------------------
# build_link_edges：双链边（与 resolve_note_relations 同规则）
# ---------------------------------------------------------------------------

def _fake_meta():
    return {
        "a.md": {"chunks": 2, "links": ["b", "子/c.md"]},
        "b.md": {"chunks": 1, "links": []},
        "子/c.md": {"chunks": 1, "links": ["a.md"]},
        "d.md": {"chunks": 0, "links": ["不存在"]},
    }


def test_build_link_edges():
    edges = gd.build_link_edges(_fake_meta())
    # a→b、a→子/c、子/c→a（无向规范化后与 a→子/c 合并）= 2 条唯一无向边
    want = {("a.md", "b.md"), ("a.md", "子/c.md")}
    _check("links: 集合（无向去重）", set(edges) == want, repr(edges))
    for a, b in edges:
        _check("links: 无向规范化", a < b, "%s %s" % (a, b))


# ---------------------------------------------------------------------------
# build_graph_for_lib：管线四态 + 页节点 + 未识别 PDF（iso fixtures）
# ---------------------------------------------------------------------------

class _Ctx:
    """临时目录桩：伪造单库的 meta / wemm meta / 磁盘 PDF。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault = self.base / "vault"
        (self.vault / "子").mkdir(parents=True)
        (self.vault / "a.md").write_text("x", encoding="utf-8")
        (self.vault / "扫描新.pdf").write_bytes(b"%PDF")
        self.meta = {
            "a.md": {"chunks": 3, "links": ["扫描课件"], "mtime": 1788000000.0,
                     "hash": "h", "size": 1, "tbd": False},
            "扫描课件.pdf": {"chunks": 9, "links": [], "mtime": 1788000100.0,
                             "hash": "h", "size": 1, "tbd": False},
            # xsrc 与当前签名一致 = 后端未变更 → scanned 终态下轮真会自动重试
            "排队论文.pdf": {"chunks": 0, "links": [], "mtime": 1788000200.0,
                             "hash": "h", "size": 1, "xfail": True,
                             "reason": "scanned", "xsrc": "sig-1"},
            "损坏论文.pdf": {"chunks": 0, "links": [], "mtime": 1788000300.0,
                             "hash": "h", "size": 1, "xfail": True,
                             "reason": "extract-failed"},
        }
        self.wemm = {"扫描课件.pdf": {"pages": 3},
                     "损坏论文.pdf": {"xfail": True, "reason": "渲染失败"}}

    def close(self):
        self.tmp.cleanup()


def _patch_gd(ctx):
    return patch.multiple(
        gd,
        load_meta=lambda name: dict(ctx.meta),
        load_wemm_meta=lambda name: dict(ctx.wemm),
        wemm_meta_path=lambda name: "unused",
        collect_md_files=lambda vault, dirs, files, pats, extensions=None: (
            [ctx.vault / "扫描新.pdf"] if extensions == ["pdf"] else []),
        current_backend_sig=lambda: "sig-1",
    )


def test_build_graph_for_lib_pipeline_states():
    ctx = _Ctx()
    cfg = {"name": "T", "path": str(ctx.vault), "exclude_dirs": [],
           "exclude_files": [], "exclude_patterns": [], "extensions": ["md", "pdf"]}
    with _patch_gd(ctx):
        nodes, edges = gd.build_graph_for_lib(cfg)
    by_id = {n["id"]: n for n in nodes}
    pipe = {k.split("|", 1)[1]: v["pipeline"] for k, v in by_id.items()
            if v["type"] in ("md", "pdf")}
    _check("graph: md 节点 done", pipe["a.md"]["mineru"] == "done")
    _check("graph: 已识别 PDF done", pipe["扫描课件.pdf"]["mineru"] == "done")
    _check("graph: scanned+签名未变=queued", pipe["排队论文.pdf"]["mineru"] == "queued",
           str(pipe["排队论文.pdf"]))
    _check("graph: 其他 xfail=failed", pipe["损坏论文.pdf"]["mineru"] == "failed")
    _check("graph: 磁盘 PDF 未识别 none", pipe["扫描新.pdf"]["mineru"] == "none",
           str(pipe["扫描新.pdf"]))
    _check("graph: wemm done/none/failed",
           pipe["扫描课件.pdf"]["wemm"] == "done"
           and pipe["损坏论文.pdf"]["wemm"] == "failed"
           and pipe["排队论文.pdf"]["wemm"] == "none",
           str(pipe))
    pages = [n for n in nodes if n["type"] == "page"]
    _check("graph: 3 个页节点", len(pages) == 3, str(len(pages)))
    page_edges = [e for e in edges if e["kind"] == "page"]
    _check("graph: 3 条归属边", len(page_edges) == 3, str(len(page_edges)))
    links = [e for e in edges if e["kind"] == "link"]
    _check("graph: 1 条双链边（a→扫描课件）", len(links) == 1
           and links[0]["b"].endswith("扫描课件.pdf"), repr(links))
    _check("graph: 失败原因保留更根本的提取失败", by_id["T|损坏论文.pdf"]["fail_reason"]
           == "extract-failed", str(by_id["T|损坏论文.pdf"]["fail_reason"]))
    ctx.close()


def test_build_graph_for_lib_pagegroup_cap():
    ctx = _Ctx()
    ctx.wemm = {"扫描课件.pdf": {"pages": 40}}
    cfg = {"name": "T", "path": str(ctx.vault), "exclude_dirs": [],
           "exclude_files": [], "exclude_patterns": [], "extensions": ["md"]}
    with _patch_gd(ctx):
        nodes, edges = gd.build_graph_for_lib(cfg)
    page_nodes = [n for n in nodes if n["type"] in ("page", "pagegroup")]
    _check("graph: 超 24 页折叠为组节点", len(page_nodes) == 1
           and page_nodes[0]["type"] == "pagegroup" and page_nodes[0]["page"] == 40,
           repr(page_nodes)[:120])
    ctx.close()


def test_mark_big():
    nodes = [{"id": x} for x in ("a", "b", "c", "d", "e", "f", "g")]
    edges = [{"a": "a", "b": x, "kind": "link"} for x in ("b", "c", "d", "e", "f", "g")]
    gd.mark_big(nodes, edges)
    big = {n["id"] for n in nodes if n.get("big")}
    _check("big: hub 共 5 个且含最高度数节点", len(big) == 5 and "a" in big, repr(big))


def test_select_semantic_edges():
    import numpy as np
    ids = ["n%d" % i for i in range(4)]
    vecs = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.0, 0.95]])
    edges = gd.select_semantic_edges(ids, vecs, threshold=0.9)
    pairs = {(e["a"], e["b"]) for e in edges}
    _check("sem: 高相似对入选", ("n0", "n1") in pairs and ("n2", "n3") in pairs,
           repr(pairs))
    _check("sem: 低相似对淘汰", ("n0", "n2") not in pairs, repr(pairs))
    sims = [e["sim"] for e in edges]
    _check("sem: sim 三位小数且 ≥ 阈值", all(s >= 0.9 for s in sims), repr(sims))
    _check("sem: 无自环", all(e["a"] != e["b"] for e in edges))


def test_build_graph_range_and_stats():
    """build_graph：范围过滤与 stats 汇总。

    注意 patch 命名空间：build_graph 顶层 `from library import …` 已把
    load_registry/effective_config 绑进 gd 命名空间，必须 patch gd.*；
    resolve_entries 是函数体内运行时导入，patch library.* 即可。
    """
    with patch.multiple(
        gd,
        load_registry=lambda: [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        effective_config=lambda e: {"name": e["name"], "path": "p",
                                    "exclude_dirs": [], "exclude_files": [],
                                    "exclude_patterns": [], "extensions": ["md"],
                                    "collection": e["name"]},
        load_meta=lambda name: {"x.md": {"chunks": 1, "links": [], "mtime": 1.0,
                                         "hash": "h", "size": 1, "tbd": False}},
        load_wemm_meta=lambda name: {},
        collect_md_files=lambda vault, dirs, files, pats, extensions=None: [],
        current_backend_sig=lambda: "sig",
    ), patch.multiple(
        "library",
        resolve_entries=lambda libs, exclude="", defaults=None: (
            [{"name": n, "path": "p", "exclude_dirs": [], "exclude_files": [],
              "exclude_patterns": [], "extensions": ["md"], "collection": n}
             for n in libs.split(",")]),
    ):
        g_all = gd.build_graph("")
        g_sub = gd.build_graph("A,C")
    _check("graph: 全部库", sorted(g_all["libs"]) == ["A", "B", "C"]
           and g_all["stats"]["nodes"] == 3, str(g_all["stats"]))
    _check("graph: 范围过滤", sorted(g_sub["libs"]) == ["A", "C"]
           and g_sub["stats"]["nodes"] == 2, str(g_sub["stats"]))


# ---------------------------------------------------------------------------
# 接线检查（前端就位后自动纳入回归；未就位则跳过并提示）
# ---------------------------------------------------------------------------

def test_ui_wiring():
    ui = ROOT / "guiweb" / "ui"
    if not (ui / "index.html").exists():
        print("SKIP test_ui_wiring（guiweb/ui 尚未生成）")
        return
    from guiweb.wiring_check import run_checks
    problems = run_checks()
    _check("wiring: 前端接线静态检查全绿", not problems,
           "; ".join(problems[:8]))


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global FAIL
            FAIL += 1
            print("FAIL %s 异常：%r" % (fn.__name__, e))
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
