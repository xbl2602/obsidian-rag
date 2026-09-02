"""config_editor.py — 设置页数据层：读 config.json、校验类型、保留注释写回。

config.json 是带 // 注释与尾随逗号的宽松 JSON（见 config.py）。直接 json.dump
会抹掉全部注释（含配置说明文档），因此写回采用"按行定位键名、只替换该键的值
文本"策略：字段注释与顺序原样保留，仅变更值行。
"""
import json
import re
from pathlib import Path

from config import CONFIG_PATH, DEFAULTS, load_config

# ---------------------------------------------------------------------------
# 设置页结构（2026-08-26 问题31 重构）
#
# GROUPS：左侧导航的分组清单。level="basic" 归入「常用」，"advanced" 归入
# 「开发者」；常用组必须排在前面（UI 按顺序渲染两个小节）。
#
# FIELD_META：每个键的展示元数据——label 中文友好名、hint 一句话说明（源自
# CONFIG_TEMPLATE 注释的浓缩版）、rebuild=改动需 --full 全量重建、choices=
# 封闭枚举（GUI 渲染成下拉而非手输魔法字符串）、suggest=推荐候选芯片
# （开放值仍可手输，芯片只是降低"不知道有什么模型可选"的门槛）、secret=
# 密码框遮显。新增配置项时三处同步：DEFAULTS / CONFIG_TEMPLATE / 这里的
# 分组+元数据（tests/test_config_editor.py 有静态断言兜底）。
# ---------------------------------------------------------------------------
GROUPS = [
    {"title": "知识库（全局默认）", "level": "basic", "icon": "FOLDER_OUTLINED",
     "desc": "本项目是多库架构：真正的库列表与每库配置在工具栏「📚 库管理」"
             "（data/libraries.json）。以下两项是单库时代的全局默认，"
             "改动不影响任何已注册库。",
     "fields": [("vault", "str"), ("collection_name", "str")]},
    {"title": "模型", "level": "basic", "icon": "MODEL_TRAINING_OUTLINED",
     "desc": "本机在用两个模型：bge-m3（嵌入）+ bge-reranker-v2-m3（精排）。"
             "候选芯片只是推荐清单，点选后首次使用才会从 HuggingFace 自动下载。",
     "fields": [("model_name", "str"), ("rerank_enabled", "bool"),
                ("rerank_model", "str")]},
    {"title": "PDF 与云端 OCR", "level": "basic", "icon": "PICTURE_AS_PDF_OUTLINED",
     "desc": "扫描件 OCR 与文字层 PDF 的提取后端；切到 mineru-cloud 会上传原始文件。",
     "fields": [("pdf_scan_backend", "str"), ("pdf_text_backend", "str"),
                ("mineru_api_key", "str"), ("mineru_timeout_seconds", "int")]},
    {"title": "检索输出", "level": "basic", "icon": "SEARCH_OUTLINED",
     "desc": "返回内容的形状与低置信护栏，全部实时生效。",
     "fields": [("return_chunk_limit", "int"), ("max_chunks_per_file", "int"),
                ("truncate_mark", "str"), ("default_top_k", "int"),
                ("default_libraries", "list"),
                ("confidence_warn_threshold", "float"),
                ("confidence_drop_threshold", "float")]},

    {"title": "融合与排序调优", "level": "advanced", "icon": "TUNE_OUTLINED",
     "desc": "BM25 / RRF 权重 / 候选池 / 重排预算。拿不准就保持默认。",
     "fields": [("bm25_k1", "float"), ("bm25_b", "float"),
                ("fusion_dense_weight", "float"), ("fusion_bm25_weight", "float"),
                ("dense_candidate_factor", "int"), ("dense_min_candidates", "int"),
                ("rerank_candidates", "int")]},
    {"title": "切块粒度", "level": "advanced", "icon": "CONTENT_CUT_OUTLINED",
     "desc": "全局默认值，可在「库管理 → 库配置」按库覆盖；改后需 --full 重建。",
     "fields": [("chunk_char_limit", "int"), ("short_doc_char_limit", "int"),
                ("small_to_big", "bool")]},
    {"title": "排除规则", "level": "advanced", "icon": "RULE_OUTLINED",
     "desc": "全局默认的排除名单：未单独配置的库都继承这里，"
             "单个库可在「库管理 → 库配置」覆盖（那里留空 = 用这里的默认）；"
             "改后需 --full 重建。",
     "fields": [("exclude_dirs", "list"), ("exclude_files", "list"),
                ("exclude_patterns", "list"), ("tbd_exclude_ratio", "float")]},
    {"title": "HyDE 查询增强", "level": "advanced", "icon": "PSYCHOLOGY_OUTLINED",
     "desc": "提问用词与笔记差太远导致检索落空时，先让本地 LLM 按问题写一段"
             "「假设答案」，拿它去检索（术语更接近笔记原文）。默认关；触发才多花一跳。",
     "fields": [("hyde_enabled", "bool"), ("hyde_llm_url", "str"),
                ("hyde_llm_model", "str"), ("hyde_min_confidence", "float")]},
    {"title": "性能与硬件", "level": "advanced", "icon": "SPEED_OUTLINED",
     "desc": "批次大小与 CUDA 冷却；运行时会自动按显存收紧。",
     "fields": [("embed_batch_size", "int"), ("encode_batch_size", "int"),
                ("cuda_cooldown_seconds", "int")]},
    {"title": "锁与心跳", "level": "advanced", "icon": "MONITOR_HEART_OUTLINED",
     "desc": "多进程写保护与卡死判定阈值，单机单进程场景无需调整。",
     "fields": [("lock_timeout_seconds", "int"), ("lock_poll_seconds", "float"),
                ("heartbeat_interval", "float"), ("heartbeat_timeout", "float"),
                ("stall_timeout", "float")]},
    {"title": "导出 / 导入", "level": "advanced", "icon": "SWAP_HORIZ_OUTLINED",
     "desc": "导出包保留策略与导入批量。",
     "fields": [("keep_exports", "int"), ("import_upsert_batch", "int")]},
]

