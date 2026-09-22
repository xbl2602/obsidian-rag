"""library_summary.py — 库简介的采样与生成（问题60，纯函数，零副作用）。

背景：list_libraries 只能告诉 agent 每个库的名称/路径/块数这类元数据，回答不了
"这库里到底是什么内容"，agent 只能靠真读文档才知道值不值得查——这就是库简介要
补的空：一段导航/澄清性质的文字，帮 agent 在通读全文之前就能判断"这库大致讲
什么、值不值得往下查"。

几百篇 md、单篇几万字，不可能也不需要全文喂给 LLM 才能写出这段话——库里每个块
在索引时已经过 BGE-M3 编码存进 Chroma，这是免费的副产品。本模块只需在这份已有
的向量空间里做**最远点采样**（farthest-point sampling：确定性、无需迭代收敛、
天然覆盖语义分散区域，比 k-means 更简单可靠）挑出十几个有代表性的块，连同免费
的目录/标题信息一起丢给 LLM 做一次概括——输入规模由采样数 k 固定，与库到底有
300 篇还是 3000 篇文档基本无关。

写盘（落 library.py 的 summary 字段）不在本模块：这里只负责"采样 + 拼 prompt +
调 LLM"，返回生成结果由调用方（server.py / guiweb bridge.py）决定是否需要先过
summary_gate 的确认门禁再落盘（source=user 时才需要）。

LLM 调用复用 retriever.hyde_generate 的方式：OpenAI 兼容 chat completions
端点，本地（如 LM Studio）免鉴权、云端加 Authorization: Bearer <api_key>。
"""
import hashlib
import json
import sys
import urllib.request

import chromadb

from config import CFG, DATA_DIR
from index import load_meta
from library import SUMMARY_MAX_CHARS, effective_config, meta_path

SAMPLE_K = 20  # 采样代表块数：固定值，输入规模不随库大小增长


def log(*args):
    print("[library_summary]", *args, file=sys.stderr)


def content_fingerprint(cfg):
    """库当前内容的指纹：聚合全部已索引文件的 相对路径:md5，任何增删改都会变化。

    用于判断已生成的简介是否可能已过时（is_stale）；不参与采样本身。
    """
    meta = load_meta(meta_path(cfg["name"]))
    parts = sorted(f"{rel}:{info.get('hash', '')}"
                   for rel, info in meta.items()
                   if isinstance(info, dict) and info.get("hash"))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def is_stale(cfg, summary):
    """已有简介（source=ai/user）内容指纹是否已对不上当前库内容。

    没有生成过（source=none 或无 fingerprint 记录）不算"过时"，是另一种状态
    （未生成），调用方分别提示。"""
    fp = summary.get("fingerprint")
    if not fp:
        return False
    return fp != content_fingerprint(cfg)


def _chroma_collection(cfg):
    """直连 Chroma 取指定 collection（与 library.list_summary 同一手法：
    不复用 retriever 的共享 client，避免把检索侧的重导入带进本模块）。"""
    client = chromadb.PersistentClient(path=str(DATA_DIR / "chroma"))
    return client.get_collection(cfg["collection"])


