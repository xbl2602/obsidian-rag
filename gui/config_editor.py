"""config_editor.py — 设置页数据层：读 config.json、校验类型、保留注释写回。

config.json 是带 // 注释与尾随逗号的宽松 JSON（见 config.py）。直接 json.dump
会抹掉全部注释（含配置说明文档），因此写回采用"按行定位键名、只替换该键的值
文本"策略：字段注释与顺序原样保留，仅变更值行。
"""
import json
import re
from pathlib import Path

from config import CONFIG_PATH, DEFAULTS, load_config

# 字段分组（设置页按组展示）。ref: 改动后需要 --full 重建的标志
GROUPS = [
    ("知识库与索引范围（改动需全量重建）", [
        ("vault", "str"), ("exclude_dirs", "list"), ("exclude_files", "list"),
        ("exclude_patterns", "list"), ("model_name", "str"),
        ("collection_name", "str"),
    ]),
    ("切块粒度（改动需全量重建）", [
        ("chunk_char_limit", "int"), ("short_doc_char_limit", "int"),
    ]),
    ("嵌入与硬件", [
        ("embed_batch_size", "int"), ("encode_batch_size", "int"),
        ("cuda_cooldown_seconds", "int"),
    ]),
    ("锁与并发", [
        ("lock_timeout_seconds", "int"), ("lock_poll_seconds", "float"),
    ]),
    ("心跳健康判定", [
        ("heartbeat_interval", "float"), ("heartbeat_timeout", "float"),
        ("stall_timeout", "float"),
    ]),
    ("检索与格式化（实时生效）", [
        ("return_chunk_limit", "int"), ("max_chunks_per_file", "int"),
        ("truncate_mark", "str"), ("bm25_k1", "float"), ("bm25_b", "float"),
        ("fusion_dense_weight", "float"), ("fusion_bm25_weight", "float"),
        ("dense_candidate_factor", "int"), ("dense_min_candidates", "int"),
        ("rerank_model", "str"), ("rerank_candidates", "int"),
        ("rerank_enabled", "bool"), ("small_to_big", "bool"),
    ]),
    ("HyDE 查询增强（需本地 LLM，默认关）", [
        ("hyde_enabled", "bool"), ("hyde_llm_url", "str"),
        ("hyde_llm_model", "str"), ("hyde_min_confidence", "float"),
    ]),
    ("工具默认值", [
        ("default_top_k", "int"),
    ]),
    ("导出 / 导入", [
        ("keep_exports", "int"), ("import_upsert_batch", "int"),
    ]),
]

ALL_KEYS = [k for _, fields in GROUPS for k, _ in fields]

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
    for _, fields in GROUPS:
        for k, kind in fields:
            if k == key:
                return kind
    return None