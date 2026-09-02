"""store.py — 数据层：读取并解析注册表/进度/meta/锁状态，判定索引三态与心跳四态。

多库版：所有统计按库读取（meta_path(name) / effective_config），
并聚合出"全部库"汇总口径。不加载模型、不碰 Chroma 写入。
"""
import os
import time
from pathlib import Path

from config import CFG
from index import (
    kb_stale,
    load_meta,
    collect_md_files,
    read_progress,
    resolve_note_relations,
    _pid_alive,
)
from library import (
    load_registry,
    effective_config,
    meta_path,
)

HEARTBEAT_TIMEOUT = CFG["heartbeat_timeout"]   # 心跳停止判定（15s）
STALL_TIMEOUT = CFG["stall_timeout"]           # 进度停滞判定（25s）

# 索引状态（三态）：ok / stale / none
# 心跳（四态）：running / dead / stalled / done / idle
STATE_OK, STATE_STALE, STATE_NONE = "ok", "stale", "none"
HB_RUNNING, HB_DEAD, HB_STALLED, HB_DONE, HB_IDLE = "running", "dead", "stalled", "done", "idle"

PROJECT_DIR = Path(__file__).resolve().parent.parent

_ALL = "<全部库>"


def library_entries():
    """注册表条目 → 生效配置列表（含 name/path/collection/excludes/extensions）。"""
    return [effective_config(e) for e in load_registry()]


def meta_stats_for(cfg):
    """单库 meta 统计：已索引文件数、总块数。损坏/缺失返回 (0, 0)。"""
    meta = load_meta(meta_path(cfg["name"]))
    files = sum(1 for v in meta.values() if isinstance(v, dict))
    chunks = sum(v.get("chunks", 0) for v in meta.values() if isinstance(v, dict))
    return files, chunks


def vault_file_count_for(cfg):
    """单库应索引的文件总数（扫描磁盘，与索引同过滤规则）。"""
    try:
        return len(collect_md_files(
            cfg["path"], cfg["exclude_dirs"], cfg["exclude_files"],
            cfg["exclude_patterns"], cfg["extensions"]))
    except Exception:
        return 0


def library_state(cfg):
    """单库三态：ok=索引最新 / stale=有变更待索引 / none=尚未索引。

    判定依据：该库 meta 有数据 + 该库指纹比对（复用现有逻辑，读盘不加载模型）。
    """
    files, chunks = meta_stats_for(cfg)
    if files == 0:
        return STATE_NONE, files, chunks
    try:
        stale, _ = kb_stale(
            cfg["path"],
            meta_file=meta_path(cfg["name"]),
            collection_name=cfg["collection"],
            exclude_dirs=cfg["exclude_dirs"],
            exclude_files=cfg["exclude_files"],
            exclude_patterns=cfg["exclude_patterns"],
            extensions=cfg["extensions"],
        )
    except Exception:
        stale = True
    return (STATE_STALE if stale else STATE_OK), files, chunks


def library_snapshot():
    """全部库快照：聚合状态 + [(name, state, files, chunks, path)]。

    聚合规则：任一库 stale → 聚合 stale；全部 none → none；否则 ok。
    """
    entries = library_entries()
    out = []
    states = []
    for cfg in entries:
        st, files, chunks = library_state(cfg)
        states.append(st)
        out.append((cfg["name"], st, files, chunks, cfg["path"]))
    if not out:
        agg = STATE_NONE
    elif any(s == STATE_STALE for s in states):
        agg = STATE_STALE
    elif all(s == STATE_NONE for s in states):
        agg = STATE_NONE
    else:
        agg = STATE_OK
    return agg, out


def meta_stats():
    """全部库汇总统计（兼容旧名）：已索引文件数、总块数。"""
    files = chunks = 0
    for cfg in library_entries():
        f, c = meta_stats_for(cfg)
        files += f
        chunks += c
    return files, chunks


def vault_file_count():
    """全部库应索引的 .md 文件总数。"""
    try:
        return sum(vault_file_count_for(cfg) for cfg in library_entries())
    except Exception:
        return 0


def index_state():
    """全部库聚合三态（兼容旧名）：ok / stale / none。"""
    agg, _ = library_snapshot()
    files, chunks = meta_stats()
    return agg, files, chunks


