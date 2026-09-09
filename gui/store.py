"""store.py — 数据层：读取并解析注册表/进度/meta/锁状态，判定索引三态与心跳四态。

多库版：所有统计按库读取（meta_path(name) / effective_config），
并聚合出"全部库"汇总口径。不加载模型、不碰 Chroma 写入。
"""
import os
import time
from pathlib import Path

from config import CFG
from extractors import current_backend_sig
from index import (
    _backend_changed,
    kb_stale,
    load_meta,
    collect_md_files,
    read_progress,
    resolve_note_relations,
    _pid_alive,
)
from wemm_indexer import load_wemm_meta, wemm_meta_path as wemm_meta_file
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
            cfg["exclude_patterns"], cfg["extensions"],
            selection=(cfg.get("selection_in"), cfg.get("selection_out")),
            selection_default=cfg.get("selection_default", "follow")))
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
            selection=(cfg.get("selection_in"), cfg.get("selection_out")),
            selection_default=cfg.get("selection_default", "follow"),
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


def file_index_rows_for(cfg):
    """单库逐文件「未正常入索引」明细（零侵入：只读 meta 指纹文件）。

    返回 {"total": 正常索引文件数, "rows": [(rel, reason, will_retry), ...]}，
    rows 按 rel 排序，只列落了终态（提取失败/扫描件/空/不可读/TBD）的文件。
    will_retry 复用 index._backend_changed 同一谓词——签名不符 = 下轮真会
    自动重试，不给用户与实际行为相反的提示（问题39 同款纪律）。
    """
    try:
        meta = load_meta(meta_path(cfg["name"]))
    except Exception:
        return {"total": 0, "rows": []}
    sig = current_backend_sig()
    rows = []
    total = 0
    for rel, info in meta.items():
        if not isinstance(info, dict):
            continue
        reason = info.get("reason")
        if reason:
            rows.append((rel, reason, bool(_backend_changed(info, sig))))
        elif info.get("xfail") or info.get("tbd"):
            rows.append((rel, "unknown", False))
        else:
            total += 1
    rows.sort(key=lambda r: r[0])
    return {"total": total, "rows": rows}


def wemm_status_for(cfg):
    """单库 WEMM 页索引逐 PDF 状态（零侵入：只读 wemm_meta_<库>.json）。

    返回 {"exists": 是否有 PDF 页数据, "total_pages": 页向量总数,
          "rows": [(rel, pages|None, failed, reason)]}（按 rel 排序）；
    failed 行 pages=None、reason 为人话原因。
    只列 PDF：WEMM 页级导航仅对 PDF 有意义（wemm_indexer 也只收 .pdf），
    meta 里的历史 md 残留（旧版勾选穿透白名单混入）一律不显示。
    meta 无 PDF 条目 = 还没建页索引。
    """
    try:
        meta = load_wemm_meta(wemm_meta_file(cfg["name"]))
    except Exception:
        return {"exists": False, "total_pages": 0, "rows": []}
    rows = []
    total = 0
    for rel, info in meta.items():
        if rel == "_version" or not isinstance(info, dict):
            continue
        if info.get("tbd") or info.get("xfail"):
            rows.append((rel, None, True, info.get("reason") or "渲染失败"))
        else:
            pages = int(info.get("pages", 0))
            total += pages
            rows.append((rel, pages, False, ""))
    rows.sort(key=lambda r: r[0])
    # meta 文件不存在时 load_wemm_meta 返回空 dict（不抛异常）：无条目 = 还没建页索引
    return {"exists": bool(rows), "total_pages": total, "rows": rows}


def wemm_backend_state():
    """WEMM 开关与地址（现读 config，不用进程启动时的快照）。"""
    from config import reload_config
    reload_config()
    return CFG.get("wemm_backend", "off"), CFG.get("wemm_url") or ""


def wemm_service_probe(url):
    """探测本机 WEMM 看图服务存活（127.0.0.1 回环短超时；GUI 须放后台线程调用）。

    返回 (alive, detail)；detail 为人话。只读探测，绝不启动服务、不加载模型。
    """
    try:
        from wemm_retriever import health
        h = health(url)
        if h.get("loaded"):
            return True, "模型已进显存（%s · %s）" % (h.get("model", "?"), h.get("device", "?"))
        return True, "服务存活，待首次请求时自动加载模型"
    except Exception as e:
        return False, ("未启动或不可达（%s）——下次页级导航会按需自动拉起，"
                       "也可手动运行 python wemm_server.py" % type(e).__name__)


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


