"""retriever.py — 混合检索：BM25 关键词召回 + Dense 向量，加权融合。"""
import math
import re
from collections import Counter

import chromadb

from config import CFG
from index import CHROMA_DIR, COLLECTION_NAME, encode_safe

CHUNK_LIMIT = CFG["return_chunk_limit"]  # 检索时返回给 LLM 的单块最大字符
MAX_CHUNKS_PER_FILE = CFG["max_chunks_per_file"]  # 正文模式下同一文件最多展示块数（防同文件饱和）
TRUNCATE_MARK = CFG["truncate_mark"]


def get_collection():
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )


# ---------- folder 过滤 ----------

def _norm_folder(folder):
    """规范化 folder：去首尾空白与首尾斜杠，\\ → /。"""
    return folder.strip().replace("\\", "/").strip("/").strip()


def _in_folder(rel, folder):
    """相对路径 rel 是否位于 folder 目录下（或等于 folder 本身，即单文件范围）。

    前缀 + 边界校验：folder="AI" 只匹配 "AI/..."，不匹配 "AIML/..."、"AI.md"、
    "AI Dev/..."（folder 须是完整目录名；父目录 "OTHER CERT" 可匹配其下所有子目录）。
    """
    folder = _norm_folder(folder)
    if not folder:
        return True
    return rel == folder or rel.startswith(folder + "/")


def _chunk_file(cid):
    """块 id '{rel}::{i}' → 相对路径 rel（Windows 下相对路径不含 ':'，安全）。"""
    return str(cid).rsplit("::", 1)[0]


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
    def __init__(self, docs, k1=None, b=None):
        if k1 is None:
            k1 = CFG["bm25_k1"]
        if b is None:
            b = CFG["bm25_b"]
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

def _format_result(collection, cids, include_body=True, file_counts=None, capped=False, scores=None):
    """一次批量取 top_k 结果（避免 N+1），并剥离首块的中文锚点。

    include_body=False 时只返回 [来源] 清单（文件名+标题+块位置），不返回正文——
    供"先探查全量、再精读个别"的两阶段检索，避免正文整体塞进上下文。
    来源行带 [块 k/N] 位置标记（N 为该文件总块数，从 BM25 缓存派生——
    与检索内容同源同生命周期，reindex 后同步重建，不会因缓存不重置而撒谎）：
    k<N 即提示该文件还有未展示内容，AI 应据题决定是否 Read 源文件。
    scores 提供时（{cid: 0-1 归一化置信度}），来源行追加 [置信度 0.87] 标记。
    正文超 CHUNK_LIMIT 时截断并附显式标记，避免"截断当完整"。
    """
    got = collection.get(ids=cids, include=["metadatas", "documents"])
    lines = []
    for cid, meta, doc in zip(got["ids"], got["metadatas"], got["documents"]):
        rel = meta.get("file", "")
        src = f"[来源] {rel} (## {meta.get('heading', '')})"
        k = meta.get("chunk")
        n = (file_counts or {}).get(rel)
        if k is not None and n is not None:
            src += f" [块 {int(k) + 1}/{n}]"
        conf = (scores or {}).get(cid)
        if conf is not None:
            src += f" [置信度 {conf:.2f}]"
        if not include_body:
            lines.append(src)
            continue
        anchor = meta.get("anchor") or ""
        if anchor and doc.startswith(anchor):
            doc = doc[len(anchor):]
        if len(doc) > CHUNK_LIMIT:
            doc = doc[:CHUNK_LIMIT] + "\n" + TRUNCATE_MARK
        lines.append(src)
        lines.append(doc)
        lines.append("---")
    if capped:
        lines.append(f"（同一文件最多展示 {MAX_CHUNKS_PER_FILE} 块，完整内容请打开源文件）")
    return "\n".join(lines) if lines else "未找到相关内容。"


# ---------- 混合检索 ----------

