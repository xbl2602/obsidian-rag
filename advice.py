"""advice.py — 检索结果 → 给 AI agent 的"下一步该怎么做"建议（纯函数、零 I/O、零模型）。

动机（2026-09-11 问题55）：agent 拿到检索结果后最常犯的错是**把"这一批结果"当成"全部事实"**
——例如 5 条命中全是 0.95+ 的强命中，其实说明这个主题在库里内容集中，应该把 top_k 调大
拿更多上下文；又例如命中落在 agents/skills 这些"AI 配置"库里（不是用户的笔记），
或者两篇**同名但不同目录**的笔记被当成同一篇（实测「FLUENT配置」：两篇标题一样、
路径不同，列表里看起来像一篇出现 5 次）。
这些"结果形态 → 该怎么用"的经验，与其写在文档里等 agent 去读，不如**随结果自动给出来**。

纪律：
- 只做**确定性规则**（不调模型/网络/不读盘）：同样输入必然同样输出，可单测、可复现；
- 每条建议必须**可执行**（给出具体参数或工具名），否则宁可不给；
- 每次最多 `MAX_LINES` 条，按优先级取——建议本身不能变成噪音；
- 建议行一律不以 `[来源]` 开头 → 两套 GUI 都按"提示横幅"渲染（guiweb 的
  `parse_search_text` 与 Flet 的块切分同规则），agent 则直接读到人话。

覆盖的情况（规则表；`*` = 优先级 1 先给）：

| # | 触发条件 | 建议做的事 |
|---|---|---|
| 1* | 整批最高分 < warn（0.30） | 换用笔记里的原始术语重搜；或用清单模式先枚举候选文件 |
| 2* | 只有 1 条 ≥0.75、其余 <warn | 只引用那一条；要更多上下文就 read_document 读整篇 |
| 3* | 最高分落在中相关（warn~0.75） | 沾边但非直答：read_document 看上下文，或换更具体的问法 |
| 4* | ≥2 条标题相同、路径不同 | 那是**两篇**笔记：引用/打开用完整路径 |
| 5* | 命中落在非默认库（agents/skills/…） | 只要笔记就传 libraries="Obsidian Vault" |
| 6  | ≥3 条 ≥0.75（或最高 ≥0.9 且已给满 top_k） | **把 top_k 调大**（如 15~20）拿同一主题的更多小节 |
| 7  | 同一文件占 ≥60% 交付条数 | 想横向比较就 exclude 该篇再搜，或调大 top_k |
| 8  | 命中含 pdf/docx | read_document 拿已提取的 Markdown 全文；图表/扫描页用 navigate_knowledge |
| 9  | 有父节回填/同节折叠 | 正文已是整节、且同节只交付一次：要原文用 read_document |
| 10 | 清单模式（include_body=False） | 挑 1~3 条再 read_document 精读，别一次读全部 |
| 11 | 交付 ≤2 条（且非 #1 场景） | 放宽范围：去 folder/exclude 限制、libraries 用默认库或 all、或换同义词 |
| 12 | query 像关键词罗列（无问句、短） | 关键词精确但覆盖窄：问"怎么做/为什么"用完整问句更全 |

阈值与 retriever/config 同尺度（真分 = 重排器给出的相关概率）：强相关线取
retriever.CONF_TIER_STRONG，低置信线取 config.confidence_warn_threshold。
**换打分模型后这两条线要重测**（与 retriever 同一条纪律）。
"""

# 与 retriever.CONF_TIER_STRONG / config.confidence_warn_threshold 同值（单一尺度）
STRONG_DEFAULT = 0.75
WARN_DEFAULT = 0.30
MAX_LINES = 2

# "不是用户笔记"的库名特征（AI 配置/技能库）；只有出现在命中里且不在默认库集合时提醒
_NON_NOTE_HINT = ("agents", "skills", "test")


def _norm_title(s):
    """标题归一化：去所有空白 + 小写。用于发现"同名不同路径"的笔记
    （实测两篇 FLUENT 笔记标题只差一个空格，肉眼在列表里分不出来）。"""
    return "".join(str(s or "").split()).lower()


