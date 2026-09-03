"""wemm_retriever.py — WEMM 页级视觉导航检索（跑在项目 .venv）。

wemm_search(query, libraries, top_k) → 把查询文字编码成向量，在每库的
`wemm_<库collection>` 页级别量库（wemm_indexer.py 建）里余弦检索，返回
「内容在哪个 PDF 的哪一页」，供 navigate_knowledge MCP 工具消费。

与文字索引（retriever.py/hybrid_search）**彻底分离**：
  - 只查 wemm_* 页库，绝不与 bge-m3 文本分数混合（避免问题20 置信度虚高）；
  - 返回结构带库/绝对路径/页码，让有视觉能力的模型可直读原 PDF。
"""
import json
import urllib.error
import urllib.request

import chromadb
import index as _index_mod
from library import effective_config, load_registry

from wemm_indexer import call_embed_text, wemm_collection


def _chroma_dir():
    return _index_mod.CHROMA_DIR


def wemm_search(query, libraries=None, top_k=5, url=None, dim=None, backend=None):
    """WEMM 页级检索。

    query：查询文字。libraries：库名列表（None=全部注册库有页库的）。
    返回 [(库, PDF相对路径, PDF绝对路径, 页码, score)]，按 score 降序。
    backend 为 None 时读 config wemm_backend；为 off 时返回 []。
    远程服务不可用时返回 [] 且带 error（调用方决定是否提示）。
    """
    from config import CFG
    if backend is None:
        backend = CFG.get("wemm_backend", "off")
    if backend not in ("on", "local"):
        return [], "WEMM 后端未开启（wemm_backend=off）"
    url = (url or CFG.get("wemm_url"))
    dim = dim or int(CFG.get("wemm_dim", 512))
    if not url:
        return [], "wemm_url 未配置"
    url = str(url).rstrip("/")

    # 断言看图服务可用
    try:
        health(url)
    except Exception as e:
        return [], f"WEMM 看图服务不可用：{type(e).__name__}（先启动 wemm_server.py）"

    # 确定要查的库
    entries = load_registry()
    targets = []
    for e in entries:
        if libraries and e["name"] not in libraries:
            continue
        targets.append(effective_config(e))

    # 编码查询
    try:
        qvec = call_embed_text(query, dim, url)
    except Exception as e:
        return [], f"查询编码失败：{type(e).__name__}"

    results = []
    try:
        client = chromadb.PersistentClient(path=str(_chroma_dir()))
    except Exception as e:
        return [], f"Chroma 打开失败：{type(e).__name__}"
    for cfg in targets:
        collection_name = wemm_collection(cfg["collection"])
        try:
            collection = client.get_or_create_collection(
                name=collection_name, metadata={"hnsw:space": "cosine"})
            if collection.count() == 0:
                continue
            hits = collection.query(query_embeddings=[qvec], n_results=min(top_k, collection.count()),
                                    include=["metadatas", "distances"])
        except Exception as e:
            continue  # 某库页库损坏/缺失，跳过不阻断整体
        metas = hits.get("metadatas")
        dists = hits.get("distances")
        if not metas or not metas[0]:
            continue
        for m, d in zip(metas[0], dists[0]):
            score = 1.0 - float(d)  # cosine 距离 → 相似度
            results.append((cfg["name"], m.get("file"), m.get("abs_path"),
                            m.get("page"), round(score, 4)))
    results.sort(key=lambda x: x[4], reverse=True)
    return results[:top_k], None


def health(url):
    """断言本地看图服务存活；失败抛异常。"""
    req = urllib.request.Request(url.rstrip("/") + "/health")
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "health 异常")
    return data
