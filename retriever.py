"""retriever.py — 混合检索：BM25 关键词召回 + Dense 向量，加权融合。"""
import math
import re
from collections import Counter

import chromadb

from index import CHROMA_DIR, get_model

CHUNK_LIMIT = 2000  # 检索时返回给 LLM 的单块最大字符


def get_collection():
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(
        name="obsidian_kb", metadata={"hnsw:space": "cosine"}
    )


# ---------- BM25 ----------

def tokenize(text):
    """简单分词：英文单词 + 中文连续片段（按 2-gram 切，兼顾中英混合）。"""
    text = text.lower()
    tokens = []
    for m in re.finditer(r"[a-z0-9][a-z0-9._+-]{1,}", text):
        tokens.append(m.group(0))
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        cn = m.group(0)
        tokens.extend([cn[i : i + 2] for i in range(len(cn) - 1)])
    return tokens


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.doc_len = [len(tokenize(d)) for d in docs]
        self.avgdl = sum(self.doc_len) / len(docs) if docs else 0
        self.term_docfreq = Counter()
        self.term_freqs = []
        for d in docs:
            tf = Counter(tokenize(d))
            self.term_freqs.append(tf)
            for t in tf:
                self.term_docfreq[t] += 1
        self.N = len(docs)

    def score(self, query):
        q_tokens = tokenize(query)
        q_tf = Counter(q_tokens)
        scores = [0.0] * self.N
        for t, qf in q_tf.items():
            df = self.term_docfreq.get(t, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for i in range(self.N):
                tf = self.term_freqs[i].get(t, 0)
                if tf == 0:
                    continue
                dl = self.doc_len[i]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[i] += idf * (tf * (self.k1 + 1)) / denom * qf
        return scores


_bm25 = None
_bm25_ids = None
_bm25_files = None


def get_bm25(collection):
    global _bm25, _bm25_ids, _bm25_files
    if _bm25 is None:
        all_data = collection.get(include=["documents", "metadatas"])  # 一次取齐 ids+documents+file
        _bm25 = BM25(all_data["documents"])
        _bm25_ids = all_data["ids"]
        _bm25_files = [m.get("file", "") for m in all_data["metadatas"]]
    return _bm25, _bm25_ids, _bm25_files


def _reset_bm25():
    global _bm25, _bm25_ids, _bm25_files
    _bm25 = None
    _bm25_ids = None
    _bm25_files = None


# ---------- 结果格式化 ----------

def _format_result(collection, cids):
    """一次批量取 top_k 结果（避免 N+1），并剥离首块的中文锚点。"""
    got = collection.get(ids=cids, include=["metadatas", "documents"])
    lines = []
    for cid, meta, doc in zip(got["ids"], got["metadatas"], got["documents"]):
        anchor = meta.get("anchor") or ""
        if anchor and doc.startswith(anchor):
            doc = doc[len(anchor):]
        if len(doc) > CHUNK_LIMIT:
            doc = doc[:CHUNK_LIMIT]
        lines.append(f"[来源] {meta.get('file', '')} (## {meta.get('heading', '')})")
        lines.append(doc)
        lines.append("---")
    return "\n".join(lines) if lines else "未找到相关内容。"


# ---------- 混合检索 ----------

def hybrid_search(query, top_k=5, folder="", dense_weight=0.6, bm25_weight=0.4):
    """混合检索：dense 向量 + BM25 关键词，加权融合后取 top_k。"""
    model = get_model()
    collection = get_collection()

    # dense 检索（不带 where：Chroma 的 $startswith 依赖版本、此处验证已失效；
    # 改为查全库候选后在内存按 file 前缀过滤，与 BM25 侧对称）
    emb = model.encode([query], normalize_embeddings=True).tolist()[0]
    dense_k = max(top_k * 8, 50)
    dense_res = collection.query(
        query_embeddings=[emb],
        n_results=dense_k,
        include=["distances"],
    )
    dense_ids = [cid for cid in dense_res["ids"][0] if not folder or str(cid).startswith(folder)]
    dense_dists = [
        d for cid, d in zip(dense_res["ids"][0], dense_res["distances"][0])
        if not folder or str(cid).startswith(folder)
    ]

    # BM25 检索（与 dense 一样按 folder 过滤，避免越界结果漏入）
    bm25, bm25_ids, bm25_files = get_bm25(collection)
    bm25_scores = bm25.score(query)
    bm25_hits = [
        bm25_ids[i]
        for i in range(len(bm25_scores))
        if bm25_scores[i] > 0 and (not folder or bm25_files[i].startswith(folder))
    ]

    # 融合打分
    all_cids = set(dense_ids) | set(bm25_hits)
    combined = {}
    for cid in all_cids:
        score = 0.0
        if cid in dense_ids:
            idx = dense_ids.index(cid)
            # distance 越小越相似，转成 0-1 相似度
            sim = 1.0 / (1.0 + dense_dists[idx])
            score += dense_weight * sim
        if cid in bm25_ids:
            bidx = bm25_ids.index(cid)
            bscore = bm25_scores[bidx]
            if bscore > 0:
                score += bm25_weight * (bscore / (1.0 + bscore))  # 归一化
        combined[cid] = score

    ranked = [c for c, _ in sorted(combined.items(), key=lambda x: x[1], reverse=True)][:top_k]
    return _format_result(collection, ranked)


def reset_bm25_index():
    _reset_bm25()
