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
    "chunk_char_limit": 1500,
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
    "bm25_k1": 1.5, "bm25_b": 0.75,
    "fusion_dense_weight": 0.6, "fusion_bm25_weight": 0.4,
    "dense_candidate_factor": 8,
    "dense_min_candidates": 200,
    "rerank_model": "BAAI/bge-reranker-v2-m3",  # 两阶段精排的 cross-encoder 模型
    "rerank_candidates": 10,   # 融合 top N 候选交给重排器精排
    "rerank_enabled": True,    # 重排总开关（False = 纯融合排序）

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
  // 调小：粒度更细、命中更准，但会切出让小片碎块、上下文被截断的素材。
  // 建议值 1200~2000；修改后务必 --full 重建对比效果。
  "chunk_char_limit": 1500,

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

  // 双路融合权重（dense 语义 / BM25 关键词），约等于 1 即两路均衡。
  // 中文概念检索可把 dense 提到 0.7；对编号/专名/精确词查询可加大 bm25。
  "fusion_dense_weight": 0.6,
  "fusion_bm25_weight": 0.4,

  // Dense 候选池 = max(top_k × 候选系数, 最小候选数)。
  // 先取大量候选再做融合与排序，保证小 top_k 时融合质量；越大越慢更准。
  "dense_candidate_factor": 8,
  "dense_min_candidates": 200,

  // 两阶段精排（cross-encoder reranker）：融合取 top rerank_candidates 后，
  // 用 BAAI/bge-reranker-v2-m3 对 (query, 块) 逐对精排再取 top_k。
  // 解决"相关块与无关块融合分太接近导致排序不稳"（实测 top3 命中 3/5 → 5/5）。
  // rerank_candidates 建议 8~15（越大越慢）；网络首次加载需下载模型 ~1.1GB。
  // rerank_enabled=false 时回到纯融合排序（模型加载失败也会自动降级）。
  "rerank_model": "BAAI/bge-reranker-v2-m3",
  "rerank_candidates": 10,
  "rerank_enabled": true,

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
    """去掉 // 行注释（保护字符串内的 // 如 URL）。再用宽容解析。"""
    out = []
    i, n, in_str, prev_c = 0, len(text), False, ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '"' and prev_c != "\\\\":
                in_str = False
            prev_c = c
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            prev_c = c
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        prev_c = c
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


def load_config():
    """加载配置：缺失自动创建；非法回退默认；环境变量覆盖 vault。返回 dict。"""
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
            text = CONFIG_PATH.read_text(encoding="utf-8")
            text = _strip_json_comments(text)
            text = _strip_trailing_commas(text)
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("config.json 顶层必须是对象")
            for k, v in loaded.items():
                if k in DEFAULTS:
                    cfg[k] = v
                else:
                    _log(f"忽略未知配置项：{k}")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            _log(f"配置解析失败（使用默认配置）：{e}")
    # 环境变量优先（兼容既有 OBSIDIAN_VAULT 迁移/容器方案）
    if os.environ.get("OBSIDIAN_VAULT"):
        cfg["vault"] = os.environ["OBSIDIAN_VAULT"]
    return cfg


CFG = load_config()