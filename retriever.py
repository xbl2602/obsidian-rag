"""retriever.py — 混合检索：BM25 关键词召回 + Dense 向量，RRF 融合。"""
import math
import re
from collections import Counter

import chromadb

from index import CHROMA_DIR, get_model

CHUNK_LIMIT = 2000  # 检索时返回给 LLM 的单块最大字符
ANCHOR_RE = re.compile(r"^【[^\n]*】\n")


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


def build_bm25_index(collection, sample_ratio=1.0):
    """从 Chroma 现有 chunk 构建 BM25 倒排索引（首次调用时构建，缓存在内存）。"""
    all_docs = collection.get(include=["documents", "metadatas"])["documents"]
    if not all_docs:
        return None
    return BM25(all_docs)


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


def get_bm25(collection):
    global _bm25, _bm25_ids
    if _bm25 is None:
        all_docs = collection.get(include=["documents"])["documents"]
        all_ids = collection.get(include=[])["ids"]
        _bm25 = BM25(all_docs)
        _bm25_ids = all_ids
    return _bm25, _bm25_ids


def _reset_bm25():
    global _bm25, _bm25_ids
    _bm25 = None
    _bm25_ids = None


# ---------- RRF 融合 ----------

def rrf_fuse(*ranked_lists, k=60):
    """Reciprocal Rank Fusion：多个候选列表按排名融合。"""
    score_map = {}
    for ranked in ranked_lists:
        for rank, cid in enumerate(ranked):
            score_map[cid] = score_map.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score_map.items(), key=lambda x: x[1], reverse=True)


def _format_result(collection, cids, metas_by_id):
    lines = []
    for cid in cids:
        meta = metas_by_id.get(cid, {})
        doc = collection.get(ids=[cid], include=["documents"])["documents"][0]
        doc = ANCHOR_RE.sub("", doc)  # 剥离检索辅助的中文锚点，只给 LLM 原文
        if len(doc) > CHUNK_LIMIT:
            doc = doc[:CHUNK_LIMIT]
        lines.append(f"[来源] {meta.get('file', '')} (## {meta.get('heading', '')})")
        lines.append(doc)
        lines.append("---")
    return "\n".join(lines) if lines else "未找到相关内容。"


def hybrid_search(query, top_k=5, folder="", dense_weight=0.6, bm25_weight=0.4, alpha=0.7):
    """混合检索：dense 向量 + BM25 关键词，加权融合后取 top_k。"""
    model = get_model()
    collection = get_collection()

    # dense 检索
    emb = model.encode([query], normalize_embeddings=True).tolist()[0]
    where = {"file": {"$startswith": folder}} if folder else None
    dense_k = max(top_k * 4, 20)
    dense_res = collection.query(
        query_embeddings=[emb],
        n_results=dense_k,
        where=where,
        include=["metadatas", "distances"],
    )
    dense_ids = dense_res["ids"][0]
    dense_dists = dense_res["distances"][0]

    # BM25 检索
    bm25, bm25_ids = get_bm25(collection)
    bm25_scores = bm25.score(query)

    # 融合打分
    all_cids = set(dense_ids) | {bm25_ids[i] for i in range(len(bm25_scores)) if bm25_scores[i] > 0}
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

    metas_by_id = {}
    for cid in ranked:
        res = collection.get(ids=[cid], include=["metadatas"])["metadatas"][0]
        metas_by_id[cid] = res
    return _format_result(collection, ranked, metas_by_id)


def reset_bm25_index():
    _reset_bm25()
