"""config.py — 统一配置中心：全部可调参数集中在 data/config.json，改配置不再翻源码。

行为：
- data/config.json 不存在时，首次导入自动创建一份带完整注释的默认配置；
- 支持 // 行注释与尾随逗号（宽容解析），无法解析时回退默认值并告警；
- 未知字段忽略并提示；缺失字段用 DEFAULTS 回填，加载后代码始终能取到值；
- 解析为模块级 CFG dict，各模块 import 一次即可使用。
"""
import json
import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"

# 默认值 = 当前代码的既有行为；模板注释缺失时以此兜底（见 file CONFIG_TEMPLATE）。
DEFAULTS = {
    # ---- 知识库 / 索引范围 ----
    "vault": r"D:\_STOREROOM\lol\Obsidian Vault",
    "exclude_dirs": [".obsidian", ".smart-env", ".trash", ".git", "TEMP", "templates"],
    "exclude_files": ["目录.md", "AGENTS.md", "LOG.md", "README.md"],
    "exclude_patterns": ["session-", "会话", ".tmp"],
    "model_name": "BAAI/bge-m3",
    "collection_name": "obsidian_kb",

    # ---- 切块（索引粒度）----
    "chunk_char_limit": 600,       # 2026-08-13：1500→600（小块语义纯净，检索粒度细；主流 ~300 token）
    "short_doc_char_limit": 200,

    # ---- 嵌入与硬件 ----
    "embed_batch_size": 8,        # index_vault 分批嵌入的批次（自动按显存收紧，此为上限）
    "encode_batch_size": 32,      # encode_safe 默认批次（查询/编码入口通用，自动按显存收紧）
    "cuda_cooldown_seconds": 300, # CUDA 失败后冷却期

    # ---- 锁与并发 ----
    "lock_timeout_seconds": 60,
    "lock_poll_seconds": 0.5,

    # ---- 进度报告（心跳双通道）----
    "heartbeat_interval": 5.0,
    "heartbeat_timeout": 15.0,
    "stall_timeout": 25.0,

    # ---- 检索与格式化 ----
    "return_chunk_limit": 2000,  # 单块返回最大字符
    "max_chunks_per_file": 3,    # 正文模式每文件最多块数
    "truncate_mark": "… [本块已截断，完整内容见源文件]",
    "small_to_big": True,        # 命中小块时回填父节全文（v5 小块索引配套）,
    "bm25_k1": 1.5, "bm25_b": 0.75,
    # RRF 融合的两路权重（score = Σ_route w_route / (k + rank_route)）。
    # 1.0/1.0 = 等权，即经典无权重 RRF（当前默认，与 2026-08-13 v5 行为一致）。
    # 调大 dense 偏语义、调大 bm25 偏关键词。2026-08-14：此前这两个键是死键（读了从不用）。
    "fusion_dense_weight": 1.0, "fusion_bm25_weight": 1.0,
    "dense_candidate_factor": 8,
    "dense_min_candidates": 200,
    "rerank_model": "BAAI/bge-reranker-v2-m3",  # 两阶段精排的 cross-encoder 模型
    "rerank_candidates": 50,   # 融合 top N 候选交给重排器精排（2026-08-13：10→50，融合排序有误差，池太小好块进不了决赛）
    "rerank_enabled": True,    # 重排总开关（False = 纯融合排序）

    # ---- HyDE 查询增强（可选，需本地 LLM）----
    "hyde_enabled": False,         # 默认关：开启后对泛化查询生成假设文档再检索（需 LM Studio）
    "hyde_llm_url": "http://localhost:1234/v1/chat/completions",
    "hyde_llm_model": "qwen2.5-3b-instruct",
    "hyde_min_confidence": 0.5,    # 首轮 top1 置信度低于此值才触发 HyDE（命中好的查询零开销）

    # ---- 工具默认值 ----
    "default_top_k": 5,

    # ---- 导出/导入 ----
    "keep_exports": 3,
    "import_upsert_batch": 500,
}