def hybrid_search(query, top_k=None, folder="", dense_weight=None, bm25_weight=None,
                  include_body=True, with_scores=False):
    """混合检索：dense 向量 + BM25 关键词，加权融合后取 top_k。

    with_scores=True 时每条来源行附加 [置信度 x.xx]（融合分按本次检索最高分归一化为 0-1）。
    """
    if top_k is None:
        top_k = CFG["default_top_k"]
    if dense_weight is None:
        dense_weight = CFG["fusion_dense_weight"]
    if bm25_weight is None:
        bm25_weight = CFG["fusion_bm25_weight"]
    folder = _norm_folder(folder)
    collection = get_collection()

    # dense 检索（不带 where：Chroma 的 $startswith 依赖版本、此处验证已失效；
    # 改为查全库候选后在内存按文件前缀过滤，与 BM25 侧对称）
    emb = encode_safe([query]).tolist()[0]
    dense_k = max(top_k * CFG["dense_candidate_factor"], CFG["dense_min_candidates"])  # 无条件放大候选池：过滤在取回后做，候选不足会漏（含 folder 场景）
    dense_res = collection.query(
        query_embeddings=[emb],
        n_results=dense_k,
        include=["distances"],
    )
    dense_ids = [
        cid for cid in dense_res["ids"][0]
        if not folder or _in_folder(_chunk_file(cid), folder)
    ]
    dense_dists = [
        d for cid, d in zip(dense_res["ids"][0], dense_res["distances"][0])
        if not folder or _in_folder(_chunk_file(cid), folder)
    ]

    # BM25 检索（与 dense 一样按 folder 过滤，避免越界结果漏入）
    bm25, bm25_ids, bm25_files = get_bm25(collection)
    bm25_scores = bm25.score(query)
    bm25_map = {
        bm25_ids[i]: bm25_scores[i]
        for i in range(len(bm25_scores))
        if bm25_scores[i] > 0 and (not folder or _in_folder(bm25_files[i], folder))
    }
    # 每文件总块数：从 BM25 缓存派生（与检索内容同源同生命周期，reindex 后同步重建）
    file_counts = Counter(bm25_files)

    # 融合打分（dict 查找替代 in/.index() 的 O(M·N) 列表扫描）
    dense_map = dict(zip(dense_ids, dense_dists))
    all_cids = set(dense_ids) | set(bm25_map)
    combined = {}
    for cid in all_cids:
        score = 0.0
        d = dense_map.get(cid)
        if d is not None:
            score += dense_weight * (1.0 / (1.0 + d))  # distance 越小越相似，转成 0-1 相似度
        b = bm25_map.get(cid)
        if b:
            score += bm25_weight * (b / (1.0 + b))  # 归一化
        combined[cid] = score

    ranked_all = [c for c, _ in sorted(combined.items(), key=lambda x: x[1], reverse=True)]

    # 同文件封顶：正文模式下每文件最多 MAX_CHUNKS_PER_FILE 块（按分数保留最高），
    # 边迭代边计数以填满 top_k；list 模式不封顶（[块 k/N] 标记即完整性提示）
    capped = False
    if include_body:
        ranked = []
        per_file = {}
        for cid in ranked_all:
            rel = _chunk_file(cid)
            if per_file.get(rel, 0) >= MAX_CHUNKS_PER_FILE:
                capped = True
                continue
            ranked.append(cid)
            per_file[rel] = per_file.get(rel, 0) + 1
            if len(ranked) >= top_k:
                break
    else:
        ranked = ranked_all[:top_k]

    scores = None
    if with_scores and combined:
        best = max(combined[c] for c in ranked if c in combined)
        if best > 0:
            scores = {c: combined[c] / best for c in ranked}

    return _format_result(collection, ranked, include_body=include_body,
                          file_counts=file_counts, capped=capped, scores=scores)


def reset_bm25_index():
    _reset_bm25()