def sample_representative_chunks(cfg, k=SAMPLE_K):
    """最远点采样：从库的全部块向量里选 k 个语义上分散的代表块。

    返回 [{"file", "heading", "title", "text"}, ...]；库未建索引/索引为空
    时返回 []。k 固定 → 输入规模与库大小无关，成本不随文档数线性增长。
    """
    try:
        col = _chroma_collection(cfg)
        n = col.count()
    except Exception as e:
        log(f"库「{cfg['name']}」collection 不可用（未建索引？）：{e}")
        return []
    if n == 0:
        return []
    data = col.get(include=["documents", "metadatas", "embeddings"])
    # 注意：embeddings 可能是 numpy 数组（Chroma 新版本），`arr or []` 对多元素数组
    # 会抛 "truth value of an array is ambiguous"——一律用 is None / len() 判空，
    # 绝不对可能是数组的值用 `or` 做真值判断。
    docs = data.get("documents")
    metas = data.get("metadatas")
    embs = data.get("embeddings")
    if docs is None or len(docs) == 0 or embs is None or len(embs) == 0:
        return []
    if metas is None:
        metas = [{}] * len(docs)
    import numpy as np
    vecs = np.asarray(embs, dtype=float)
    k = min(k, len(docs))
    if k <= 0:
        return []
    chosen = [0]
    dists = np.linalg.norm(vecs - vecs[0], axis=1)
    for _ in range(1, k):
        nxt = int(np.argmax(dists))
        if nxt in chosen:  # 全部重合的退化情形（如全部向量相同），提前收手
            break
        chosen.append(nxt)
        d = np.linalg.norm(vecs - vecs[nxt], axis=1)
        dists = np.minimum(dists, d)
    rows = []
    for i in chosen:
        meta = metas[i] or {}
        rows.append({
            "file": meta.get("file", ""),
            "heading": meta.get("heading", ""),
            "title": meta.get("title", ""),
            "text": (docs[i] or "")[:400],
        })
    return rows


_PROMPT_INSTRUCTIONS = (
    "你在给一个个人知识库写一段简介，供另一个 AI agent 在检索前快速判断"
    "\"这个库大致讲什么、值不值得往这查\"。这段话必须同时做到两件事——"
    "先画范围、再给判断依据，不能只做其中一个：\n"
    "1. 先用一两句给出总体定位（这库大致是什么性质/服务于什么），再概括库内"
    "主要覆盖哪几类主题或板块（口语化提及即可，比如\"主要是……和……\"，"
    "不要用编号或项目符号罗列成清单）；\n"
    "2. 接着明确写清楚\"适合来这库查什么类型的问题\"，以及\"大概率查不到"
    "什么\"（正反两面都要有），直接服务于 agent \"值不值得查\"这个决策，"
    "而不是让 agent 看完主题范围自己再去猜；\n"
    "3. 禁止逐字摘抄下面给的片段原文，必须用自己的话概括转写；\n"
    "4. 不点名任何一篇具体笔记的细节，只讲库整体范围；\n"
    f"5. 100~{SUMMARY_MAX_CHARS} 字，一段话，不用 markdown、不分点、不用标题；\n"
    "6. 直接输出这段简介正文，不要任何解释、引导语或包装。"
)


def build_prompt(cfg, sample_rows):
    """拼装本次请求里"库独有"的那部分内容：目录/标题信息 + 采样代表片段。

    通用的写作规范（_PROMPT_INSTRUCTIONS）不掺进这里——它逐字不变，单独作为
    system 消息发送（见 call_llm），好让本地 llama.cpp/LM Studio 这类服务端的
    提示词前缀缓存能安全地把这段规范缓存住、只对每次都不同的这部分内容重新
    计算，批量连续刷新多个库时能明显提速，而不必为了防串味把缓存整个关掉。
    """
    files = sorted({r["file"] for r in sample_rows if r["file"]})
    lines = [f"库名：{cfg['name']}",
             f"涉及文件（部分，共 {len(files)} 份采样命中）：" + "、".join(files[:15]),
             "", "代表性片段（仅供你概括主题用，不要摘抄）："]
    for r in sample_rows:
        head = " / ".join(x for x in (r["title"], r["heading"]) if x)
        lines.append(f"- [{r['file']}{(' · ' + head) if head else ''}] {r['text']}")
    return "\n".join(lines)