ALL_KEYS = [k for g in GROUPS for k, _ in g["fields"]]

FIELD_META = {
    # ---- 知识库（全局默认；多库架构下真正的库在 data/libraries.json 注册表）----
    "vault": {"label": "知识库路径（全局默认）", "rebuild": True,
              "hint": "仅作首库自动迁移源与未注册场景兜底；已注册库的路径"
                      "请用工具栏「📚 库管理」，改这里不影响任何已注册库"},
    "collection_name": {"label": "向量库名（全局默认）", "rebuild": True,
                        "hint": "每库 collection 由库名派生或按库覆盖（库管理）；"
                                "本键只作用于旧单库路径与首库迁移"},
    # ---- 模型 ----
    "model_name": {"label": "嵌入模型", "rebuild": True,
                   "hint": "HuggingFace 模型标识；候选芯片仅为推荐（点选后首次使用"
                           "自动下载，非本机已装）；换模型 = 换向量空间",
                   "suggest": [
                       ("BAAI/bge-m3", "推荐 · 中英多语"),
                       ("BAAI/bge-large-zh-v1.5", "中文 · 效果优先"),
                       ("BAAI/bge-small-zh-v1.5", "中文 · 低配轻量"),
                       ("sentence-transformers/all-MiniLM-L6-v2", "英文 · 轻量"),
                   ]},
    "rerank_enabled": {"label": "两阶段重排开关",
                       "hint": "检索两步走：向量+关键词融合先粗筛候选，重排模型再对"
                               "「查询-块」逐对精排取 top_k；关闭 = 只用粗筛排序"},
    "rerank_model": {"label": "重排模型",
                     "hint": "做第二步精排的 cross-encoder；本机在用的是默认这个，"
                             "换别的首次使用下载约 1.1GB",
                     "suggest": [
                         ("BAAI/bge-reranker-v2-m3", "默认 · 多语"),
                         ("BAAI/bge-reranker-base", "轻量快速"),
                         ("BAAI/bge-reranker-large", "效果更强更慢"),
                     ]},
    # ---- PDF 与云端 OCR ----
    "pdf_scan_backend": {"label": "扫描件 OCR 后端",
                         "choices": [
                             ("none", "不做 OCR，扫描件跳过（默认）"),
                             ("mineru-cloud", "MinerU 云端 OCR（上传原始文件）"),
                         ],
                         "hint": "无文字层 PDF 的处理方式；切换后下轮索引自动重试存量扫描件"},
    "pdf_text_backend": {"label": "文字层 PDF 后端",
                         "choices": [
                             ("local", "本地直提（默认，免费快速）"),
                             ("mineru-cloud", "MinerU 结构识别（上传原始文件）"),
                         ],
                         "hint": "有文字层 PDF 的提取方式；云端版面/表格识别更准（is_ocr=False 不重复计费）"},
    "mineru_api_key": {"label": "MinerU API Key", "secret": True,
                       "hint": "mineru.net → 个人中心 → API Token；敏感信息，不进任何日志"},
    "mineru_timeout_seconds": {"label": "MinerU 超时（秒）",
                               "hint": "单个扫描件「提交+轮询+下载」的总时间预算"},
    # ---- 检索输出 ----
    "return_chunk_limit": {"label": "单块返回字符上限",
                           "hint": "超出截断并附标记；直接影响回答注入的 token 量"},
    "max_chunks_per_file": {"label": "同文件最多块数",
                            "hint": "防单文件霸屏 top_k；想看更多可临时调到 3–5"},
    "truncate_mark": {"label": "截断标记",
                      "hint": "块被截断时附在末尾的提示文案"},
    "default_top_k": {"label": "默认返回条数",
                      "hint": "search_knowledge 未显式传参时的 top_k"},
    "default_libraries": {"label": "默认检索库",
                          "hint": "逗号分隔库名（须与注册表一致）；留空 = 全部注册库"},
    "confidence_warn_threshold": {"label": "低置信标注阈值",
                                  "hint": "命中置信度低于此值 → 来源标注「仅供参考」（0~1）"},
    "confidence_drop_threshold": {"label": "低置信丢弃阈值",
                                  "hint": "低于此值直接不输出该来源，宁缺毋滥（0~1）"},
    # ---- 融合与排序调优 ----
    "bm25_k1": {"label": "BM25 k1", "hint": "词频饱和度，标准值 1.5 一般不动"},
    "bm25_b": {"label": "BM25 b", "hint": "长度归一强度，标准值 0.75 一般不动"},
    "fusion_dense_weight": {"label": "语义权重（dense）",
                            "hint": "调大偏语义检索；1.0/1.0 即经典等权 RRF"},
    "fusion_bm25_weight": {"label": "关键词权重（bm25）",
                           "hint": "调大偏关键词/专名检索；两项均实时生效"},
    "dense_candidate_factor": {"label": "候选池系数",
                               "hint": "候选池 = top_k × 此系数，越大越准越慢"},
    "dense_min_candidates": {"label": "候选池下限",
                             "hint": "候选池保底数量，保证小 top_k 时融合质量"},
    "rerank_candidates": {"label": "重排候选数",
                          "hint": "送重排的融合候选数，建议 30–80；越大越慢"},
    # ---- 切块粒度（全局默认，可按库覆盖）----
    "chunk_char_limit": {"label": "单块最大字符", "rebuild": True,
                         "hint": "全局默认，可按库覆盖；建议 400–800"
                                 "（小块检索 + 父节回填路线）"},
    "short_doc_char_limit": {"label": "整篇收录阈值", "rebuild": True,
                             "hint": "全局默认，可按库覆盖；正文短于此字符数的笔记"
                                     "不切块、整篇一块"},
    "small_to_big": {"label": "父节回填（small-to-big）",
                     "hint": "命中小块时回填父节全文补偿上下文；与小块切块配套，"
                             "若改回 1200+ 大块应关掉"},
    # ---- 排除规则（全局默认，可按库覆盖）----
    "exclude_dirs": {"label": "排除目录", "rebuild": True,
                     "hint": "全局默认，可按库覆盖；目录树任一层同名目录整棵跳过，逗号分隔"},
    "exclude_files": {"label": "排除文件名", "rebuild": True,
                      "hint": "全局默认，可按库覆盖；精确文件名匹配"
                              "（任何层级同名都不索引），逗号分隔"},
    "exclude_patterns": {"label": "排除文件前缀", "rebuild": True,
                         "hint": "全局默认，可按库覆盖；文件名以任一前缀开头即跳过，逗号分隔"},
    "tbd_exclude_ratio": {"label": "TBD 占位过滤", "rebuild": True,
                          "hint": "[TBD] 行占比 ≥ 此值的半成品文件跳过索引；0 = 关闭；"
                                  "仅全局生效（不可按库覆盖）"},
    # ---- HyDE ----
    "hyde_enabled": {"label": "启用 HyDE",
                     "hint": "开启后才可能触发；触发时多跑一轮检索 + 一次本地 LLM 调用"
                             "（不触发零开销）；需 LM Studio 类服务在运行"},
    "hyde_llm_url": {"label": "HyDE 服务地址",
                     "hint": "OpenAI 兼容接口（如 LM Studio 默认 localhost:1234）"},
    "hyde_llm_model": {"label": "HyDE 模型名",
                       "hint": "填本地服务里已加载的模型名（如 qwen2.5-3b-instruct）"},
    "hyde_min_confidence": {"label": "HyDE 触发阈值",
                            "hint": "首轮 top1 置信度低于此值才触发（0~1）；调大更爱触发"},
    # ---- 性能与硬件 ----
    "embed_batch_size": {"label": "索引嵌入批次",
                         "hint": "显存紧张调小；运行时自动收紧，此处为上限"},
    "encode_batch_size": {"label": "查询编码批次",
                          "hint": "单次编码显存占用，同样自动按显存收紧"},
    "cuda_cooldown_seconds": {"label": "CUDA 冷却秒数",
                              "hint": "GPU 失败（OOM 等）后的冷却期，避免反复崩"},
    # ---- 锁与心跳 ----
    "lock_timeout_seconds": {"label": "写锁等待上限（秒）",
                             "hint": "超时报「锁繁忙」明确错误；经常多进程并发才需调大"},
    "lock_poll_seconds": {"label": "锁轮询间隔（秒）",
                          "hint": "获锁失败后的重试间隔"},
    "heartbeat_interval": {"label": "心跳间隔（秒）",
                           "hint": "索引进度写盘周期；以下两个判定按其倍数联动"},
    "heartbeat_timeout": {"label": "心跳停止判定（秒）",
                          "hint": "超过即判「疑似卡死」，一般保持 3× 心跳间隔"},
    "stall_timeout": {"label": "进度停滞判定（秒）",
                      "hint": "心跳正常但进度不动判卡死，一般保持 5× 心跳间隔；"
                              "特定阶段（转换/模型加载/写库）有内置宽限"},
    # ---- 导出 / 导入 ----
    "keep_exports": {"label": "保留导出包数",
                     "hint": "data/export 自动清理多余的旧导出包"},
    "import_upsert_batch": {"label": "导入批量",
                            "hint": "每批 upsert 的块数；内存紧张调小"},
}

