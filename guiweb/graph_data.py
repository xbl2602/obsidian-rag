"""graph_data.py — 全库图谱数据构建（纯函数，零模型、零 Chroma 写入）。

从库注册表 / index meta / wemm meta 三个只读来源构建图谱 JSON：
- 节点：meta 内已索引文件（md/txt/docx/pdf）+ 磁盘上未识别的 PDF + WEMM 页节点
- 边：显式双链（meta 的 links 字段现算解析，同 resolve_note_relations 规则）
      + 归属边（PDF → WEMM 页节点）
- 管线状态：PDF 的 MinerU/WEMM 生效情况直接从 meta 终态推导（可验证性核心）：
  meta 有条目且非 xfail → done；scanned 且后端未变更 → queued；其他 xfail → failed；
  磁盘有文件但 meta 无条目 → none（未识别）。

主题族（theme）：按相对路径关键词确定性归族，语义聚落的别名来源。
"""
import re
from pathlib import Path

from index import collect_md_files, load_meta, _backend_changed
from extractors import current_backend_sig
from library import effective_config, load_registry, meta_path
from wemm_indexer import load_wemm_meta, wemm_meta_path

# 主题族关键词（顺序即优先级，命中即停）；大小写不敏感，匹配相对路径
THEME_RULES = [
    ("wemm", ("wemm", "页级", "视觉导航")),
    ("mineru", ("mineru", "ocr", "扫描")),
    ("chunk", ("切块", "清洗", "chunk", "嵌入", "向量", "检索", "重排", "embedding")),
    ("daily", ("journal", "日记", "周会", "会议记录", "meeting")),
    ("config", ("设置", "配置", "config", "门禁", "隐私")),
]
MAX_PAGE_NODES = 24      # 单 PDF 页节点上限，超出折叠为一个组节点
BIG_DEGREE = 5           # hub 节点数


def classify_theme(rel):
    low = rel.lower()
    for name, kws in THEME_RULES:
        for kw in kws:
            if kw.lower() in low:
                return name
    return "general"


def _ext(rel):
    return Path(rel).suffix.lstrip(".").lower() or "md"


def _pdf_pipeline(info, sig):
    """meta 条目 → PDF 的 mineru 管线状态（done/queued/failed）。"""
    if info.get("xfail") or info.get("tbd"):
        reason = info.get("reason") or ""
        if reason == "scanned" and not _backend_changed(info, sig):
            return "queued"
        return "failed"
    return "done"


def build_link_edges(meta):
    """meta → 显式双链边集合 [(relA, relB)]（无向去重）。

    解析规则与 index.resolve_note_relations 一致：links 里的名字先精确匹配
    rel，再按不含扩展名的 stem 匹配；自链跳过。
    """
    real = {k: v for k, v in meta.items() if isinstance(v, dict)}
    by_stem = {Path(k).stem: k for k in real}

    def _resolve(name):
        if name in real:
            return name
        return by_stem.get(Path(name).stem)

    edges = set()
    for rel, info in real.items():
        for name in info.get("links", []) or []:
            r = _resolve(name)
            if r and r != rel:
                edges.add((min(rel, r), max(rel, r)))
    return sorted(edges)