CONFIG_TEMPLATE = """\
// ============================================================
// obsidian-rag 统一配置中心
//
// 如何改：编辑本文件即可，无需动代码。保存后新进程自动生效。
//   生效前提：修改后重启 server（MCP 重连）或重跑 index.py。
//   哪些改动需全量重建（--full）：affect model_name，切块类
//   （chunk_char_limit / short_doc_char_limit），排除名单
//   （exclude_*），collection_name，以及 vault 路径变更时——
//   旧向量与新设置不匹配，增量无法修正结构差异。
//  哪些无需重建（即时生效）：检索/格式化/锁/进度等参数。
//
// 语法说明：// 行注释 与 尾随逗号 会被宽容解析；键名固定。
// 删除本文件 = 重置默认；误改坏文件 = 自动回退默认值（不崩溃）。
// ============================================================

{
  // ----------------------------------------------------------
  // 一、知识库与索引范围（改动需 --full 重建）
  // ----------------------------------------------------------

  // Obsidian 知识库根目录（绝对路径）。索引与检索都基于它。
  // 填: 形如 "D:\\\\_STOREROOM\\\\lol\\\\Obsidian Vault"（JSON 中反斜杠要双写，
  //     或直接用正斜杠 "D:/.../" 也行）。
  // 环境变量 OBSIDIAN_VAULT 若已设置会优先于此项（接收端/容器场景）。
  "vault": "D:\\\\_STOREROOM\\\\lol\\\\Obsidian Vault",

  // 排除的目录名：目录树中任一层的同名目录整个跳过（不索引）。
  // 填专业目录名列表。示例：想禁用检索 TEMP 学习区可在注释里加 "TEMP"。
  "exclude_dirs": [".obsidian", ".smart-env", ".trash", ".git", "TEMP", "templates"],

  // 排除的文件名（精确匹配文件名，任何目录下同名都不索引）。
  // 当前排除的是结构/指令类文件，避免“检索污染”——若想让某文件可检索，从
  // 这里移除即可（无需重建，但需索引刷新只需自动增量）。
  "exclude_files": ["目录.md", "AGENTS.md", "LOG.md", "README.md"],

  // 按文件名前缀排除（文件名以任一元素开头即跳过）。
  // 用于跳过 session 转录/临时文件；如需纳入只需删掉对应元素。
  "exclude_patterns": ["session-", "会话", ".tmp"],

  // 嵌入模型名（sentence-transformers 标识）。换模型 = 换向量空间：
  // 旧向量与新向量不可混用，必须 python index.py --full 全量重嵌。
  // 影响：决定中文语义检索质量与嵌入维度；bge-m3 为中文场景默认好选择。
  "model_name": "BAAI/bge-m3",

  // Chroma collection 名。多库场景请用 library.py 管理注册表（data/libraries.json），
  // 本项仅作单库/全局默认；每个库可独立设置 collection。
  "collection_name": "obsidian_kb",

  // ----------------------------------------------------------
  // 二、切块粒度（索引结构类，改后需 --full 重建）
  // ----------------------------------------------------------

  // 单个块的最大字符数。超过该值的标题块按“段落切分”，段落仍超长再
  // “按句子切分”（永不从句子中间剪断）。
  // 调大：块更完整、上下文碎片少，但检索粒度粗、命中结果更长（更费 token）；
  // 调小：粒度更细、命中更准，代价是单块上下文不足——由 small_to_big
  //       （命中后回填父节全文）补偿，两者是配套设计。
  // 建议值 400~800（v5 起走“小块检索 + 父节回填”路线；主流实践约 300 token）。
  // 若关掉 small_to_big，则应回到 1200~2000 的大块路线。
  // 修改后务必 --full 重建对比效果。
  "chunk_char_limit": 600,

  // 正文短于此字符数的文件不切块，整体作为一块（标题用 frontmatter title）。
  // 调大可让更多小笔记保持整篇；调小则更多小笔记也按标题切。一般无需改动。
  "short_doc_char_limit": 200,

  // ----------------------------------------------------------
  // 三、嵌入与硬件（性能/显存）
  // ----------------------------------------------------------

  // 索引嵌入的分批大小。显存/内存紧张时调小（如 8），显存充裕调大（如 64）
  // 可略微提速。影响：显存峰值 + 进度数字跳动频率（小批更平滑）。
  // 运行时还会按当前可用显存自动收紧（此值为上限），一般无需手动调整。
  "embed_batch_size": 8,

  // 通用编码默认批次大小（查询编码等单点编码用，索引批大小另由上项控制）。
  // 影响：单次 encode 的显存；运行时同样自动按显存收紧。
  "encode_batch_size": 32,

  // CUDA 失败（OOM 等）后冷却的秒数：冷却期内不尝试 GPU，到期自动轻量探测。
  // 影响：显存暂时不足时避免反复崩；显存恢复后最多等待一个冷却期切换。
  "cuda_cooldown_seconds": 300,

  // ----------------------------------------------------------
  // 四、写锁与索引并发（防 Chroma 写损坏）
  // ----------------------------------------------------------

  // 等待写锁的最大秒数：超时抛“锁繁忙”明确错误（不再无限死等）。
  // 如果经常“多进程同时索引”，可调大；单进程场景 60 已足够。
  "lock_timeout_seconds": 60,

  // 获锁失败后的重试间隔（秒）。仅当多进程并发索引频繁时调整。
  "lock_poll_seconds": 0.5,

  // ----------------------------------------------------------
  // 五、进度健康判定（后台索引上报 data/index_progress.json）
  // ----------------------------------------------------------

  // 心跳写盘间隔（秒）。独立线程恒定刷新（与硬件无关）。
  "heartbeat_interval": 5.0,

  // 心跳停止上限（秒）：超过即判定“疑似卡死”（卡死判定里固定基数）。
  // 一般保持 3×interval。
  "heartbeat_timeout": 15.0,

  // 进度停滞上限（秒）：心跳正常但进度超过此值未推进 → 判定“批次内卡死”。
  // 一般保持 5×interval。
  "stall_timeout": 25.0,

  // ----------------------------------------------------------
  // 六、检索结果与格式化（实时生效，无需重建）
  // ----------------------------------------------------------

  // 单个返回块正文的最大字符数：超出截断并附截断标记。
  // 影响：回答注入 LLM 的 token 量。调小更省，调大更完整。
  "return_chunk_limit": 2000,

  // 正文模式下同一文件最多展示的块数（防单个文件“霸屏”top_k）。
  // 想看到某文件的更多块时，可暂时调大（建议 3~5）。
  "max_chunks_per_file": 3,

  // 块被截断时附加的提示文案（搜索输出末尾）。
  "truncate_mark": "… [本块已截断，完整内容见源文件]",

  // BM25 参数（k1 饱和/ b 长度归一；标准值 1.5/0.75 一般不用动）。
  "bm25_k1": 1.5,
  "bm25_b": 0.75,

  // RRF 融合的两路权重：score = Σ_route w_route / (k + rank_route)。
  // 1.0 / 1.0 = 等权，即经典无权重 RRF（推荐默认）。
  // 偏语义检索调大 dense；偏关键词/专名检索调大 bm25。实时生效，无需重建。
  "fusion_dense_weight": 1.0,
  "fusion_bm25_weight": 1.0,

  // Dense 候选池 = max(top_k × 候选系数, 最小候选数)。
  // 先取大量候选再做融合与排序，保证小 top_k 时融合质量；越大越慢更准。
  "dense_candidate_factor": 8,
  "dense_min_candidates": 200,

  // 两阶段精排（cross-encoder reranker）：融合取 top rerank_candidates 后，
  // 用 BAAI/bge-reranker-v2-m3 对 (query, 块) 逐对精排再取 top_k。
  // 解决"相关块与无关块融合分太接近导致排序不稳"（实测 top3 命中 3/5 → 5/5）。
  // rerank_candidates 建议 30~80：融合排序本身有误差，池子太小好块进不了决赛
  //   （v5 起 10→50）。越大越慢；网络首次加载需下载模型 ~1.1GB。
  // rerank_enabled=false 时回到纯融合排序（模型加载失败也会自动降级）。
  "rerank_model": "BAAI/bge-reranker-v2-m3",
  "rerank_candidates": 50,
  "rerank_enabled": true,

  // small-to-big：命中小块后，把该块所属父节（同一标题路径下的相邻块）
  // 按原文顺序拼全一并返回，补偿小块上下文不足。与 chunk_char_limit=600
  // 是配套设计——若把块调回 1200+ 的大块路线，这里应设 false，否则返回
  // 内容会明显冗长。实时生效，无需重建。
  "small_to_big": true,

  // ----------------------------------------------------------
  // 六之二、HyDE 查询增强（可选，需本地 LLM，默认关）
  // ----------------------------------------------------------

  // 思路（arXiv:2212.10496）：首轮检索命中很弱时，让本地 LLM 先凭查询写一段
  // “假设答案”，再拿这段文本去检索，用它带出的专业术语弥补提问与笔记的词面鸿沟。
  // 代价：触发时会多跑一轮检索 + 一次 LLM 调用。命中好的查询不触发，零开销。
  // 需要一个 OpenAI 兼容的本地服务（如 LM Studio）在 hyde_llm_url 上监听。
  "hyde_enabled": false,
  "hyde_llm_url": "http://localhost:1234/v1/chat/completions",
  "hyde_llm_model": "qwen2.5-3b-instruct",

  // 首轮 top1 置信度低于此值才触发 HyDE。置信度取重排器分数的 sigmoid
  // （重排不可用时退回 RRF 双路一致度），0~1。调大 = 更爱触发。
  "hyde_min_confidence": 0.5,

  // ----------------------------------------------------------
  // 七、MCP 工具默认值
  // ----------------------------------------------------------

  // search_knowledge 的参数 top_k 默认值。调用处仍可显式传参覆盖。
  "default_top_k": 5,

  // ----------------------------------------------------------
  // 八、导出 / 导入
  // ----------------------------------------------------------

  // data/export 保留最近导出包的数量（多余自动清理）。
  "keep_exports": 3,

  // 导入时每批 upsert 的块数（拥塞小值时减慢导入，较大值占用更多内存）。
  "import_upsert_batch": 500
}
"""


