"""semantic.py — 图谱可选语义边（纯读路径）。

用当前嵌入模型把「标题 + 相对路径」编码成向量（encode_safe，与检索共用，
带 CUDA→CPU 自动降级），余弦相似度超阈值的节点对输出为语义边。

- 纯读：不碰 Chroma、不写任何文件；模型加载与首次检索同源同价（30–60s）。
- 结果按 (库范围, 文件数, 最大 mtime, threshold) 缓存在内存；文件无变更时
  重复调用零成本。
- 模型加载失败（离线/显存被占）→ 返回 (edges=[], error=人话)。
"""
import threading

_cache = {}
_lock = threading.Lock()

# 语义编码不建页节点/组节点
_SKIP_TYPES = {"page", "pagegroup"}


def _fingerprint(nodes, threshold):
    mtimes = [n.get("updated") for n in nodes if n.get("updated")]
    return (tuple(sorted({n["lib"] for n in nodes})), len(nodes),
            max(mtimes) if mtimes else 0, threshold)


def compute_edges(nodes, threshold=0.62, progress=None):
    """nodes：graph.build_graph 输出的节点列表。返回 (edges, error)。

    progress(cb_str)：可选回调，用于向 UI 推阶段文案（"正在加载嵌入模型…"）。
    """
    doc_nodes = [n for n in nodes
                 if n["type"] not in _SKIP_TYPES and (n["chunks"] or n["type"] != "pdf")]
    ids = [n["id"] for n in doc_nodes]
    if len(ids) < 3:
        return [], None
    fp = _fingerprint(doc_nodes, threshold)
    with _lock:
        hit = _cache.get(fp)
    if hit is not None:
        return hit, None

    if progress:
        progress("正在加载嵌入模型（首次约 30–60 秒）…")
    try:
        from index import encode_safe
        texts = ["%s %s" % (_stem(n["rel"]), n["rel"]) for n in doc_nodes]
        vecs = encode_safe(texts)
    except Exception as e:  # noqa: BLE001
        return [], "嵌入模型加载失败（%s），语义边不可用；双链与归属边不受影响" % type(e).__name__

    if progress:
        progress("计算相似度…")
    from guiweb.graph_data import select_semantic_edges
    edges = select_semantic_edges(ids, vecs, threshold=threshold)
    with _lock:
        _cache.clear()          # 只保留最新一份，控内存
        _cache[fp] = edges
    return edges, None


def _stem(rel):
    from pathlib import Path
    return Path(rel).stem