def call_llm(prompt, url=None, model=None, api_key=None, timeout=None, max_tokens=None,
             system=None):
    """调用 OpenAI 兼容 chat completions 端点（本地免鉴权/云端带 Bearer）。

    失败（服务不在线/超时/返回异常）返回空串，由调用方判断是否重试或报错——
    与 retriever.hyde_generate 同一降级口径，但这里不做静默降级，调用方
    （generate_summary）会把空串转成明确的 RuntimeError，因为生成简介是
    显式请求的动作，失败应该让用户看见而不是悄悄什么都没发生。

    timeout/max_tokens 缺省时读 config（library_summary_llm_timeout_seconds
    默认 180、library_summary_llm_max_tokens 默认 2000，均可在设置页调）：
    这里调的是本地推理模型，很可能是"思考型"模型（如 Qwen3 系列，先输出隐藏的
    reasoning_content 再给最终 content），thinking 阶段可能就要吃掉几十秒——
    HyDE 那种查询期的 30s 量级在这里等不到最终答案就被掐断（真实复现：连接在
    30s 整被掐，模型还在 reasoning_content 阶段，content 恒为空）。max_tokens
    同理要给够 thinking + 正文两段的预算，不是只够写最终这一小段简介。

    system 单独成一条 system 消息（默认 _PROMPT_INSTRUCTIONS，逐库调用时这段
    文字逐字不变）而不是拼进 prompt 里的 user 消息——这样批量连续刷新多个库时，
    本地 llama.cpp/LM Studio 这类服务端能把这段共享前缀的 KV 缓存复用（同一份
    system 消息，caching 命中的是完全相同的内容，语义上不存在"串味"风险），
    只对每次都不同的 user 消息（库名/片段）重新计算，兼顾速度与正确性，不需要
    靠强行关闭 prompt 缓存来防污染（真正的串味来自把会变的内容和不变的规范
    混在同一条消息里让缓存前缀匹配点落在了不该落的地方，分成两条消息后
    system 部分永远精确匹配、user 部分永远精确不匹配，没有"匹配到一半"的
    歧义地带）。
    """
    if url is None:
        url = CFG.get("library_summary_llm_url")
    if model is None:
        model = CFG.get("library_summary_llm_model")
    if timeout is None:
        timeout = CFG.get("library_summary_llm_timeout_seconds", 180)
    if max_tokens is None:
        max_tokens = CFG.get("library_summary_llm_max_tokens", 2000)
    if system is None:
        system = _PROMPT_INSTRUCTIONS
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.5,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        if not text:
            # 思考型模型：思考阶段没走完就撞上 max_tokens，content 空、
            # reasoning_content 非空——如实报告，别悄悄把思考过程当简介用
            # （那不是"一段概括"，是内心独白，违反内容规范）。
            if msg.get("reasoning_content"):
                log("LLM 只吐了思考过程（reasoning_content）就用完 token 预算，"
                    "content 为空；若持续出现，考虑调大 max_tokens 或换非思考型模型")
            return ""
        return text
    except Exception as e:
        # 敏感信息（api_key 走 header 不落入异常文本）不进日志，仅类型与摘要
        log(f"LLM 调用失败：{type(e).__name__}: {e}")
        return ""


def generate_summary(lib_name):
    """完整生成流程：采样 → 拼 prompt → 调 LLM → 截断守恒。

    返回 (text, fingerprint, model)。失败抛 ValueError（库不存在/未建索引）
    或 RuntimeError（LLM 调用失败/空返回）——调用方决定如何呈现给用户。
    """
    from library import load_registry
    entry = next((e for e in load_registry() if e["name"] == lib_name), None)
    if entry is None:
        raise ValueError(f"库不存在：{lib_name}")
    cfg = effective_config(entry)
    sample = sample_representative_chunks(cfg)
    if not sample:
        raise ValueError(f"库「{lib_name}」尚未建索引或索引为空，无法生成简介"
                         f"（先调 reindex_knowledge 建好索引再重试）")
    prompt = build_prompt(cfg, sample)
    model = CFG.get("library_summary_llm_model")
    text = call_llm(prompt, model=model, api_key=(CFG.get("library_summary_llm_api_key") or None))
    if not text:
        raise RuntimeError("LLM 生成失败或返回为空（检查 library_summary_llm_url 指向的"
                           "本地/云端服务是否可用、模型名是否正确）")
    text = text.strip()[:SUMMARY_MAX_CHARS]
    return text, content_fingerprint(cfg), model