def _log(*args):
    print("[config]", *args, file=sys.stderr)


def _strip_json_comments(text):
    """去掉 // 行注释（保护字符串内的 // 如 URL）。再用宽容解析。

    2026-08-14 修：原实现写成 `prev_c != "\\\\"`——拿 1 字符的 prev_c 去比一个
    2 字符的字符串，恒为真，转义处理是死代码。于是值里的 \\" 被当成字符串结束，
    其后的 // 被当注释整行删掉 → JSON 断裂 → 全部配置静默回退默认值。
    （可达路径：GUI 设置页的 truncate_mark 里打一个双引号即触发。）
    改为在字符串内显式跳过反斜杠转义对。
    """
    out = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(c)
                out.append(text[i + 1])  # 转义对整体透传，不参与引号判定
                i += 2
                continue
            out.append(c)
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text):
    """去掉 JSON 中容错的尾随逗号（,} / ,] / ,\n} / , 空格 }）。"""
    out = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c != ",":
            out.append(c)
            i += 1
            continue
        # 逗号后跳过空白（含换行），若下一个非空白是 } 或 ] 则视为尾逗号删除
        j = i + 1
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j < n and text[j] in "]}":
            i = j  # 丢弃该逗号及其后空白
            continue
        out.append(c)
        i += 1
    return "".join(out)