def heartbeat_state(progress):
    """心跳四态判定（复用现有双通道规则，与库无关）。

    converting（文档转换）相位豁免停滞告警：单文件转换耗时与页数相关，
    大文件超过 STALL_TIMEOUT 属预期——与 index.progress_text 的口径保持一致
    （双看门狗一致，防一边正常一边弹卡死）。心跳停止仍照常判 dead。

    停滞宽限（问题 32）：进度 dict 带 stall_grace_until（绝对截止时间戳，由
    index._stall_grace 在模型加载/等写锁/写库等合法长静默开始前写入）且未过期
    时，停滞改判 running——色态与呼吸不变。判定表达式与 index._stall_grace_left
    互为镜像，两侧必须同步修改；非法值视为无宽限（fail-closed）。
    ⚠ 升级过渡期：旧版本读不到该字段按原逻辑走（or-0 回退旧行为），任意方向
    新旧混跑都不比引入前糟。DEAD 先于一切豁免——宽限绝不掩盖心跳停止。
    """
    now = time.time()
    if not progress.get("running"):
        return HB_DONE if progress.get("phase") == "done" and progress.get("pid") else HB_IDLE
    updated = progress.get("updated_at") or 0
    advanced = progress.get("last_advance_at") or 0
    if now - updated > HEARTBEAT_TIMEOUT:
        return HB_DEAD
    if progress.get("phase") == "converting":
        return HB_RUNNING
    until = progress.get("stall_grace_until")
    if isinstance(until, (int, float)) and not isinstance(until, bool) \
            and now < until:
        return HB_RUNNING
    if now - advanced > STALL_TIMEOUT:
        return HB_STALLED
    return HB_RUNNING


def heartbeat_note(progress):
    """心跳胶囊文案（纯函数，不碰 flet）：converting → 转换提示；停滞宽限内 →
    合法长静默提示（含已安静秒数）；否则 None（显示默认「心跳正常」）。

    DEAD-first 短路：心跳冻结超 HEARTBEAT_TIMEOUT 时无论宽限是否未过期一律
    返回 None——红 DEAD 胶囊配「宽限内」文案自相矛盾（reliability N1）。
    """
    now = time.time()
    if not progress.get("running"):
        return None
    updated = progress.get("updated_at") or 0
    if now - updated > HEARTBEAT_TIMEOUT:
        return None
    if progress.get("phase") == "converting":
        return "文档转换中（大文件耗时属预期）"
    until = progress.get("stall_grace_until")
    if isinstance(until, (int, float)) and not isinstance(until, bool) \
            and now < until:
        advanced = progress.get("last_advance_at") or 0
        quiet = max(0, int(now - advanced))
        return f"模型加载/写库中（已安静 {quiet}s，宽限内）"
    return None


# 提取失败（xfail 终态）的展示文案：reason → (短标签, 处置指引)
ISSUE_TEXT = {
    "scanned": ("扫描件 PDF",
                "如已在设置中启用 pdf_scan_backend（云端 OCR），下一轮索引会自动重试；"
                "未启用则请到设置中开启，或改用文字层版本"),
    "unreadable": ("不可读", "文件被占用/权限不足，解除后重新索引自动重试"),
    "extract-failed": ("提取失败", "文件可能损坏或加密，修复源文件后重建"),
    "empty": ("空文件", "无正文内容，补全内容后自动入索引"),
    "tbd": ("TBD 占位", "占位符过多暂不索引，补全后自动恢复"),
}


def meta_issues_for(cfg):
    """单库提取失败（xfail 终态）统计：{reason: 文件数}，无问题返回 {}。

    只读该库指纹文件的终态条目，不加载模型、不碰 Chroma。
    """
    try:
        meta = load_meta(meta_path(cfg["name"]))
    except Exception:
        return {}
    issues = {}
    for v in meta.values():
        if isinstance(v, dict) and v.get("xfail"):
            r = v.get("reason") or "unknown"
            issues[r] = issues.get(r, 0) + 1
    return issues


def note_relations_for(cfg, target):
    """给定库配置与笔记标识（相对路径或不含扩展名的标题），返回其双链关系。

    只读 meta 指纹文件，不加载模型、不碰 Chroma。
    """
    try:
        return resolve_note_relations(meta_path(cfg["name"]), target)
    except Exception:
        return {"resolved": False, "file": None, "outlinks": [], "inlinks": []}


def progress_ratio(progress):
    """进度条比例：嵌入阶段按块数，其他阶段按文件数。无数据返回 0。"""
    phase = progress.get("phase")
    if phase == "embedding":
        total, done = progress.get("chunks_total"), progress.get("chunks_done")
    else:
        total, done = progress.get("files_total"), progress.get("files_done")
    if isinstance(total, (int, float)) and total and isinstance(done, (int, float)):
        return min(done / total, 1.0)
    return 0.0


def index_busy():
    """是否已有索引任务在运行（GUI 自身进程或其他进程，含 MCP 触发的后台索引）。"""
    prog = read_progress()
    if prog.get("running"):
        pid = prog.get("pid")
        if pid and _pid_alive(pid):
            return True
    return False


def last_elapsed(progress):
    """最后索引耗时（秒）。"""
    el = progress.get("elapsed_s")
    return el if isinstance(el, (int, float)) else None


def progress_library(progress):
    """当前进度所属库名（进度文件 library 字段，可能缺失）。"""
    return progress.get("library") or ""


def is_library_dir(path):
    """判断路径是否为 Obsidian vault（含 .obsidian 目录），用于选择打开方式。"""
    if not path:
        return False
    try:
        return (Path(path) / ".obsidian").is_dir()
    except OSError:
        return False


def _fmt_ts(ts):
    if not ts:
        return "从未"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
