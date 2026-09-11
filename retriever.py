"""retriever.py — 混合检索：BM25 关键词召回 + Dense 向量，RRF 融合 + cross-encoder 精排。"""
import math
import re
import sys
import threading
from collections import Counter

import chromadb

from config import CFG
from index import CHROMA_DIR, COLLECTION_NAME, encode_safe
from library import effective_config, meta_path, resolve_entries
from advice import advice_for

# 检索类配置一律在调用时读 CFG，不在导入时快照——GUI 设置页改完会调
# config_editor.reload_cfg() 原地更新 CFG，模块级常量拿不到新值，
# 而设置页那一组的标题正写着"实时生效"（2026-08-14 审计 F12）。


def _chunk_limit():
    return CFG["return_chunk_limit"]  # 检索时返回给 LLM 的单块最大字符


def _max_chunks_per_file():
    return CFG["max_chunks_per_file"]  # 正文模式下同一文件最多展示块数（防同文件饱和）


# 同节折叠（2026-09-11 问题54）：同一小节的多个命中只交付一份正文，折叠掉的候选不占
# top_k 名额，故正文模式下按 top_k × 该倍数取候选窗口，由 _format_results 边折叠边计数
# 凑满 top_k。4 倍足够：极端"整窗同节"也要同篇封顶 3 块先过滤一遍。
FOLD_WINDOW_FACTOR = 4


def _candidate_window(top_k):
    """正文模式的候选窗口大小（≥top_k；折叠吃掉的候选由窗口补位）。"""
    k = max(1, int(top_k or 1))
    return k * FOLD_WINDOW_FACTOR


# 共享 PersistentClient：此前每次 get_collection 都新建 client，并发搜索时
# 多个 client 同时初始化会竞态建 sqlite/tenant（实测报 "Could not connect to
# tenant default_tenant"）。共享单例 + 初始化互斥锁，读操作天然可并发。
_chroma_client = None
_chroma_client_lock = threading.Lock()


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        with _chroma_client_lock:
            if _chroma_client is None:
                _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _chroma_client