# 必须为正数的键（0 或负数会让下游逻辑失效，如切块死循环、批次为空）
_POSITIVE_KEYS = frozenset((
    "chunk_char_limit", "short_doc_char_limit", "embed_batch_size", "encode_batch_size",
    "lock_timeout_seconds", "lock_poll_seconds", "heartbeat_interval", "heartbeat_timeout",
    "stall_timeout", "return_chunk_limit", "max_chunks_per_file", "bm25_k1",
    "dense_candidate_factor", "dense_min_candidates", "default_top_k", "keep_exports",
    "import_upsert_batch",
))


def _coerce(key, value, default):
    """按 DEFAULTS 里同名值的类型校验/规整一个配置值。

    不合法则抛 ValueError，由调用方记录并回退默认值——静默接受错误类型会在
    很远的地方炸（如 chunk_char_limit="六百" 要到切块比大小时才 TypeError），
    或者更糟：悄悄改变行为（如 rerank_enabled="false" 是真值，重排照开）。
    """
    if isinstance(default, bool):
        # 注意 bool 必须先于 int 判断（Python 里 bool 是 int 的子类）
        if isinstance(value, bool):
            return value
        raise ValueError(f"应为 true/false，实得 {value!r}")
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"应为整数，实得 {value!r}")
        out = value
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"应为数字，实得 {value!r}")
        out = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"应为字符串，实得 {value!r}")
        return value
    elif isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"应为字符串数组，实得 {value!r}")
        return value
    else:
        return value
    if key in _POSITIVE_KEYS and out <= 0:
        raise ValueError(f"必须为正数，实得 {out!r}")
    return out