def build_graph_for_lib(cfg):
    """单库 → (nodes, edges)。nodes/edges 均为 dict 列表（JSON 就绪）。"""
    lib = cfg["name"]
    meta = load_meta(meta_path(lib))
    sig = current_backend_sig()
    nodes, edges = [], []

    real = {k: v for k, v in meta.items() if isinstance(v, dict)}

    # ---- 已索引文件节点（含 xfail 终态）----
    for rel, info in sorted(real.items()):
        node = {
            "id": "%s|%s" % (lib, rel),
            "lib": lib, "rel": rel, "type": _ext(rel),
            "chunks": int(info.get("chunks", 0) or 0),
            "updated": info.get("mtime"),
            "fail_reason": (info.get("reason") or None)
                           if (info.get("xfail") or info.get("tbd")) else None,
            "theme": classify_theme(rel),
            "pipeline": {"mineru": "done", "wemm": "none"},
            "big": False,
        }
        if rel.lower().endswith(".pdf"):
            node["pipeline"]["mineru"] = _pdf_pipeline(info, sig)
            if node["pipeline"]["mineru"] != "done":
                node["chunks"] = 0
        nodes.append(node)

    # ---- 磁盘上存在但 meta 无条目的 PDF（未识别）----
    indexed = {rel.lower() for rel in real}
    try:
        for p in collect_md_files(cfg["path"], cfg["exclude_dirs"],
                                  cfg["exclude_files"], cfg["exclude_patterns"],
                                  extensions=["pdf"],
                                  selection=(cfg.get("selection_in"), cfg.get("selection_out")),
                                  selection_default=cfg.get("selection_default", "follow")):
            rel = str(Path(p).relative_to(cfg["path"]))
            if rel.lower() in indexed:
                continue
            nodes.append({
                "id": "%s|%s" % (lib, rel),
                "lib": lib, "rel": rel, "type": "pdf",
                "chunks": 0, "updated": None, "fail_reason": None,
                "theme": classify_theme(rel),
                "pipeline": {"mineru": "none", "wemm": "none"},
                "big": False,
            })
    except Exception:
        pass  # 库目录不可达：跳过磁盘扫描，meta 节点照常输出

    # ---- 显式双链边 ----
    for a, b in build_link_edges(meta):
        edges.append({"a": "%s|%s" % (lib, a), "b": "%s|%s" % (lib, b),
                      "kind": "link"})

    # ---- WEMM 页节点与归属边 ----
    wemm = load_wemm_meta(wemm_meta_path(lib))
    wemm_ok = {}
    for rel, info in wemm.items():
        if rel == "_version" or not isinstance(info, dict):
            continue
        if info.get("tbd") or info.get("xfail"):
            for n in nodes:
                if n["rel"] == rel and n["lib"] == lib and n["type"] == "pdf":
                    n["pipeline"]["wemm"] = "failed"
                    n["fail_reason"] = n["fail_reason"] or info.get("reason") or "页库渲染失败"
            continue
        pages = int(info.get("pages", 0) or 0)
        wemm_ok[rel] = pages
        for n in nodes:
            if n["rel"] == rel and n["lib"] == lib and n["type"] == "pdf":
                n["pipeline"]["wemm"] = "done"
                n["pages"] = pages

    for rel, pages in sorted(wemm_ok.items()):
        pdf_id = "%s|%s" % (lib, rel)
        if pages <= MAX_PAGE_NODES:
            for p in range(1, pages + 1):
                pid = "%s|wemm|%s|p%d" % (lib, rel, p)
                nodes.append({"id": pid, "lib": lib, "rel": rel, "type": "page",
                              "page": p, "chunks": 0, "updated": None,
                              "fail_reason": None, "theme": classify_theme(rel),
                              "pipeline": {"mineru": "done", "wemm": "done"},
                              "big": False})
                edges.append({"a": pdf_id, "b": pid, "kind": "page"})
        else:
            gid = "%s|wemm|%s|grp" % (lib, rel)
            nodes.append({"id": gid, "lib": lib, "rel": rel, "type": "pagegroup",
                          "page": pages, "chunks": 0, "updated": None,
                          "fail_reason": None, "theme": classify_theme(rel),
                          "pipeline": {"mineru": "done", "wemm": "done"},
                          "big": False})
            edges.append({"a": pdf_id, "b": gid, "kind": "page"})

    return nodes, edges


def mark_big(nodes, edges):
    """按连接数标记前 BIG_DEGREE 个 hub 节点（平手按 id 排序稳定选取）。"""
    deg = {}
    for e in edges:
        deg[e["a"]] = deg.get(e["a"], 0) + 1
        deg[e["b"]] = deg.get(e["b"], 0) + 1
    by_id = {n["id"]: n for n in nodes}
    top = sorted(deg.items(), key=lambda kv: (-kv[1], kv[0]))[:BIG_DEGREE]
    for nid, _ in top:
        if nid in by_id:
            by_id[nid]["big"] = True
    return nodes


def build_graph(lib_names=""):
    """库范围（""=全部 / "A,B"）→ 图谱 JSON dict。"""
    from library import resolve_entries
    try:
        entries = [effective_config(e) for e in load_registry()]
        cfgs = resolve_entries(lib_names) if lib_names else entries
    except ValueError:
        cfgs = []
    nodes, edges = [], []
    for cfg in cfgs:
        ns, es = build_graph_for_lib(cfg)
        nodes.extend(ns)
        edges.extend(es)
    mark_big(nodes, edges)
    return {
        "nodes": nodes,
        "edges": edges,
        "libs": [c["name"] for c in cfgs],
        "stats": {"nodes": len(nodes), "edges": len(edges)},
    }


def select_semantic_edges(ids, vectors, threshold=0.62, per_node=4, cap=2000):
    """纯函数：给定节点 id 与已归一化向量，选出语义相似边。

    每节点最多保留 per_node 个最近邻（sim ≥ threshold），全局截断 cap 条。
    返回 [{a, b, sim}]，sim 保留三位。
    """
    import math
    n = len(ids)
    out = []
    for i in range(n):
        vi = vectors[i]
        ni = math.sqrt(float(vi @ vi)) or 1.0
        cand = []
        for j in range(n):
            if i == j:
                continue
            vj = vectors[j]
            nj = math.sqrt(float(vj @ vj)) or 1.0
            sim = float(vi @ vj) / (ni * nj)
            if sim >= threshold:
                cand.append((sim, j))
        cand.sort(key=lambda t: (-t[0], t[1]))
        for sim, j in cand[:per_node]:
            a, b = min(ids[i], ids[j]), max(ids[i], ids[j])
            out.append((a, b, round(sim, 3)))
    # 无向去重（同一对可能被两端各选一次），保序去重后截断
    seen = set()
    dedup = []
    for a, b, sim in out:
        key = (a, b)
        if key in seen:
            continue
        seen.add(key)
        dedup.append({"a": a, "b": b, "sim": sim})
    return dedup[:cap]