def get_collection(collection_name=COLLECTION_NAME):
    return _get_chroma_client().get_or_create_collection(
        name=collection_name, metadata={"hnsw:space": "cosine"}
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

# 中文停用词（检索噪声：虚词/泛动词/无区分度词）。配合 jieba 分词过滤。
# 注意保留"配置/能力"这类实义词（本系统笔记语境下的关键词不应误滤）。
_CH_STOP = frozenset(
    "的了是在有和就不太人这那也还我一个要会去与或于及之而但并其们"
    "对于为了以及根据相关包括如何什么怎么哪些为什么要需要可以应该"
    "我们你们他们这个那个这样那样时候地方情况问题办法方式途径通过"
    "进行使用用到做了着过吧吗呢哦啊呀呢因为所以然后接着接下来"
)
# 常见中英/编号专名：jieba 精确模式可能切碎的词，2-gram 兜底时已能覆盖；
# 这里列出需整词保留的高价值术语（进入 BM25 词表，提高 IDF 准确性）。
_TERMS = frozenset(("y+", "k-ω", "sst", "cfd", "cad", "gpu", "llm", "rag"))

try:
    import jieba
except ImportError:  # 缺失时降级为纯 2-gram，见 tokenize 说明
    jieba = None

_jieba_warned = False


def tokenize(text):
    """BM25 分词：jieba 精确分词（滤停用词）+ 中文 2-gram 双通道 + 英文 token。

    jieba 负责词级语义（"火箭发动机"→ 一个词，IDF 更准）；2-gram 兜底召回
    （jieba 对专名/未登录词切错时 bigram 仍能命中）；英文/数字走原 token 路。
    双通道并集：精确与召回兼顾（2026-08-13 升级）。

    2026-08-14：jieba 此前是函数内裸 import 且不在 requirements.txt 里，
    任何按 AI_GUIDE 部署的新机器一检索就 ImportError，再被 server 的宽 except
    吞成"（检索失败：No module named 'jieba'）"——检索 100% 不可用（审计 F1）。
    现已加入 requirements；此处仍保留降级：缺 jieba 时只走 2-gram + 英文 token，
    召回略降但系统可用。
    """
    global _jieba_warned
    text = text.lower()
    tokens = []
    for m in re.finditer(r"[a-z0-9][a-z0-9._+-]{1,}", text):
        tok = m.group(0)
        tokens.append(tok)
    zh_segs = [m.group(0) for m in re.finditer(r"[\u4e00-\u9fff]+", text)]
    if zh_segs and jieba is None and not _jieba_warned:
        _jieba_warned = True
        print("[retriever] 未安装 jieba，中文分词降级为纯 2-gram（召回略降）。"
              "建议 pip install jieba", file=sys.stderr)
    for seg in zh_segs:
        if jieba is not None:
            for w in jieba.lcut(seg):
                if w.strip() and w not in _CH_STOP:
                    tokens.append(w)
        tokens.extend([seg[i : i + 2] for i in range(len(seg) - 1)])
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


_bm25_cache = {}


def get_bm25(collection, stamp=None):
    """BM25 缓存按 collection 名隔离（多库各算各的，懒加载）。

    缓存指纹 = (collection.count(), stamp)，stamp 取该库 index_meta 文件的
    mtime_ns（每次索引成功都会 save_meta → os.replace，必变）。

    2026-08-14：原来只比 count。但"改一段文字"通常不改变块数，count 相等
    恰恰是编辑场景的常态，于是缓存永不失效，BM25 一直拿旧文本做关键词召回，
    dense 侧却是新的——两路错位（审计 F7）。放大因素：GUI 是把 index.py 当
    独立子进程拉起的，server 进程里的 reset_bm25_index() 根本不会被调用。
    """
    key = collection.name
    cached = _bm25_cache.get(key)
    try:
        cnt = collection.count()
    except Exception:
        cnt = None
    sig = (cnt, stamp)
    if cached is not None and cached[3] == sig:
        return cached[0], cached[1], cached[2]
    all_data = collection.get(include=["documents", "metadatas"])  # 一次取齐 ids+documents+file
    cached = (BM25(all_data["documents"]), all_data["ids"],
              [m.get("file", "") for m in all_data["metadatas"]], sig)
    _bm25_cache[key] = cached
    return cached[0], cached[1], cached[2]


def _meta_stamp(lib_name):
    """该库指纹文件的 mtime_ns，作为"索引是否被重建过"的信号；取不到返回 None。"""
    try:
        return meta_path(lib_name).stat().st_mtime_ns
    except OSError:
        return None


def _reset_bm25():
    _bm25_cache.clear()


# ---------- 两阶段精排（cross-encoder reranker） ----------

_reranker = None
_reranker_failed = False
_reranker_lock = threading.Lock()  # 懒加载互斥：并发首次搜索会同时触发加载（双份模型 + 显存浪费）
rerank_failures = 0  # 重排执行失败次数（eval 回归据此检测"静默降级"）


def release_reranker():
    """释放常驻重排模型（配合 index.release_model 让显存；下次懒加载回来）。"""
    global _reranker
    _reranker = None


def _get_reranker():
    """懒加载 cross-encoder 重排器。加载失败置标记（本次会话不再重试，降级纯融合）。

    reranker 与 embedding 模型独立，检索时才加载（首次 ~10s + 模型 ~1.1GB）。
    2026-08-15：加载加锁（双重检查）——此前无锁，mcp 并发请求时两个线程
    同时通过 None 检查、各自加载一份模型，后者覆盖前者（孤儿模型占显存）。
    """
    global _reranker, _reranker_failed
    if _reranker is not None or _reranker_failed:
        return _reranker
    with _reranker_lock:
        if _reranker is not None or _reranker_failed:
            return _reranker
        try:
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder(CFG["rerank_model"], max_length=512)
        except Exception as e:
            _reranker_failed = True
            print(f"[retriever] 重排器加载失败（降级纯融合）：{e}", file=sys.stderr)
            return None
    return _reranker


# ---------- 结果格式化 ----------

def _strip_ctx(doc, meta):
    """剥掉块正文开头的锚点前缀行（索引侧拼进去用于嵌入/BM25，展示时不需要）。

    v6 起 metadata 存完整前缀 ctx（文件名/title/tags + 标题路径）；
    v5 及更早只有 hp，故回退到 hp 以兼容尚未重建的旧索引。
    """
    for key in ("ctx", "hp"):
        pre = meta.get(key) or ""
        if pre and doc.startswith(pre + "\n"):
            return doc[len(pre) + 1:]
    return doc


def _truncate_at_line(doc):
    """返回截断：优先落在完整行边界（表格行/段落不拦腰切），最多 ±300 字符。

    索引侧对含表格的超长块"宁大勿断"整块保留（可 >2000），返回侧此前硬切
    2000 字符会把表格行从中间切断；改为在截断点附近找行尾/行首收边。
    """
    limit = _chunk_limit()
    cut = doc[:limit]
    nl = cut.rfind("\n")
    if nl > 0 and limit - nl <= 300:
        cut = doc[:nl]
    else:
        nxt = doc.find("\n", limit)
        if nxt != -1 and nxt - limit <= 300:
            cut = doc[:nxt]
    return cut + "\n" + CFG["truncate_mark"]


def _expand_parent(collection, file, chunk_idx, hp, min_len=300):
    """small-to-big：把命中块所在父节（同 hp 的全部块）按原文顺序拼回完整文本。

    命中块过短或仅为片段时，父节（小节标题下完整内容）比小块更能支撑回答。
    从 Chroma 取同文件全部块（一次查询），筛出 hp 相同的，按 chunk 序号排序拼接。
    父节只有命中块自己（小节没被切碎）时返回空，避免无谓重复。

    2026-08-14 修三处（审计 F18）：
    - 原来排除了命中块本身，标称"父节全文"实则缺一块；
    - 输出顺序是"命中块 + 其余兄弟块"，若命中第 3 块就成了 3,1,2，叙述被打乱；
    - 兄弟块正文带着各自的锚点前缀没剥，父节里每段前都重复一遍标题路径。
    现在返回的是完整、有序、已剥前缀的父节全文，由调用方整体替换展示正文。
    """
    if not hp:
        return ""
    try:
        got = collection.get(where={"file": file}, include=["metadatas", "documents"])
    except Exception:
        return ""
    siblings = []
    for m, doc in zip(got["metadatas"], got["documents"]):
        m = m or {}
        if m.get("hp") != hp:
            continue
        try:
            idx = int(m.get("chunk", 0))
        except (TypeError, ValueError):
            continue
        siblings.append((idx, _strip_ctx(doc or "", m)))
    if len(siblings) <= 1:
        return ""
    siblings.sort(key=lambda x: x[0])
    parent = "\n\n".join(d for _, d in siblings if d)
    if len(parent) <= min_len:
        return ""
    return parent


# ---------- 置信度尺度与分档（2026-09-11 问题54 重标定） ----------
# 尺度本体：BAAI/bge-reranker-v2-m3 经 sentence_transformers.CrossEncoder 取分时，
# 模型自带 sigmoid 激活（实测 ce.predict == sigmoid(HF logits)，逐元素相等），
# 所以 `rr_scores` **本身已经是"该块与查询相关的概率"**（0~1，0.5=无法判断）。
# 此前在 hybrid_search 里又套了一次 sigmoid（见该处注释），把全体分数压进
# (0.5, 0.731)——那才是问题43/45 记录的"噪音地板 0.50~0.52"与"强命中上限 0.73"
# 的真正来源，也是一整套零点重标定的由来。现改为**只 sigmoid 一次**：
# 原始分即展示分，_CONF_ANCHORS / _conf_display 随其服务的压缩一起删除。
#
# 分档线在真分尺度上重测（2026-09-11 九组查询实测，同一台机器/同一模型）：
#   确定命中 top1 = 0.98 / 0.89 / 0.98；同篇次优节 0.76；模糊口语 top1 = 0.28 /
#   0.04 / 0.14；库里不存在的查询 top1 = 0.60（重排器误判）/ 0.009 / 0.017；
#   池内噪音块普遍 <0.05。故 高相关线取 0.75（真命中主力 ≥0.86；0.60 的误判留在
#   中相关，不再被夸成高相关），中/弱分界直接对齐 warn 阈值（单一事实来源）。
# **换重排/嵌入打分模型后必须重测这两条线**，否则档位失真。
CONF_TIER_STRONG = 0.75


def _conf_tier(conf, warn_c):
    """置信度 → 分档词（高相关/中相关/弱相关），附在 [置信度 x.xx·档位] 里。

    参数与比较都在真分尺度（重排器自己的相关概率）上；展示数值即该分数本身。"""
    if conf >= CONF_TIER_STRONG:
        return "高相关"
    if conf >= warn_c:
        return "中相关"
    return "弱相关"


def _format_results(col_map, pairs, file_counts=None, include_body=True,
                    scores=None, small_to_big=False, fold_sections=False, limit=None,
                    cap=None, query=""):
    """选交付 + 格式化：pairs = [(库名, cid)] 按最终排序；col_map = {库名: collection}。

    include_body=False 时只返回 [来源] 清单（文件名+标题+块位置），不返回正文——
    供"先探查全量、再精读个别"的两阶段检索，避免正文整体塞进上下文。
    来源行带 [块 k/N] 位置标记（N 为该文件总块数，从 BM25 缓存派生）与库名前缀
    （同名文件跨库不歧义）。正文超 CHUNK_LIMIT 时截断并附显式标记。
    small_to_big=True 时，命中碎片块的来源行标 [已回填父节全文]，正文整体替换为
    该块所属整节（按原文顺序、已剥锚点前缀），供 LLM 在碎片命中有完整上下文
    （2026-08-13 v5 小块索引 + 2026-08-14 F18 修正的配套）。

    **名额施加在"实际交付"上**（2026-09-11 问题54）——传送进来的是候选窗口，
    本函数按序边走边定交付：
    - limit：最多交付几条（正文模式 = top_k）；折叠/封顶/丢弃掉的候选不占名额。
    - cap：同一文件最多交付几条（正文模式 = max_chunks_per_file）。此前按"命中块数"
      封顶，而 small_to_big 交付的是整节正文：三块命中同一小节 → 送三份逐字节相同的
      正文（实测 md5 相同）、还挤掉别节别篇内容，与"让更多内容进上下文"相反。
    - fold_sections：同一小节的多个命中只交付一次（留分数最高那块当代表），
      块级名次 / [块 k/N] / 置信度标记一概不变；小到不值得回填的节
      （_expand_parent 返回空）不折叠，各块照常交付。
    list 模式三个开关都不生效（无正文可重复，[块 k/N] 即完整性提示）。

    query 只用于结果建议（advice.py：把"结果形态 → 下一步怎么做"教给 agent），
    不参与检索与排序。
    """
    got_map = {}
    by_lib = {}
    for name, cid in pairs:
        by_lib.setdefault(name, []).append(cid)
    for name, cids in by_lib.items():
        col = col_map[name]
        got = col.get(ids=cids, include=["metadatas", "documents"])
        # Chroma get() 不保证返回顺序与传入 ids 一致：先按 cid 映射，
        # 再严格按 cids（排序后）顺序输出——输出顺序 = 检索排序，不得被返回顺序打乱。
        meta_map = dict(zip(got["ids"], got["metadatas"]))
        doc_map = dict(zip(got["ids"], got["documents"]))
        for cid in cids:
            got_map[(name, cid)] = (meta_map.get(cid) or {}, doc_map.get(cid) or "")
    lines = []
    shown = 0          # 实际交付的来源数（低置信过滤 + 折叠 + 封顶后）
    warn_c = CFG.get("confidence_warn_threshold", 0.35)
    drop_c = CFG.get("confidence_drop_threshold", 0.15)
    cap_used = cap if cap is not None else _max_chunks_per_file()
    capped = False
    parent_cache = {}   # (库, rel, hp) → 父节全文：同节多块只查一次（此前每块一次重复 get）
    emitted_sections = set()   # 已交付过正文的小节（同节折叠的去重键）
    per_file = {}       # (库, rel) → 已交付条数（同篇封顶数交付数，不数命中块）
    folded = 0          # 被同节折叠掉的块数（结果建议用）
    hits_info = []      # 每条交付的形态（结果建议用，见 advice.py）
    for name, cid in pairs:
        if limit is not None and shown >= limit:
            break              # 已交付够 limit 条（折叠/封顶/丢弃掉的候选不计入）
        meta, doc = got_map[(name, cid)]
        rel = meta.get("file", "")
        src = f"[来源] {name}/{rel} (## {meta.get('heading', '')})"
        k = meta.get("chunk")
        n = (file_counts or {}).get((name, rel))
        if k is not None and n is not None:
            src += f" [块 {int(k) + 1}/{n}]"
        # 父节正文先算（带缓存）：fold_sections 要按"实际交付的正文"判重，
        # 而小到不值得回填的节（_expand_parent 返回空）仍按块各自交付、不折叠。
        hp = meta.get("hp") or ""
        parent = ""
        if include_body and small_to_big and hp:
            sec_key = (name, rel, hp)
            if sec_key not in parent_cache:
                parent_cache[sec_key] = _expand_parent(
                    col_map[name], rel, int(k) if k is not None else 0, hp)
            parent = parent_cache[sec_key]
            if parent and fold_sections and sec_key in emitted_sections:
                folded += 1
                continue          # 这一节的正文已交付过 → 该块不占名额
        if include_body and cap is not None and per_file.get((name, rel), 0) >= cap:
            capped = True         # 该文件交付额度已满（折叠掉的候选不算数）
            continue
        conf = (scores or {}).get((name, cid))
        if conf is not None:
            # 展示数值 = conf 本身（真分尺度：重排器给出的相关概率），不再重标定
            src += f" [置信度 {conf:.2f}·{_conf_tier(conf, warn_c)}]"
            if conf < drop_c:
                # 低置信护栏（drop）：噪音命中直接不输出，宁缺毋滥——
                # 防止 LLM 把不相关来源当真引用（实测"火箭冷却"混入 agents/test 噪音）。
                # 2026-09-11 尺度修正后此护栏**真的会触发**了（此前被 sigmoid 两次压进
                # (0.5, 0.731)，drop=0.40 数学上不可达）。当前默认 drop=0.0 = 关闭：
                # "低于及格线就少给几条"是产品决策，用户明确先放着；真要开，实测建议
                # 0.05~0.10（池内噪音普遍 <0.05，真命中 ≥0.14），会少于 top_k。
                continue
            if conf < warn_c:
                # 低置信护栏（warn）：照常输出但显式标注，供调用方判断（同一真分尺度）
                src += f"（低置信度 {conf:.2f}，仅供参考）"
        shown += 1
        hits_info.append({"lib": name, "rel": rel, "title": meta.get("title") or "",
                          "score": conf, "backfilled": bool(parent)})
        if not include_body:
            lines.append(src)
            continue
        if parent and fold_sections:
            emitted_sections.add((name, rel, hp))   # 该节正文已交付（后续同节块折叠掉）
        per_file[(name, rel)] = per_file.get((name, rel), 0) + 1
        doc = _strip_ctx(doc, meta)
        if parent:
            # 父节已含命中块且按原文顺序，整体替换即可——再拼一次命中块只会重复。
            # 命中的是哪一块，来源行的 [块 k/N] 已经标了。
            doc = parent
            src += " [已回填父节全文]"
        if len(doc) > _chunk_limit():
            doc = _truncate_at_line(doc)
        lines.append(src)
        lines.append(doc)
        lines.append("---")
    if shown == 0:
        if scores:
            return "未找到相关内容（检索到的命中均低于置信度下限，已过滤；" \
                   "可尝试换关键词、扩库范围或检查是否索引了相关内容）。"
        return "未找到相关内容。"
    # 结果建议（问题55）：把"这一批结果该怎么用"直接教给 agent —— 例如多条强命中
    # 说明该主题内容集中、应把 top_k 调大；命中落在非笔记库；两篇同名不同目录的笔记。
    # 纯规则、随结果给出、不以 [来源] 开头（两套 GUI 都按提示横幅渲染）。
    # 原先单独那条"整体置信度偏低"提示已并入 advice 规则 1（同条件触发，避免重复两行）。
    advice = advice_for(hits_info, query=query, mode="body" if include_body else "list",
                        top_k=limit, capped=capped, folded=folded,
                        default_libraries=CFG.get("default_libraries"),
                        warn=warn_c)
    if advice:
        lines[:0] = ["（%s）" % a for a in advice]
    if capped:
        lines.append(f"（同一文件最多展示 {cap_used} 块"
                     f"{'、同一小节只交付一次全文' if fold_sections else ''}，"
                     f"完整内容请打开源文件）")
    return "\n".join(lines)


# ---------- HyDE（查询侧增强，可选） ----------

def hyde_generate(query, url=None, model=None, timeout=30):
    """调用本地 LLM（LM Studio，OpenAI 兼容）为查询生成一段假设的理想答案文档。

    HyDE 思想（arXiv:2212.10496）：让 LLM 根据查询凭空写一段"如果笔记里有答案，
    大概长什么样"的文本，再拿它去检索。假设文档里会带出笔记真实存在的术语
    （如"个人能力"→"ANSYS Fluent、边界条件、阻力系数"），弥补词面鸿沟。
    失败（LLM 不在线/超时）返回空串，由调用方静默降级为普通检索。
    """
    import json
    import urllib.request

    if url is None:
        url = CFG["hyde_llm_url"]
    if model is None:
        model = CFG["hyde_llm_model"]
    prompt = (
        "你是一个航空航天/工程背景的工程师，正在整理自己的个人知识库笔记。"
        "下面是一个检索查询。请写一段 60~150 字的中文笔记正文，内容是：如果这份笔记里"
        "记录了这个问题，它大概会包含哪些具体工具、术语、专有名词、清单和要点。"
        "要具体、贴近工程实际（如软件名、方法名、参数），不要泛泛而谈通用能力。"
        "直接输出这段笔记正文，不要任何解释、引导语或列表符号外的包装。\n\n"
        f"查询：{query}"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.7,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        return text if text else ""
    except Exception as e:
        print(f"[retriever] HyDE 调用失败（降级普通检索）：{e}", file=sys.stderr)
        return ""


def hybrid_search_hyde(query, top_k=None, libraries="", exclude="", folder="",
                       hyde_enabled=None, defaults=None, **kwargs):
    """HyDE 增强版检索：首轮置信度低时用 LLM 假设文档重查。

    标准流程：普通检索 → 若 top1 置信度 < hyde_min_confidence（说明命中弱）→
    生成假设文档 → 以假设文档为查询重跑 hybrid_search → 两轮取置信度更高者。
    降级链：LLM 失败 → 用原查询结果；配置关闭 → 直接普通检索（零额外开销）。

    2026-08-14（审计 F11）：
    - 此前本函数没有任何调用方（server/GUI 都直接调 hybrid_search），
      整个 HyDE 特性是死代码，把 hyde_enabled 设成 true 也不会发生任何事。
      现已由 server.search_knowledge 接入。
    - 此前判断置信度要靠 _top1_confidence 再跑一整轮完整检索，等于"开启 HyDE
      = 每次检索至少 2 倍开销"。现改为让首轮直接把 top1 置信度带回来。
    - 此前 docstring 说"取置信度更高者"，代码却无条件返回第二轮，现已对齐。
    """
    if hyde_enabled is None:
        hyde_enabled = CFG["hyde_enabled"]
    kwargs.pop("return_top_confidence", None)
    if not hyde_enabled:
        return hybrid_search(query, top_k=top_k, libraries=libraries, exclude=exclude,
                             folder=folder, defaults=defaults, **kwargs)
    first, conf = hybrid_search(query, top_k=top_k, libraries=libraries, exclude=exclude,
                                folder=folder, defaults=defaults,
                                return_top_confidence=True, **kwargs)
    threshold = CFG["hyde_min_confidence"]
    if conf is None or conf >= threshold:
        return first
    print(f"[retriever] 首轮 top1 置信度 {conf:.2f} < {threshold}，"
          f"触发 HyDE 重查：{query}", file=sys.stderr)
    hypo = hyde_generate(query)
    if not hypo:
        return first
    print(f"[retriever] HyDE 假设文档：{hypo[:80]}...", file=sys.stderr)
    second, conf2 = hybrid_search(hypo, top_k=top_k, libraries=libraries, exclude=exclude,
                                  folder=folder, defaults=defaults,
                                  return_top_confidence=True, **kwargs)
    return second if (conf2 or 0.0) > conf else first


# ---------- 混合检索 ----------

def hybrid_search(query, top_k=None, libraries="", exclude="", folder="",
                  dense_weight=None, bm25_weight=None, include_body=True, with_scores=False,
                  small_to_big=None, return_top_confidence=False, defaults=None):
    """混合检索：dense 向量 + BM25 关键词，RRF 融合后取 top_k。

    libraries/exclude 选库（空 = 默认库 defaults；"A,B" 指定；exclude 做减法，见 library.resolve_entries）：
      最终范围 = (libraries 非空 ? libraries : defaults 非空 ? defaults : 全部) − exclude。
    跨库排序：每库 RRF 融合取 top rerank_candidates 进全局重排池，重排器（纯文本打分）
    全局精排；重排不可用时按库归一化融合分合并。结果来源行带 <库名>/<相对路径> 前缀。

    置信度（with_scores=True 时附在来源行）与最终排序**同源**：
      重排生效 → 重排器给出的相关概率本身（0~1，0.5=无法判断；模型自带 sigmoid，
      此处不再二次激活，见下方 all_scores 处注释）；重排不可用 → RRF 双路一致度归一化。
      2026-08-14 修：此前排序用重排分、置信度却用 RRF 分，两套体系无关，
      结果常出现"越往下置信度越高"的单调递增（审计 F6）。

    dense_weight/bm25_weight：RRF 两路权重，score = Σ w/(k+rank)。
      默认 1.0/1.0 = 等权（经典无权重 RRF）。2026-08-14 前这两个参数读了从不用。

    return_top_confidence=True 时返回 (文本, top1 置信度)，供 HyDE 判断是否重查——
    避免为拿一个置信度再跑一整轮检索。
    """
    if top_k is None:
        top_k = CFG["default_top_k"]
    if dense_weight is None:
        dense_weight = CFG["fusion_dense_weight"]
    if bm25_weight is None:
        bm25_weight = CFG["fusion_bm25_weight"]
    if small_to_big is None:
        small_to_big = CFG["small_to_big"]
    folder = _norm_folder(folder)

    def _out(text, conf=None):
        return (text, conf) if return_top_confidence else text

    try:
        entries = resolve_entries(libraries, exclude, defaults=defaults)
    except ValueError as e:
        return _out(f"（{e}）")

    rerank_n = CFG["rerank_candidates"]
    reranker = _get_reranker() if CFG["rerank_enabled"] and rerank_n else None
    col_map = {}
    file_counts = Counter()
    lib_results = []  # (name, collection, combined{cid:融合分}, ranked_all[cid...])
    for entry in entries:
        cfg = effective_config(entry)
        name = cfg["name"]
        collection = get_collection(cfg["collection"])
        col_map[name] = collection

        # dense 检索（不带 where：Chroma 的 $startswith 依赖版本、此处验证已失效；
        # 改为查全库候选后在内存按文件前缀过滤，与 BM25 侧对称）
        emb = encode_safe([query]).tolist()[0]
        dense_k = max(top_k * CFG["dense_candidate_factor"], CFG["dense_min_candidates"])
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
        # 缓存指纹带该库 index_meta 的 mtime：外部进程（GUI 拉起的 index.py 子进程）
        # 重建索引后即使块数不变，也能立即失效重建（审计 F7）。
        bm25, bm25_ids, bm25_files = get_bm25(collection, _meta_stamp(name))
        bm25_scores = bm25.score(query)
        bm25_map = {
            bm25_ids[i]: bm25_scores[i]
            for i in range(len(bm25_scores))
            if bm25_scores[i] > 0 and (not folder or _in_folder(bm25_files[i], folder))
        }
        for rel, cnt in Counter(bm25_files).items():
            file_counts[(name, rel)] = cnt

        # RRF 融合（2026-08-13 起）：两路按排名贡献分，消除量纲差异。
        # 原方案 dense_weight·1/(1+d) + bm25_weight·b/(1+b) 中 BM25 无界、b/(1+b)≈1
        # 恒主导，dense 语义被废——Qdrant 明确"固定 alpha 加权原始分不可靠"。
        # RRF：score = Σ w/(k+rank)，rank 从 1 起；dense 按距离升序、BM25 按分数降序。
        combined = _rrf_combine(dense_ids, dense_dists, bm25_map,
                                dense_w=dense_weight, bm25_w=bm25_weight)

        ranked_all = [c for c, _ in sorted(combined.items(), key=lambda x: x[1], reverse=True)]
        lib_results.append((name, collection, combined, ranked_all))

    # 跨库全局排序
    rr_conf = {}  # {(库名, cid): 重排器给出的相关概率}；非空 = 重排真的生效了
    if reranker is not None and lib_results:
        # 每库融合 top rerank_candidates 进全局重排池（保留融合已认定的强相关块，只做局部调序）
        pool = []
        for name, collection, combined, ranked_all in lib_results:
            pool.extend((name, collection, cid) for cid in ranked_all[:rerank_n])
        if pool:
            try:
                # 按库名分组批量取文档（Collection 对象不可哈希，用 name 作键），
                # 再按池顺序构造 (query, 块) 对
                doc_got = {}
                by_lib = {}
                for name, collection, cid in pool:
                    by_lib.setdefault(name, []).append(cid)
                for name, cids in by_lib.items():
                    got = col_map[name].get(ids=cids, include=["documents"])
                    m = dict(zip(got["ids"], got["documents"]))
                    for cid in cids:
                        doc_got[(name, cid)] = m.get(cid, "")
                rr_scores = reranker.predict([(query, doc_got[(n, c)]) for n, _, c in pool],
                                             batch_size=16)
                ranked_pool = [pair for pair, _ in sorted(zip(pool, rr_scores),
                                                          key=lambda x: -float(x[1]))]
                rr_conf = {(n, c): float(s) for (n, _, c), s in zip(pool, rr_scores)}
                # 全局序 = 重排池（已全局精排）+ 各库池外余量（保持各库融合序）
                tails = []
                for name, collection, combined, ranked_all in lib_results:
                    tails.extend((name, collection, cid) for cid in ranked_all[rerank_n:])
                merged = ranked_pool + tails
            except Exception as e:
                global rerank_failures
                rerank_failures += 1
                print(f"[retriever] 重排失败（按库归一化合并）：{e}", file=sys.stderr)
                merged = _merge_normalized(lib_results)
        else:
            merged = []
    else:
        merged = _merge_normalized(lib_results)

    # 正文模式：交**候选窗口**给 _format_results，由它按"实际交付"施加同篇封顶与 top_k
    # （2026-09-11 问题54）。封顶与截断都改到交付侧的原因：同节折叠会吃掉候选——三块
    # 命中同一小节只交付一份正文，若在挑选阶段就按块数封顶/截断，被折叠的候选会白占
    # 名额、真正的第 4 名（别节别篇）反而进不来（实测案发现场）。
    # (库名, 相对路径) 为封顶键，同名文件跨库不互封顶。
    # list 模式不封顶不折叠（[块 k/N] 标记即完整性提示，无正文可重复）。
    if include_body:
        ranked_pairs = [(n, c) for n, _, c in merged[:_candidate_window(top_k)]]
    else:
        ranked_pairs = [(n, c) for n, _, c in merged[:top_k]]

    # 置信度必须与最终排序同源，否则会出现"越往下分越高"的自相矛盾输出。
    all_scores = {}
    if rr_conf:
        # 重排生效：**直接用重排器给的分，不再套 sigmoid**（2026-09-11 问题54）。
        # 原因：sentence_transformers.CrossEncoder 对 BAAI/bge-reranker-v2-m3 自带
        # Sigmoid 激活（实测 ce.predict == sigmoid(HF logits) 逐元素相等），
        # `reranker.predict` 交出来的已经就是"该块与查询相关的概率"（0.5=无法判断）。
        # 此前这里再套一次 1/(1+exp(-s))，把所有置信度压进 (0.5, 0.731)：
        # "肯定无关"显示成 50%（假的噪音地板）、真·中段被系统性夸大、
        # 而 drop 阈值 0.40 在数学上不可达（护栏成死代码）。只钳位，不二次激活。
        for key, s in rr_conf.items():
            all_scores[key] = max(0.0, min(1.0, float(s)))
    else:
        # 重排未生效（关闭/加载失败/池空）：退回 RRF 双路一致度。
        # RRF 分上限 = (w_dense + w_bm25)/(k+1)（两路都第一），除以它归一化到 0-1。
        k = 2
        rrf_max = (float(dense_weight) + float(bm25_weight)) / (k + 1)
        for name, collection, combined, ranked_all in lib_results:
            for cid in ranked_all:
                all_scores[(name, cid)] = (min(1.0, combined[cid] / rrf_max)
                                           if rrf_max > 0 else 0.0)

    text = _format_results(col_map, ranked_pairs, file_counts=file_counts,
                           include_body=include_body,
                           scores=all_scores if with_scores else None,
                           small_to_big=small_to_big,
                           fold_sections=include_body and small_to_big,
                           limit=top_k if include_body else None,
                           cap=_max_chunks_per_file() if include_body else None,
                           query=query)
    top_conf = all_scores.get(ranked_pairs[0]) if ranked_pairs else None
    return _out(text, top_conf)


def _rrf_combine(dense_ids, dense_dists, bm25_map, k=2, dense_w=1.0, bm25_w=1.0):
    """RRF 融合两路排名：score = Σ_route w_route / (k + rank_route)。

    dense 路 rank 按距离升序（dense_ids 由 Chroma 按距离排好），BM25 路 rank 按
    分数降序。只在一路出现的候选只贡献单路项。返回 {cid: RRF 分}。

    dense_w / bm25_w 来自 config 的 fusion_dense_weight / fusion_bm25_weight，
    默认 1.0/1.0 即经典等权 RRF。2026-08-14 接线：此前这两个配置项是死键
    （hybrid_search 里赋值后从不读取），用户调了完全没有效果（审计 F10）。
    """
    dense_rank = {cid: i + 1 for i, cid in enumerate(dense_ids)}
    bm25_sorted = sorted(bm25_map.items(), key=lambda x: x[1], reverse=True)
    bm25_rank = {cid: i + 1 for i, (cid, _) in enumerate(bm25_sorted)}
    combined = {}
    for cid in set(dense_ids) | set(bm25_map):
        s = 0.0
        r = dense_rank.get(cid)
        if r:
            s += float(dense_w) / (k + r)
        r = bm25_rank.get(cid)
        if r:
            s += float(bm25_w) / (k + r)
        combined[cid] = s
    return combined


def _merge_normalized(lib_results):
    """重排不可用时的降级合并：各库融合分按库内最高分归一化后全局排序。"""
    merged = []
    for name, collection, combined, ranked_all in lib_results:
        best = max(combined.values()) if combined else 0.0
        merged.extend(((name, collection, cid),
                       combined[cid] / best if best > 0 else 0.0)
                      for cid in ranked_all)
    merged.sort(key=lambda x: x[1], reverse=True)
    return [item for item, _ in merged]


def reset_bm25_index():
    _reset_bm25()