def _backfill_missing_keys(text, missing):
    """把 DEFAULTS 里新增、而既有 config.json 尚无的键追加进原文（保留全部注释）。

    load_config 只在文件不存在时写模板，所以老机器上的 config.json 永远不会
    获得新版本新增的配置项——那些键便无法通过"改 config.json 快速调参"这条
    唯一路径被调整（2026-08-14 审计 F2(b)）。此处按需补写，注释与原有内容不动。
    """
    close = text.rfind("}")
    if close == -1:
        return None
    head, tail = text[:close], text[close:]
    prev = head.rstrip()
    sep = "" if prev.endswith(("{", ",")) else ","
    lines = [sep, "\n\n  // ---- 以下为新版本新增配置项，由程序自动补写 ----\n"]
    for k in missing:
        lines.append(f"  {json.dumps(k)}: {json.dumps(DEFAULTS[k], ensure_ascii=False)},\n")
    return head.rstrip("\n") + "".join(lines) + tail


def load_config():
    """加载配置：缺失自动创建；非法值回退默认；新增键自动补写；环境变量覆盖 vault。"""
    cfg = dict(DEFAULTS)
    if not CONFIG_PATH.exists():
        try:
            DATA_DIR.mkdir(exist_ok=True)
            CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            _log(f"未找到配置，已创建带注释默认配置：{CONFIG_PATH}")
        except OSError as e:
            _log(f"创建配置失败（使用默认值）：{e}")
    else:
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            text = _strip_trailing_commas(_strip_json_comments(raw))
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("config.json 顶层必须是对象")
            for k, v in loaded.items():
                if k not in DEFAULTS:
                    _log(f"忽略未知配置项：{k}")
                    continue
                try:
                    cfg[k] = _coerce(k, v, DEFAULTS[k])
                except ValueError as e:
                    _log(f"配置项 {k} 非法（{e}），该项回退默认值 {DEFAULTS[k]!r}")
            missing = [k for k in DEFAULTS if k not in loaded]
            if missing:
                patched = _backfill_missing_keys(raw, missing)
                if patched is not None:
                    try:
                        CONFIG_PATH.write_text(patched, encoding="utf-8")
                        _log(f"已向 config.json 补写新增配置项：{', '.join(missing)}")
                    except OSError as e:
                        _log(f"补写新增配置项失败（不影响本次运行）：{e}")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            _log(f"配置解析失败（使用默认配置）：{e}")
    # 环境变量优先（兼容既有 OBSIDIAN_VAULT 迁移/容器方案）
    if os.environ.get("OBSIDIAN_VAULT"):
        cfg["vault"] = os.environ["OBSIDIAN_VAULT"]
    return cfg


def template_consistency_errors():
    """校验 CONFIG_TEMPLATE（出厂种子）与 DEFAULTS 键值一致。返回问题列表，空 = 一致。

    这两份是同一个"默认值"的两种表述：DEFAULTS 是代码兜底，CONFIG_TEMPLATE 是
    首跑写盘、给人调参用的带注释版本。2026-08-13 只改了前者（chunk 1500→600、
    候选池 10→50），模板没跟上，导致首跑 600、第二跑起 1500，v5 大改被静默回退。
    本函数只约束"出厂种子"，用户此后怎么改 config.json 完全不受限制。
    """
    errors = []
    try:
        parsed = json.loads(_strip_trailing_commas(_strip_json_comments(CONFIG_TEMPLATE)))
    except json.JSONDecodeError as e:
        return [f"CONFIG_TEMPLATE 不是合法 JSON：{e}"]
    for k in DEFAULTS:
        if k not in parsed:
            errors.append(f"模板缺少键：{k}（默认值 {DEFAULTS[k]!r}）")
        elif parsed[k] != DEFAULTS[k]:
            errors.append(f"模板值与 DEFAULTS 不一致：{k} 模板={parsed[k]!r} DEFAULTS={DEFAULTS[k]!r}")
    for k in parsed:
        if k not in DEFAULTS:
            errors.append(f"模板含 DEFAULTS 里没有的键：{k}")
    return errors


CFG = load_config()