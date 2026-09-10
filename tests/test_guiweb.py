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


def test_parse_confidence_tier_suffix():
    """问题43（2026-09-06）：retriever 来源行置信度标记升级为 [置信度 x.xx·分档词]，
    解析必须取到数字本身且剥掉整个标记（含分档词），旧格式（无后缀）继续兼容。"""
    text = ("[来源] 技术笔记/foo.md (## 小节) [块 1/2] [置信度 0.72·高相关]\n正文A\n---\n"
            "[来源] 技术笔记/bar.md [置信度 0.42·弱相关]（低置信度 0.42，仅供参考）\n正文B")
    rs = parse_search_text(text)
    _check("tier: 两条结果", len(rs) == 2, repr(rs)[:120])
    _check("tier: 高相关数值", rs[0]["confidence"] == 0.72, str(rs[0]["confidence"]))
    _check("tier: 分档词不混进 rel", rs[0]["rel"] == "foo.md", rs[0]["rel"])
    _check("tier: 弱相关数值", rs[1]["confidence"] == 0.42, str(rs[1]["confidence"]))
    _check("tier: 旧格式兼容",
           parse_search_text("[来源] a/b.md [置信度 0.87]\n正文")[0]["confidence"] == 0.87)


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
        collect_md_files=lambda vault, dirs, files, pats, extensions=None,
                            **_: (
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


def test_settings_choices_guard():
    """回归（2026-09-06 真机实测）：fieldRow 用 `if (f.choices)` 判分支，
    而后端对无选项字段返回空数组 []——JS 里空数组是 truthy，导致几乎所有
    设置字段（含 bool 开关）被渲染成零选项的空下拉框，设置页整体不可用。
    守卫必须是长度判断。"""
    ui = ROOT / "guiweb" / "ui"
    js = (ui / "app.js").read_text(encoding="utf-8") if (ui / "app.js").exists() else ""
    if not js:
        print("SKIP test_settings_choices_guard（guiweb/ui 尚未生成）")
        return
    _check("settings: fieldRow choices 守卫用长度判断（空数组不进下拉分支）",
           "f.choices && f.choices.length" in js)
    _check("settings: 不存在裸 if (f.choices) 旧写法",
           "if (f.choices) {" not in js)
    # mock 与真桥同构：get_settings 的 choices 一律是数组（可能为空），
    # 前端必须同时兼容两种空形态
    sys.path.insert(0, str(ROOT))
    from guiweb.bridge import Bridge
    data = Bridge().get_settings()
    bad = [f["key"] for g in data["groups"] for f in g["fields"]
           if not isinstance(f["choices"], list)]
    _check("settings: 真桥 get_settings 的 choices 均为数组", not bad, str(bad))


def test_snapshot_sys_fields_contract():
    """快照占用字段契约（问题47）：contracts/mock/app 三处同形。

    contracts 快照含 wemm_live/gpu/cpu 且 phase 枚举含 wemm；
    mock 快照带同形假数据、模拟阶段含 wemm；
    app.js 有 sysLine 渲染与 '页库同步' 映射。
    """
    from pathlib import Path as _P
    ui = _P(__file__).resolve().parent.parent / "guiweb" / "ui"
    md = (_P(__file__).resolve().parent.parent / "guiweb" / "contracts.md").read_text(encoding="utf-8")
    _check("contracts: phase 枚举含 wemm", "writing|wemm|done" in md)
    for key in ('"wemm_live"', '"gpu"', '"cpu"'):
        _check("contracts: 快照含 %s" % key, key in md, key)
    mock = (ui / "mock.js").read_text(encoding="utf-8")
    for key in ("wemm_live", "gpu", "cpu:"):
        _check("mock: 快照含 %s" % key, key in mock, key)
    _check("mock: 模拟阶段含 wemm", "{ name: 'wemm'" in mock)
    js = (ui / "app.js").read_text(encoding="utf-8")
    _check("app: sysLine 渲染存在", "function sysLine(snap)" in js)
    _check("app: wemm 映射为页库同步", "wemm: '页库同步'" in js)
    _check("contracts: 快照含 task 归属",
           '"task":"idle|ours|starting|foreign"' in md)
    _check("mock: 快照含 task", "task: 'idle'" in mock and "task: 'ours'" in mock)
    _check("app: 门锁走 task 归属", "snap.task" in js and "t === 'foreign'" in js)
    _check("app: 启动中有文案", "任务启动中" in js)
    _check("app: 生产推送入口存在（问题47附记）",
           "window.__push = function (type, payloadJson)" in js)
    html = (ui / "index.html").read_text(encoding="utf-8")
    _check("html: 索引页有占用行 idxSys", 'id="idxSys"' in html)
    _check("app: 占用行写入 idxSys", "$('idxSys')" in js)


def test_preview_result_mapping():
    """回归（2026-09-08 真机实测）：bridge.preview_poll 曾误读 info["markdown"]
    （extract_preview 的键是 `md`），成功提取在前端永远是"完成"配两块空面板；
    且 reason/route 被丢弃，管线级失败同样冒充"完成"。映射抽成纯函数后锁定。"""
    from guiweb.bridge import Bridge
    m = Bridge._preview_result_of
    ok = m({"ok": True, "info": {"md": "# 标题\n正文", "reason": "",
                                 "route": "local", "cached": False,
                                 "elapsed": 1.2, "chars": 6}})
    _check("preview: 成功时 markdown 落盘", ok["markdown"] == "# 标题\n正文",
           repr(ok["markdown"])[:60])
    _check("preview: 成功时 ok 保持真", ok["ok"] is True)
    _check("preview: 成功时 reason 为空", ok["reason"] == "", repr(ok["reason"]))
    _check("preview: route 透传", ok["route"] == "local", ok["route"])
    _check("preview: rendered_html 由 md 生成", "标题" in ok["rendered_html"],
           repr(ok["rendered_html"])[:80])
    _check("preview: chars/elapsed 透传",
           ok["chars"] == 6 and ok["elapsed"] == 1.2,
           "%r %r" % (ok["chars"], ok["elapsed"]))
    no = m({"ok": True, "info": {"md": None, "reason": "scanned",
                                 "route": "ocr:none", "cached": False,
                                 "elapsed": 0.3, "chars": 0}})
    _check("preview: 未产出时 ok 仍真（进程正常交付）", no["ok"] is True)
    _check("preview: 未产出时 reason 透传（前端据此显示未产出）",
           no["reason"] == "scanned", repr(no["reason"]))
    _check("preview: 未产出时 markdown 为空串", no["markdown"] == "")
    crash = m({"ok": False, "error": "boom"})
    _check("preview: 子进程异常时 ok 为假", crash["ok"] is False)
    _check("preview: 子进程异常时 markdown 为空串", crash["markdown"] == "")
    _check("preview: 空 payload 不抛", m(None)["markdown"] == "")


def test_preview_result_contract_parity():
    """preview result 新字段 contracts/mock/app 三处同形（reason/route/chars）。"""
    from pathlib import Path as _P
    base = _P(__file__).resolve().parent.parent
    md = (base / "guiweb" / "contracts.md").read_text(encoding="utf-8")
    _check("contracts: preview result 含 reason/route/chars",
           "reason, route, cached, elapsed, chars" in md)
    ui = base / "guiweb" / "ui"
    mock = (ui / "mock.js").read_text(encoding="utf-8")
    for key in ("reason: ''", "route:", "chars"):
        _check("mock: preview result 含 %s" % key.strip(), key in mock, key)
    js = (ui / "app.js").read_text(encoding="utf-8")
    _check("app: 未产出分支存在", "未产出 · " in js)
    _check("app: 路由名映射存在", "ROUTE_NAME" in js)
    _check("app: 不再无条件完成", "r.ok && !r.reason" in js)


def test_failures_aggregate_all_libs():
    """回归（诊断页）：failures("") 必须聚合全部库而非当"库名=空串"返回 0 条；
    total=失败条数（此前把"正常文件数"当 total，前端标题显示 176 却只有 2 条真失败）。"""
    from guiweb.bridge import Bridge
    with patch("store.library_entries") as le, \
         patch("store.file_index_rows_for") as fir, \
         patch("guiweb.bridge.Bridge._log_tail",
               return_value=["[t] 提取失败（tbd），记入终态待重试：x.md",
                             "[t] MinerU 云端失败，按提取失败处理（extract-failed）：y.pdf"]):
        le.return_value = [
            {"name": "A", "path": "D:/a", "collection": "ca"},
            {"name": "B", "path": "D:/b", "collection": "cb"},
        ]
        fir.side_effect = [
            {"total": 100, "rows": [("x.md", "tbd", False)]},          # A
            {"total": 40, "rows": [("y.pdf", "extract-failed", True)]},  # B
        ]
        b = Bridge()
        out = b.failures("")
        _check("failures: 全部库不再返回 0 条", out["total"] == 2,
               "total=%r" % out["total"])
        _check("failures: total=失败条数而非正常数", out["total"] == 2 and out["healthy"] == 140,
               "%r/%r" % (out["total"], out["healthy"]))
        _check("failures: 行带库名与日志摘录",
               {r["lib"] for r in out["rows"]} == {"A", "B"}
               and any(r["detail"] for r in out["rows"]))
        _check("failures: multi 标记（前端据此显示库 chip）", out["multi"] is True)
        # 指定单库：只给该库行、multi=False
        fir.side_effect = None
        fir.return_value = {"total": 5, "rows": [("x.md", "tbd", False)]}
        le.return_value = [{"name": "A", "path": "D:/a", "collection": "ca"}]
        single = b.failures("A")
        _check("failures: 单库过滤生效", single["total"] == 1 and single["multi"] is False,
               repr(single))


def test_wemm_status_rows_are_objects():
    """回归（诊断页）：store 层 wemm 行是元组（Flet 共用契约），guiweb 必须转成
    对象，否则前端取 r.rel/r.pages 恒空、failed 恒 falsy（全显示"页库就绪"）。"""
    from guiweb.bridge import Bridge
    with patch("store.library_entries") as le, \
         patch("store.wemm_status_for") as wsf:
        le.return_value = [{"name": "A", "path": "D:/a", "collection": "ca"}]
        wsf.return_value = {"exists": True, "total_pages": 10,
                            "rows": [("p1.pdf", 6, False, ""),
                                     ("p2.pdf", None, True, "extract-failed")]}
        out = Bridge().wemm_status("A")
        _check("wemm: 元组转对象（rel/pages/failed/reason 可读）",
               out["rows"] == [
                   {"lib": "A", "rel": "p1.pdf", "pages": 6,
                    "failed": False, "reason": ""},
                   {"lib": "A", "rel": "p2.pdf", "pages": None,
                    "failed": True, "reason": "extract-failed"}],
               repr(out["rows"]))
        _check("wemm: exists/total_pages 透传",
               out["exists"] is True and out["total_pages"] == 10)


def test_md_to_html_blocks():
    """_md_to_html 升级（检索命中/试验台/正文查看共用渲染器）：标题/表格/
    围栏代码/引用/列表/行内样式都要成标签，且原文尖括号必须转义。"""
    from guiweb.bridge import Bridge
    h = Bridge._md_to_html
    _check("md: h1/h2", "<h1>标题</h1>" in h("# 标题") and "<h2>小节</h2>" in h("## 小节"),
           repr(h("# 标题"))[:60])
    _check("md: 粗体+行内代码", "<b>重点</b>" in h("这是 **重点**") and "<code>片段</code>" in h("用 `片段`"),
           repr(h("这是 **重点**"))[:80])
    _check("md: GFM 表格", "<table>" in h("| A | B |\n|---|---|\n| 1 | 2 |") and "<th>A</th>" in h("| A | B |\n|---|---|\n| 1 | 2 |"))
    _check("md: 围栏代码转义", "<pre><code" in h("```py\nprint(1)\n```") and "print(1)" in h("```py\nprint(1)\n```"))
    _check("md: 引用+列表", "<blockquote>" in h("> 警告") and "<ul>" in h("- 一\n- 二") and "<ol>" in h("1. 甲\n2. 乙"))
    _check("md: XSS 转义", "<script>" not in h("<script>alert(1)</script>") and "&lt;script&gt;" in h("<script>alert(1)</script>"))
    _check("md: 空输入", h("") == "" and h(None) == "")
    _check("md: 旧断言不回归", "标题" in h("# 标题\n正文"))


def test_search_attaches_rendered_html():
    """search() 给非 notice 命中带 rendered_html（前端默认看渲染）。"""
    from guiweb.bridge import Bridge
    text = ("[来源] 技术笔记/foo.md (## 小节) [块 1/2] [置信度 0.72]\n"
            "#  real标题\n\n表格 **加粗**\n---\n"
            "（本次查询整体置信度偏低，以下结果仅供参考）")
    with patch("retriever.hybrid_search", return_value=text):
        out = Bridge().search("测试", top_k=5, libraries="", include_body=True)
    _check("search: 无 error", out["error"] is None, repr(out["error"]))
    _check("search: 两条（命中+notice）", len(out["results"]) == 2, repr(out["results"])[:120])
    hit = out["results"][0]
    _check("search: 命中带 rendered_html",
            "<h1>" in hit.get("rendered_html", "") and "<b>加粗</b>" in hit.get("rendered_html", ""),
            repr(hit.get("rendered_html"))[:120])
    _check("search: notice 不带 rendered_html",
            "rendered_html" not in out["results"][1], repr(out["results"][1])[:120])


def test_read_document_ok_and_failures():
    """read_document：命中给全文+渲染；未知库/未提取/非法路径给 ok:false。"""
    from guiweb.bridge import Bridge
    with patch("store.library_entries",
               return_value=[{"name": "T", "path": "D:/vault", "collection": "c"}]), \
         patch("store.read_document_text",
               return_value=("# 全文\n\n**重点**", "源文件", False)):
        out = Bridge().read_document("T", "a.md")
        _check("doc: ok+全文", out["ok"] is True and out["markdown"] == "# 全文\n\n**重点**", repr(out)[:120])
        _check("doc: 渲染", "<h1>全文</h1>" in out["rendered_html"] and "<b>重点</b>" in out["rendered_html"],
               repr(out["rendered_html"])[:120])
        _check("doc: 字数/路由", out["chars"] == len("# 全文\n\n**重点**") and out["route"] == "源文件"
               and out["truncated"] is False)
    with patch("store.library_entries", return_value=[]):
        bad = Bridge().read_document("NOPE", "a.md")
        _check("doc: 未知库失败", bad["ok"] is False and "库不存在" in bad["error"], repr(bad)[:80])
    with patch("store.library_entries",
               return_value=[{"name": "T", "path": "D:/vault", "collection": "c"}]), \
         patch("store.read_document_text", return_value=(None, "not-cached", False)):
        nc = Bridge().read_document("T", "p.pdf")
        _check("doc: 未提取指引先索引",
                nc["ok"] is False and "增量重建" in nc["error"] and nc["route"] == "not-cached",
                repr(nc)[:120])


def test_read_document_contract_parity():
    """read_document 契约/mock/前端三处同形（对齐 preview 的 parity 用例写法）。"""
    from pathlib import Path as _P
    base = _P(__file__).resolve().parent.parent
    md = (base / "guiweb" / "contracts.md").read_text(encoding="utf-8")
    _check("contracts: read_document 章节存在", "### read_document(" in md)
    ui = base / "guiweb" / "ui"
    mock = (ui / "mock.js").read_text(encoding="utf-8")
    _check("mock: read_document 存在", "read_document:" in mock)
    js = (ui / "app.js").read_text(encoding="utf-8")
    _check("app: 调用 read_document", "API.read_document(" in js)
    _check("app: 正文弹层 id 全挂载",
            all(i in (ui / "index.html").read_text(encoding="utf-8")
                for i in ("mDoc", "docTitle", "docMeta", "docHtml", "docMd",
                          "docTabHtml", "docTabMd", "docTrunc", "docOpen")))


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