# 主设置页不展示尖括号反斜杠的 vault，避免误改路径类长文本
TEXT_EDITABLE = {"vault", "model_name", "collection_name", "truncate_mark",
                 "hyde_llm_url", "hyde_llm_model"}


def missing_keys():
    """DEFAULTS 里有、而设置页没有暴露的配置项（应为空）。

    2026-08-14（审计 F12）：2026-08-13 新增的 small_to_big / hyde_* 五个键
    只改了 DEFAULTS，既没进 CONFIG_TEMPLATE 也没进这里，于是既不能在
    config.json 里看到、也不能在 GUI 里调——"快速调参"对它们完全失效。
    tests/test_config_editor.py 据此做回归。
    """
    return [k for k in DEFAULTS if k not in ALL_KEYS]


def _split_field(text):
    """抽象 JSON 中 `"key" : value,` -> (key, value_text)。带 // 注释行跳过。"""
    m = re.match(r'\s*"([A-Za-z0-9_]+)"\s*:\s*(.*)$', text)
    if not m:
        return None, None
    key, val = m.group(1), m.group(2)
    while val.endswith((",", " ")):
        val = val.rstrip()
        if val.endswith(","):
            val = val[:-1].rstrip()
    return key, val


def load_raw():
    """读取 config.json 原文（缺失时返回 config 模板文本）。"""
    try:
        return CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def save_value(key, value):
    """写回单个字段（保留注释与格式），返回 True 成功 / False 键不存在。"""
    raw = load_raw()
    if not raw:
        return False
    new = _replace_value(raw, key, value)
    if new is raw:
        return False
    try:
        CONFIG_PATH.write_text(new, encoding="utf-8")
    except OSError:
        return False
    return True