def index_busy(progress=None):
    """是否已有索引任务在运行（GUI 自身进程或其他进程，含 MCP 触发的后台索引）。

    progress：调用方已读的同一份快照（问题47 附记）。快照内"忙不忙"和
    "跑没跑"若来自两次读盘，正好跨一次状态翻转就会拼出 busy=True +
    running=False 的撕裂帧——前端会翻译成"有别的 MCP 在跑"。5 秒跑完的
    增量任务几乎必中这一帧（全量跑几分钟反而看不见）。传同一份则无此问题。
    """
    prog = progress if progress is not None else read_progress()
    if prog.get("running"):
        pid = prog.get("pid")
        if pid and _pid_alive(pid):
            return True
    return False


def index_task_owner(progress, own_pid=None):
    """索引任务归属（问题47 附记）：'ours' 本GUI拉起 / 'starting' 本GUI刚拉起
    （启动 imports 数秒，进度文件还没动静）/ 'foreign' 其他进程（MCP/CLI/
    另一GUI）在跑 / 'idle' 无任务。

    progress 必须是同一快照（与 index_busy 同一防撕裂纪律）；own_pid 是本
    GUI 拉起的子进程 pid（没拉起传 None，此时 running 任务一律判 foreign）。
    只比对 pid，不杀不碰任何进程。
    """
    alive = bool(progress.get("running")) and bool(_pid_alive(progress.get("pid")))
    if alive:
        if own_pid and progress.get("pid") == own_pid:
            return "ours"
        return "foreign"
    return "starting" if own_pid else "idle"


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


_GPU_CACHE = (0.0, None)  # (timestamp, result)：nvidia-smi 约 0.2s/次，5s 缓存


def gpu_stats(ttl=5.0):
    """整卡只读状态（问题47）：{ok, mem_used_mb, mem_total_mb, util_pct, power_w}。

    数据源 nvidia-smi（普通用户可执行，只读计数器，无需提权；无 N 卡/
    驱动缺失/解析失败一律 {ok: False}，调用方显示"—"即可，fail-open）。
    注意 WDDM 下拆不到"哪个进程占多少"——只报整卡总数，不承诺归属。
    进程内 5s 缓存：两套 GUI 都是 1s 轮询，直调也不会每秒起进程。
    """
    global _GPU_CACHE
    now = time.time()
    if _GPU_CACHE[1] is not None and now - _GPU_CACHE[0] < ttl:
        return _GPU_CACHE[1]
    bad = {"ok": False}
    try:
        import subprocess
        # Windows + pythonw（无控制台）：必须 CREATE_NO_WINDOW，否则每次调用
        # 都闪一个 CMD 窗口（gui/worker.py 同款教训）。Linux 无此标志位。
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=8, **kwargs)
    except Exception:
        return bad
    try:
        parts = out.stdout.decode("utf-8", "replace").strip().split(",")
        if len(parts) < 4:
            _GPU_CACHE = (now, bad)
            return bad
        used, total, util, power = (p.strip() for p in parts[:4])
        good = {"ok": True,
                "mem_used_mb": float(used), "mem_total_mb": float(total),
                "util_pct": float(util),
                "power_w": None if power in ("[N/A]", "N/A", "") else float(power)}
        _GPU_CACHE = (now, good)
        return good
    except Exception:
        _GPU_CACHE = (now, bad)
        return bad


_cpu_last = None  # (timestamp, idle_ticks, total_ticks)：进程内采样缓存


def cpu_percent(min_interval=1.0):
    """本机 CPU 总占用百分比（问题47）：ctypes 读 GetSystemTimes 差值。

    无需提权、无第三方依赖（psutil 遵 requirements 注释不引入）。
    两次采样才有差值：首次调用（或间隔不足）返回 None，调用方显示"…"
    （两套 GUI 都是 1s 轮询，第二次起即有数）。失败一律 None，fail-open。
    """
    global _cpu_last
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _T(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        def _to_int(t):
            return (t.high << 32) + t.low

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        idle, kernel, user = _T(), _T(), _T()
        if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                 ctypes.byref(user)):
            return None
        now = time.time()
        idle_i, total_i = _to_int(idle), _to_int(kernel) + _to_int(user)
        prev = _cpu_last
        _cpu_last = (now, idle_i, total_i)
        if prev is None or now - prev[0] < min_interval:
            return None
        d_idle, d_total = idle_i - prev[1], total_i - prev[2]
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, (d_total - d_idle) * 100.0 / d_total))
    except Exception:
        return None