def _stem(rel):
    return str(rel or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _ext(rel):
    r = str(rel or "")
    return r.rsplit(".", 1)[-1].lower() if "." in r.rsplit("/", 1)[-1] else ""


def _looks_keywordish(query):
    """像"关键词罗列"而不是一句问话：短、无问号、无常见疑问/动词虚词。"""
    q = (query or "").strip()
    if not q or "?" in q or "？" in q:
        return False
    if len(q) > 12:
        return False
    return not any(w in q for w in ("怎么", "如何", "为什么", "哪些", "什么", "是否", "能不能"))


def advice_for(hits, query="", mode="body", top_k=None, capped=False, folded=0,
               default_libraries=None, warn=WARN_DEFAULT, strong=STRONG_DEFAULT,
               max_lines=MAX_LINES):
    """检索结果 → 建议行列表（按优先级，≤max_lines 条）。

    hits（按交付顺序、已过折叠/封顶/低置信过滤）：每项一个 dict，键——
      lib 库名；rel 库内相对路径；title 笔记标题（可空，取 meta["title"]）；
      score 置信度（真分；None = 无分数）；backfilled 该条正文是否回填为整节。
    query 原查询（用于判"关键词式"）；mode "body"|"list"（正文/清单）；
    top_k 本次请求条数（用于判"已给满"）；capped 同篇封顶是否触发（说明该篇还有内容）；
    folded 被同节折叠掉的块数；default_libraries 默认库集合（判"非笔记库命中"；
    传 None = 不判该规则）。
    """
    out = []
    hits = [h for h in hits if isinstance(h, dict)]
    if not hits:
        return out
    scores = [h.get("score") for h in hits if h.get("score") is not None]
    top = max(scores) if scores else None
    n_strong = sum(1 for s in scores if s >= strong)
    delivered = len(hits)
    libs = []
    for h in hits:
        if h.get("lib") not in libs:
            libs.append(h.get("lib"))

    # ---- 优先级 1：先防止 agent 误用这一批结果 ------------------------------
    if top is not None and top < warn:
        out.append("整批命中的相关度都偏低（最高 %.2f）：以下结果仅供参考，库里可能没有"
                   "直接答案。建议改用笔记里出现的原始术语重搜，或先传 "
                   "include_body=false 枚举候选文件。" % top)
    elif n_strong == 1 and delivered > 1:
        out.append("只有 1 条真正相关（其余低于 %.2f）：请只引用这一条；"
                   "要更多上下文可用 read_document 读该篇全文。" % warn)
    elif top is not None and top < strong and n_strong == 0:
        out.append("最高一条属中相关（%.2f）：沾边但不是直答。建议用 read_document "
                   "看它的完整上下文，或把问题问得更具体再搜。" % top)

    if len(out) < max_lines:
        # 同名不同路径 = 两篇笔记（列表里只用文件名，最容易看错）
        by_title = {}
        for h in hits:
            key = _norm_title(h.get("title")) or _norm_title(_stem(h.get("rel")))
            by_title.setdefault(key, []).append((h.get("lib"), h.get("rel")))
        dup = next((v for v in by_title.values() if len(set(v)) > 1), None)
        if dup:
            uniq, paths = set(), []
            for lib, rel in dup:
                if (lib, rel) not in uniq:
                    uniq.add((lib, rel))
                    paths.append("%s（%s）" % (rel, lib))
            out.append("注意同名不同目录：「%s」下有 %s 是**两篇不同笔记**（标题一样、内容不同），"
                       "引用与打开请用完整路径区分。" % (_stem(dup[0][1]), "；".join(paths[:2])))

    if len(out) < max_lines and default_libraries:
        # 只在配置了非空默认库集合时判"非笔记库"（空 = 默认就是全部库，无所谓越界）
        foreign = [l for l in libs if l not in set(default_libraries)]
        if foreign:
            out.append("命中含非笔记库（%s）：那是 AI 配置/技能内容，不是你的笔记。"
                       "只要笔记请传 libraries=\"%s\"。"
                       % ("、".join(foreign), default_libraries[0]))

    # ---- 优先级 2：怎么拿到更多/更合适的上下文 ------------------------------
    if len(out) < max_lines and top is not None and (
            n_strong >= 3 or (n_strong >= 1 and top >= 0.90 and top_k and delivered >= top_k)):
        out.append("多条高置信命中（%d 条 ≥%.2f，最高 %.2f）说明该主题内容集中："
                   "把 top_k 调大（如 15~20）可拿到同一主题的更多小节与笔记。"
                   % (n_strong, strong, top))

    if len(out) < max_lines and delivered >= 3:
        per_file = {}
        for h in hits:
            per_file[(h.get("lib"), h.get("rel"))] = per_file.get((h.get("lib"), h.get("rel")), 0) + 1
        (fl, frel), fn = max(per_file.items(), key=lambda kv: kv[1])
        if fn >= max(2, int(round(delivered * 0.6))):
            out.append("命中集中在《%s》（%d/%d 条）：想横向比较其它笔记，"
                       "可传 exclude=\"%s\" 再搜一次，或把 top_k 调大。"
                       % (_stem(frel), fn, delivered, frel))

    if len(out) < max_lines:
        exts = {_ext(h.get("rel")) for h in hits}
        if exts & {"pdf", "docx"}:
            out.append("命中含 PDF/Word：read_document 可拿已提取的 Markdown 全文，"
                       "图表或扫描页内容用 navigate_knowledge 看页。")

    if len(out) < max_lines and (folded > 0 or any(h.get("backfilled") for h in hits)):
        out.append("命中正文已按小节回填、且同一小节只交付一次：要看原文全文用 "
                   "read_document，要看它连到哪些笔记用 note_relations。")

    if len(out) < max_lines and mode == "list":
        out.append("这是候选清单（只有来源行、无正文）：挑 1~3 条再 read_document 精读，"
                   "不要把整清单都读进来。")

    if len(out) < max_lines and delivered <= 2 and (top is None or top >= warn):
        out.append("命中很少（%d 条）：可去掉 folder/exclude 限制、把 libraries 放宽到"
                   "默认库或 \"all\"，或换同义词再搜。" % delivered)

    if len(out) < max_lines and _looks_keywordish(query):
        out.append("关键词式查询命中精确但覆盖窄：若想问\"怎么做/为什么\"这类，"
                   "换成完整问句（一句自然语言的问题）结果会更全。")

    return out[:max_lines]