def _value_to_json(value, kind):
    """按字段类型把 UI 字符串转成合法 JSON 值文本；校验失败抛 ValueError。"""
    if kind == "str":
        s = str(value)
        return json.dumps(s, ensure_ascii=False)
    if kind == "int":
        n = int(str(value).strip())
        return str(n)
    if kind == "float":
        f = float(str(value).strip())
        return str(f)
    if kind == "list":
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        if not parts:
            return "[]"
        return "[" + ", ".join(json.dumps(p, ensure_ascii=False) for p in parts) + "]"
    if kind == "bool":
        return "true" if str(value).strip().lower() in ("1", "true", "yes", "on") else "false"
    raise ValueError("未知类型: %s" % kind)


def _replace_value(raw, key, new_text):
    """在 raw 中定位 "key": 的赋值行，替换其值文本（保留行首缩进与行内注释）。

    若同一 key 出现在注释里（如模板注释示例），只替换真正赋值的那行。
    匹配规则：行内容以 `"key" :` 开头（允许前导空白）且不是 // 注释。
    """
    if key not in ALL_KEYS:
        return raw
    lines = raw.splitlines(keepends=True)
    pat = re.compile(r'^\s*"' + re.escape(key) + r'"\s*:')
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        if pat.match(line):
            indent = line[: len(line) - len(line.lstrip())]
            rest = line[len(indent):]
            m = re.match(r'"[^"]*"\s*:\s*[^,]*[,\n]?', rest)
            if not m:
                continue
            newline = indent + '"%s": %s' % (key, new_text)
            if rest.rstrip().endswith(","):
                newline += ","
            if not line.endswith("\n"):
                newline = newline.rstrip("\n")
            elif not newline.endswith("\n"):
                newline += "\n"
            lines[i] = newline
            changed = True
            break
    else:
        return raw
    return "".join(lines) if changed else raw


def apply_updates(updates):
    """批量写回 {key: (kind, value_str)}；任一失败即整体中止并回滚。返回错误 dict。"""
    raw = load_raw()
    if not raw:
        return {"__file__": "无法读取 config.json"}
    text = raw
    errors = {}
    for key, (kind, value) in updates.items():
        if kind == "vault_note":
            continue
        try:
            new_text = _value_to_json(value, kind)
        except ValueError as e:
            errors[key] = "格式错误：%s" % e
            continue
        replaced = _replace_value(text, key, new_text)
        if replaced is text:
            errors[key] = "config.json 中没有该字段"
        else:
            text = replaced
    if errors:
        return errors
    try:
        CONFIG_PATH.write_text(text, encoding="utf-8")
    except OSError as e:
        return {"__file__": "写入失败：%s" % e}
    # 让运行中的 GUI 进程读到新值
    reload_cfg()
    return {}


def reload_cfg():
    """重新加载 config 到模块级 CFG（供运行中的 GUI 使用）。"""
    import config as cm
    fresh = load_config()
    cm.CFG.clear()
    cm.CFG.update(fresh)


def kind_of(key):
    for g in GROUPS:
        for k, kind in g["fields"]:
            if k == key:
                return kind
    return None