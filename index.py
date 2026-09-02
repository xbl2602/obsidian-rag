"""index.py — 扫描 Obsidian Vault，按标题切块，嵌入，存入 Chroma。"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import chromadb

from config import CFG
from extractors import BINARY_EXTS, TEXT_EXTS, current_backend_sig, extract_to_markdown
from library import effective_config, load_registry, meta_path, resolve_entries

# 跨平台文件锁：Windows 用 msvcrt（字节范围锁），Linux/macOS 用 fcntl（flock）。
# 按平台函数内局部导入：Linux 上 import index 不触碰 msvcrt，反之亦然。
_IS_WINDOWS = os.name == "nt"

# 环境变量 OBSIDIAN_VAULT 优先于 config.json 的 vault（config.py 已并入该优先逻辑）
VAULT = CFG["vault"]
DATA_DIR = Path(__file__).parent / "data"
CHROMA_DIR = DATA_DIR / "chroma"
INDEX_META = DATA_DIR / "index_meta.json"
LOCK_FILE = DATA_DIR / "index.lock"
PROGRESS_FILE = DATA_DIR / "index_progress.json"
MODEL_NAME = CFG["model_name"]
COLLECTION_NAME = CFG["collection_name"]
EXCLUDE_DIRS = set(CFG["exclude_dirs"])
# 结构类文件：纯链接清单/指令文件，非知识本体，排除以免污染检索
STRUCTURE_FILES = set(CFG["exclude_files"])
# AI 会话/临时文件模式
EXCLUDE_PATTERNS = tuple(CFG["exclude_patterns"])

LOCK_TIMEOUT_SECONDS = CFG["lock_timeout_seconds"]
LOCK_POLL_SECONDS = CFG["lock_poll_seconds"]
EMBED_BATCH_SIZE = CFG["embed_batch_size"]

# 切块/清洗逻辑版本：升级后旧索引需重嵌（指纹感知不到代码升级），
# meta 版本不匹配时 index_vault 自动按全量重建处理。
META_VERSION = 9  # v9: 多格式提取（docx/pdf）+ 统一终态机制 + 原始字节指纹
                  # （v8: TBD 占位重文件跳过索引；v7: 空正文文件跳过不产生块）

# _load_text 读取失败时的指纹哨兵：锁定/OneDrive/AV 占用等 OSError 场景。
# 两轮哨兵等值 = 判稳（不反复触发重建）；文件恢复可读后真实 hash ≠ 哨兵 → 自动重试。
_UNREADABLE = "unreadable"

# [TBD]/[tbd]/TBD —/TODO — 等占位标记行识别（排除"半成品报告"用的判定）
TBD_RE = re.compile(r"\[TBD|TBD\s*[—-]|\[todo\]|TODO\s*[—-]", re.IGNORECASE)


def is_tbd_heavy(content, ratio):
    """正文中占位标记行（TBD 等）占比 ≥ ratio 判为"占位重文件"，应跳过索引。

    ratio <= 0 表示关闭此过滤。空白行不参与统计（与切块的"非空行"口径一致）。
    """
    if not ratio or ratio <= 0:
        return False
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return False
    tbd = sum(1 for l in lines if TBD_RE.search(l))
    return tbd / len(lines) >= ratio


def _load_text(fpath):
    """统一读取入口（终态机制 A 的全部语义所在）。返回 (text, bhash)。

    - bhash：文件「原始字节」的 MD5。对合法 UTF-8 文本与旧版
      「解码后再编码」的内容指纹完全等值（decode→encode 往返无损），
      既有 meta 条目无需迁移；二进制源（pdf/docx）也用它做指纹，
      kb_stale 据此实现「真没变就绝不跑转换」的零成本快速比对。
      读取失败（锁定/权限/AV 占用等 OSError）→ 哨兵 _UNREADABLE。
    - text：文本类后缀（TEXT_EXTS）→ 解码文本（errors=replace），
      空/纯空白归一为 None；二进制后缀 → 一律 None——内容必须走
      extractors 提取管线，此处绝不转换（GUI 每秒轮询的零成本边界）；
      读取失败 → None。

    后缀判定一律 suffix.lower()（堵 .PDF/.MD 大写扩展名被静默误路由）。
    本函数绝不抛异常。
    """
    try:
        raw = fpath.read_bytes()
    except OSError:
        return None, _UNREADABLE
    bhash = hashlib.md5(raw).hexdigest()
    suffix = fpath.suffix.lower().lstrip(".")
    if suffix in TEXT_EXTS:
        text = raw.decode("utf-8", errors="replace")
        return (text if text.strip() else None), bhash
    return None, bhash


def _skipped(info):
    """单点谓词：该 meta 条目是否为「不产块的持久化终态」（TBD 重 / xfail 提取失败类）。

    收拢 kb_stale/_index_core 里所有「排除出应索引集合」的判断，
    防止新增终态类型时漏改某一处导致每轮误判 stale。
    """
    return isinstance(info, dict) and bool(info.get("tbd") or info.get("xfail"))


# 终态 reason 字面量的单一事实来源：kb_stale（判是否已收敛）与 _index_core
# （落盘终态）必须共用同一组常量表达同一个 reason，否则两侧字符串静默失配
# → kb_stale 永远判"未收敛"、每轮误报 stale、每轮无谓重建。
# ⚠ 值本身是持久化数据格式（写进 meta 的 reason 字段），绝不可改动。
REASON_UNREADABLE = "unreadable"
REASON_EXTRACT_FAILED = "extract-failed"
REASON_EMPTY = "empty"
REASON_TBD = "tbd"
REASON_SCANNED = "scanned"


def _terminal_entry(st, bhash, reason, xsrc=None):
    """构造持久化终态条目（不产块，但必须留在 meta 里防重建死循环）。

    reason ∈ {REASON_UNREADABLE, REASON_EXTRACT_FAILED, REASON_EMPTY,
    REASON_TBD, REASON_SCANNED}。
    xsrc：产生该终态时的 OCR 能力签名（仅二进制提取类失败携带）——
    签名变化（用户启用后端/补配 Key）时条目自动获得重试资格。
    chunks=0 使其不贡献任何有效块 id：该文件若曾有旧块，会在写库阶段
    被精确清理逻辑删除。调用方必须同时 current_rels.add(rel) 让条目持久化。
    """
    e = {"hash": bhash, "chunks": 0, "size": st.st_size,
         "mtime": st.st_mtime_ns, "tbd": False,
         "xfail": True, "reason": reason}
    if xsrc:
        e["xsrc"] = xsrc
    return e


def _backend_changed(entry, xsig):
    """终态条目产生时的 OCR 能力签名与当前不符 = 该文件值得重试转正。

    典型场景：用户把 pdf_scan_backend 从 none 切到 mineru-cloud、或补配了
    mineru_api_key——已跳过的扫描件在下一轮索引自动重试，无需 --full。
    """
    return bool(entry.get("xfail")
                and entry.get("reason") in (REASON_SCANNED, REASON_EXTRACT_FAILED)
                and entry.get("xsrc") != xsig)


def _entry_converged(entry, reason):
    """既有终态条目是否已经反映了这个 reason——是则本轮已收敛，kb_stale 不必再计一次
    changed。与 _terminal_entry 写入的 reason 必须用同一组 REASON_* 常量表达，
    防止 kb_stale 与 _index_core 两侧的判定字符串静默失配、每轮误判 stale。
    """
    return bool(entry and entry.get("xfail") and entry.get("reason") == reason)


def _links_missing(entry):
    """已有的正常（非终态）条目缺少 links 字段——功能上线前建立的旧索引需要一次性补齐。

    与 _backend_changed 是同一种"惰性触发重试"机制：不强制 --full 全量重建，
    下一轮增量索引里这个文件会自然穿透快速路径重新处理一次，之后恢复正常跳过。
    只对非终态（非 xfail/tbd）条目生效——终态条目没有成功解析出的正文可供抽取链接，
    它们有自己的 _backend_changed/_entry_converged 机制负责重试，不需要这个额外触发。
    """
    return bool(entry) and not _skipped(entry) and "links" not in entry


class LockBusyError(RuntimeError):
    """写锁被其他存活进程持有且等待超时。"""


def log(*args):
    """所有进度信息输出到 stderr，避免污染 MCP stdio 协议。"""
    print(*args, file=sys.stderr)


# ---------- 进度报告（data/index_progress.json，AI/人可随时读取） ----------
# 双通道心跳（2026-08-07 v3 设计）：
#   1. 独立心跳线程每 HEARTBEAT_INTERVAL 秒强制写盘一次（updated_at 刷新）——
#      与硬件性能无关，间隔恒定；覆盖模型加载期与批次内耗时；
#   2. 事件更新：批次完成/阶段切换时立即写盘（进度数字推进）。
# 双重判定（index_status 输出依据，固定阈值、简单明确）：
#   - 心跳停止：距上次心跳 > HEARTBEAT_TIMEOUT（3 间隔）→ 疑似卡死
#   - 进度停滞：心跳在走，但进度（块数/文件数）> STALL_TIMEOUT（5 间隔）未推进
#     → 疑似批次内卡死（假活）

_progress = {}
_progress_lock = threading.Lock()
HEARTBEAT_INTERVAL = CFG["heartbeat_interval"]   # 心跳线程写盘间隔（秒）
HEARTBEAT_TIMEOUT = CFG["heartbeat_timeout"]     # 心跳停止判定（3 × interval）
STALL_TIMEOUT = CFG["stall_timeout"]             # 进度停滞判定（5 × interval）

# 停滞判定的合法长静默宽限（问题 32）：模型加载 / 等写锁 / 写库清理这类
# 「心跳正常但进度必然长时间不推进」的阶段，在进入前写入一段自过期宽限
# （进度 JSON 键 stall_grace_until = 绝对截止时间戳），停滞看门狗在宽限内
# 不判 stalled。硬编码不进 config：宽限一旦可配置就成了"永久静默开关"。
# 升级矩阵与阈值漂移声明（方案 §7.1）：若用户把 lock_timeout_seconds 调到
# > STALL_GRACE_WRITE(180) 或模型冷加载实际超过 STALL_GRACE_MODEL_LOAD(300)，
# 对应窗口超出宽限 → 行为回退为现状（照旧误报停滞），不会比引入本机制前更糟；
# 任意方向的新旧版本混跑同理（旧代码读不到字段按原逻辑走）。
STALL_GRACE_MAX_S = 600.0        # 单次宽限时长 clamp 上限
STALL_GRACE_MODEL_LOAD = 300.0   # 模型冷加载 / CUDA 切换重载（fp16→fp32 双次加载同盖）
STALL_GRACE_WRITE = 180.0        # 写锁排队 + 写库清理（> 默认 lock_timeout 60s 全覆盖）

_heartbeat_thread = None
_heartbeat_stop = threading.Event()


def _pid_alive(pid):
    """进程是否存活。

    ⚠️ 不能用 os.kill(pid, 0)：Windows 上 CPython 的 os.kill 对除 CTRL_C_EVENT /
    CTRL_BREAK_EVENT 以外的任何 sig 都执行 OpenProcess + TerminateProcess，
    sig=0 也不例外——那是"杀掉目标进程"，不是"探测存活"。
    本项目有三处依赖它（单例守卫、锁持有者判定、GUI 每秒轮询索引进程），
    在 Windows 上会分别导致：杀掉正在服务的 server、在 Chroma 写一半时杀掉
    持锁进程、GUI 把自己刚拉起的索引子进程杀掉（2026-08-14 审计 F5）。

    Windows 走 OpenProcess(SYNCHRONIZE) + WaitForSingleObject(0)：
    已退出的进程句柄是 signaled 态，未退出则 WAIT_TIMEOUT。纯只读，无副作用。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        ERROR_ACCESS_DENIED = 5
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                # 拿不到句柄：ACCESS_DENIED 说明进程确实存在（只是无权限），
                # 其余（典型 ERROR_INVALID_PARAMETER=87）说明该 PID 不存在。
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
            finally:
                k32.CloseHandle(h)
        except Exception as e:  # ctypes 不可用等极端情况：宁可报"存活"，不误杀
            log(f"进程存活探测失败（视为存活）：{e}")
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在，只是不属于当前用户
    except OSError:
        return False


def read_progress():
    """读取进度文件（失败返回 {}）。"""
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_progress_file(base):
    """原子写进度文件（在 _progress_lock 内调用）。"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, PROGRESS_FILE)
    except Exception as e:
        log(f"写进度文件失败（忽略）：{e}")


def update_progress(**fields):
    """事件更新（批次完成/阶段切换时调用）：推进进度并立即写盘。

    事件更新即"进度推进"——刷新 last_advance_at（停滞判定依据）。
    心跳线程不经过本函数，因此不会掩盖停滞。

    停滞宽限字段（问题 32）的一次性豁免语义：
    - 写入方传 stall_grace_s（相对秒数，**永不落盘**）；进度 JSON 里只存
      绝对截止时间戳 stall_grace_until；
    - 每次调用默认移除既有 stall_grace_until——宽限只覆盖「本次调用之后
      那一段合法静默窗」，任何普通进度事件都会终止它（心跳整表拷贝除外，
      宽限在静默窗口内存活靠心跳原样保留该字段）；
    - 带 stall_grace_s 且为正数值时才重新写回：
      until = max(旧值, now + min(grace_s, STALL_GRACE_MAX_S))——
      max 合并防「后写者反向缩短已有宽限」（回退攻击），clamp 防永久静默；
      非数值 / ≤0 一律不写（fail-closed）。
    """
    global _progress
    now = time.time()
    grace_s = fields.pop("stall_grace_s", None)
    with _progress_lock:
        base = dict(_progress)
        base.update(fields)
        base.setdefault("pid", os.getpid())
        base.setdefault("started_at", now)
        base["updated_at"] = now
        base["last_advance_at"] = now
        # 宽限：先默认清除，再按需 max 合并重写（语义见 docstring）
        prev_grace = base.pop("stall_grace_until", None)
        if (isinstance(grace_s, (int, float))
                and not isinstance(grace_s, bool) and grace_s > 0):
            until = now + min(float(grace_s), STALL_GRACE_MAX_S)
            if isinstance(prev_grace, (int, float)):
                until = max(until, float(prev_grace))
            base["stall_grace_until"] = until
        elapsed = now - base["started_at"]
        base["elapsed_s"] = round(elapsed, 1)
        done = base.get("chunks_done") or 0
        total = base.get("chunks_total")
        if base.get("running") and done and total:
            speed = done / elapsed if elapsed > 0 else 0
            base["eta_s"] = round((total - done) / speed, 1) if speed > 0 else None
        else:
            base["eta_s"] = None
        _progress = base
        _write_progress_file(base)


def _heartbeat_tick():
    """心跳线程体：定时强制写盘（刷新 updated_at 但不动 last_advance_at）。

    仅刷新心跳时间戳、耗时与 ETA；进度数字保持内存中的最新值。
    模型加载期、单批嵌入期间心跳照常走——心跳停止即为真异常。
    """
    with _progress_lock:
        base = dict(_progress)
        if not base.get("running"):
            return
        now = time.time()
        base["updated_at"] = now
        base["elapsed_s"] = round(now - base.get("started_at", now), 1)
        done = base.get("chunks_done") or 0
        total = base.get("chunks_total")
        if done and total:
            elapsed = base["elapsed_s"]
            speed = done / elapsed if elapsed > 0 else 0
            base["eta_s"] = round((total - done) / speed, 1) if speed > 0 else None
        else:
            base["eta_s"] = None
        _write_progress_file(base)


def _heartbeat_loop():
    """心跳循环：每 HEARTBEAT_INTERVAL 秒写盘一次，直到任务结束。"""
    while not _heartbeat_stop.wait(HEARTBEAT_INTERVAL):
        try:
            _heartbeat_tick()
        except Exception as e:
            log(f"心跳写盘失败（忽略）：{e}")


def progress_start(phase, files_total, message="", library=""):
    """索引开始：重置进度，置 running，启动心跳线程。"""
    global _heartbeat_thread, _heartbeat_stop
    _heartbeat_stop = threading.Event()
    # 显式清空上一任务的宽限残留（与 update_progress 的默认 pop 双保险）：
    # server 长驻进程背靠背索引多库时，上一库 writing 宽限若不清会泄漏进
    # 下一任务，抑制其早期的停滞判定。注意不能在持锁状态下调 update_progress
    # （其内部也要拿同一把非重入锁 → 死锁），所以先独立 pop，再走正常更新路径。
    with _progress_lock:
        _progress.pop("stall_grace_until", None)
    update_progress(running=True, phase=phase, message=message, library=library,
                    files_total=files_total, files_done=0,
                    chunks_total=None, chunks_done=0, error=None,
                    last_advance_at=time.time())
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True,
                                         name="progress-heartbeat")
    _heartbeat_thread.start()


def progress_finish(phase, message):
    """索引结束：保留最终统计，置 running=False，停心跳线程。"""
    global _heartbeat_thread
    update_progress(running=False, phase=phase, message=message)
    _heartbeat_stop.set()


def progress_error(error):
    """索引失败：置 error 态，停心跳线程。"""
    global _heartbeat_thread
    update_progress(running=False, phase="error",
                    message="索引失败", error=str(error)[:500])
    _heartbeat_stop.set()


def progress_text(p):
    """把进度 dict 格式化为 AI 可读文本（供 index_status 工具使用）。

    双重判定（固定阈值，与硬件性能无关——心跳由独立线程每 5s 恒定刷新）：
    - 心跳停止：距上次心跳 > HEARTBEAT_TIMEOUT → 疑似卡死；
    - 进度停滞：心跳在走但进度 > STALL_TIMEOUT 未推进 → 疑似批次内卡死。
    """
    if not p:
        return "（无进度记录：索引从未运行或进度文件缺失）"
    now = time.time()
    lines = []
    if p.get("running"):
        lines.append("▶ 索引运行中")
    elif p.get("phase") == "done":
        lines.append("✔ 索引完成")
    elif p.get("phase") == "error":
        lines.append("✘ 索引失败")
    else:
        lines.append("· 索引空闲（无进行中任务）")
    lines.append(f"  阶段: {p.get('phase', '?')}")
    if p.get("library"):
        lines.append(f"  库: {p.get('library')}")
    lines.append(f"  消息: {p.get('message', '')}")
    if p.get("device"):
        dev = p.get("device")
        mark = "✅" if dev == "cuda" else "⚠ 慢速模式"
        lines.append(f"  设备: {dev} {mark}")
    if p.get("error"):
        lines.append(f"  错误: {p.get('error')}")
    if p.get("files_total") is not None:
        lines.append(f"  文件: {p.get('files_done', 0)}/{p.get('files_total')}")
    if p.get("chunks_total"):
        lines.append(f"  块: {p.get('chunks_done', 0)}/{p.get('chunks_total')}")
    if p.get("elapsed_s") is not None:
        lines.append(f"  耗时: {p['elapsed_s']}s")
    if p.get("eta_s") is not None:
        lines.append(f"  预计剩余: {p['eta_s']}s")
    if p.get("running"):
        last = p.get("updated_at") or 0
        gap = now - last
        advance = p.get("last_advance_at") or last
        stall = now - advance
        # 分支顺序即判定优先级（与 gui/store.heartbeat_state 镜像，勿重排）：
        # 心跳停止(DEAD) 先于一切豁免 → converting 白名单 → 停滞宽限 → stalled。
        if gap > HEARTBEAT_TIMEOUT:
            tip = ""
            if (p.get("elapsed_s") or 0) < 300 and (p.get("chunks_done") or 0) == 0:
                tip = "（任务早期：首次加载 embedding 模型可能耗时 1-2 分钟，若进程 CPU 仍活跃属正常）"
            lines.append(f"  ⚠ 疑似卡死：心跳已停 {int(gap)}s（> {int(HEARTBEAT_TIMEOUT)}s）{tip}")
            lines.append(f"  建议检查 PID {p.get('pid')} 是否存活；确认卡死可结束该进程后重试。")
        elif p.get("phase") == "converting":
            # 文档转换（pdf/docx→md）相位豁免：单文件转换耗时与页数相关，
            # 大文件超过 STALL_TIMEOUT 属预期，不判"批次内卡死"（白名单无条件
            # 生效、不依赖宽限字段——旧版本读新文件同样豁免，升级过渡期安全）
            lines.append(f"  · 文档转换中（进度 {int(stall)}s 未推进，属大文件转换预期，"
                         f"心跳 {int(gap)}s 前正常）")
        elif _stall_grace_left(p, now) is not None:
            # 停滞宽限内（模型加载/等写锁/写库等合法长静默）：信息行而非告警，
            # 不含任何告警字样；宽限绝不掩盖心跳停止（上面 DEAD 分支优先）
            left = int(_stall_grace_left(p, now) or 0)
            pid_txt = p.get("pid") if p.get("pid") is not None else "?"
            lines.append(f"  · 模型加载/写库中（合法长静默，宽限内）：已安静 {int(stall)}s，"
                         f"宽限剩余 {left}s（PID {pid_txt}），心跳 {int(gap)}s 前正常")
        elif stall > STALL_TIMEOUT:
            lines.append(f"  ⚠ 进度停滞：心跳正常（{int(gap)}s 前）但进度已 {int(stall)}s 未推进"
                         f"（> {int(STALL_TIMEOUT)}s），疑似批次内卡死（假活）")
            lines.append(f"  建议检查 PID {p.get('pid')} 是否仍在消耗 CPU；确认卡死可结束该进程后重试。")
        else:
            lines.append(f"  心跳: {int(gap)}s 前（正常） · 进度推进: {int(stall)}s 前")
    return "\n".join(lines)


_model = None
_device = None  # 当前模型所在设备："cuda" / "cpu" / None
DEVICE_STATE_FILE = DATA_DIR / "device_state.json"
CUDA_COOLDOWN_SECONDS = CFG["cuda_cooldown_seconds"]  # CUDA 失败后冷却期，到期轻量探测自动重试
_cuda_cooldown_until = 0.0   # 进程内冷却截止时间戳


def _write_device_state(state):
    """原子写设备状态（同 save_meta 策略，仅作诊断记录，不做门禁）。"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        tmp = DEVICE_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DEVICE_STATE_FILE)
    except Exception as e:
        log(f"写入设备状态失败（忽略）：{e}")


def _report_device(device, note=""):
    """上报当前设备：写入 device_state.json（清旧失败标记）+ 索引进度里记录 device。

    在每次模型成功就位后调用：成功用 CUDA 会覆盖掉历史上残留的 failed 记录，
    避免陈旧失败标记（如旧版 _argkmin 拦截）误导后续排查。
    """
    global _progress
    _write_device_state({
        "device": device,
        "healthy": device == "cuda",
        "checked_at": round(time.time(), 1),
        "note": note,
    })
    if device == "cpu":
        log(f"⚠ 当前在 CPU 模式编码（{note}）——速度约为 CUDA 的 1/12。")
    with _progress_lock:
        base = dict(_progress)
        if base.get("running"):
            base["device"] = device
            _write_progress_file(base)
            _progress = base


def _stall_grace(seconds, **fields):
    """给当前索引进度写入一段停滞宽限（两段式快照守卫，复刻 _report_device 先例）。

    两段式原因（防不可重入死锁）：update_progress 内部要拿 _progress_lock，
    本函数若在锁内直接调它必然自死锁——所以先锁内取内存快照做守卫判断，
    锁外再调 update_progress(stall_grace_s=..., **fields)。

    守卫两条：
    - 内存快照无运行中任务不写：无任务时写宽限纯属噪音进度记录；
    - pid 不是本进程不写：进度文件可能是强杀残留（running=True 冻结），
      本进程绝不能"接手复活"它——否则 server._index_running 会把死任务当
      活任务，新索引被多挡一整个宽限期。守卫只读内存（_progress 从不从
      文件水合），残留文件天然不满足条件。
    """
    with _progress_lock:
        snap = dict(_progress)  # 段1：锁内仅取快照
    if not snap.get("running"):
        return
    if snap.get("pid") != os.getpid():
        return
    # 段2：锁外调用（结构断言由 audit 用例钉死：调用点不得在 with 块内）
    update_progress(stall_grace_s=seconds, **fields)


def _stall_grace_left(p, now=None):
    """判定侧唯一入口（index 侧）：宽限是否有效 → 剩余秒数；无效返回 None。

    有效当且仅当：任务 running、字段为数值、now < 截止时间戳。非法值
    （字符串/None/负数残留）一律视为无宽限（fail-closed）。
    ⚠ gui/store.py 心跳判定里有一份逐字镜像的同一表达式——双看门狗口径
    必须同步修改，否则一边显示「宽限内」另一边报停滞（镜像样本用例压住）。
    """
    if not p.get("running"):
        return None
    until = p.get("stall_grace_until")
    now = now if now is not None else time.time()
    if isinstance(until, (int, float)) and not isinstance(until, bool) and now < until:
        return until - now
    return None


def _cuda_probe():
    """轻量探测 GPU 可用性：分配 64MB 显存成功即视为可用（毫秒级，失败返回 False）。

    探测只分配小显存，自身不会 OOM 崩溃；模型加载若仍失败由 encode_safe 兜底。
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        t = torch.empty(64 * 1024 * 1024, dtype=torch.uint8, device="cuda")
        del t
        return True
    except Exception:
        return False


def _cuda_ready():
    """冷却期内直接用 CPU；到期后轻量探测，可用才尝试 CUDA。

    无硬性窗口：显存恢复后最多等一个冷却期（5 分钟）自动切回。
    """
    global _cuda_cooldown_until
    if time.time() < _cuda_cooldown_until:
        return False
    if not _cuda_probe():
        _cuda_cooldown_until = time.time() + CUDA_COOLDOWN_SECONDS
        return False
    return True


def _cooldown_cuda(reason):
    """CUDA 失败：进入 5 分钟冷却 + 诊断落盘（到期自动探测重试，非硬性窗口）。"""
    global _cuda_cooldown_until
    _cuda_cooldown_until = time.time() + CUDA_COOLDOWN_SECONDS
    _write_device_state({"device": "cuda", "failed_at": time.time(), "reason": reason[:200]})
    log(f"CUDA 失败，进入 {CUDA_COOLDOWN_SECONDS}s 冷却（到期自动探测重试）：{reason}")


def _build_model(device, fp16=False):
    """构建模型实例；import 在函数内（延迟加载，import 本身可能因内存不足失败）。"""
    from sentence_transformers import SentenceTransformer
    if fp16:
        return SentenceTransformer(MODEL_NAME, device=device,
                                   model_kwargs={"torch_dtype": "float16"})
    return SentenceTransformer(MODEL_NAME, device=device)


def _param_dtype_mixed(model):
    """fp16 加载后参数 dtype 是否混搭（Half 与 Float 并存）。

    实测 bge-m3 以 torch_dtype=float16 加载时 391 个参数全部为 fp16——
    混搭即异常状态，推理时激活值与权重 dtype 不匹配必报
    "mat1 and mat2 must have the same dtype, but got Half and Float"。
    防御性检查：混搭则回退 fp32 整体重建，避免病态模型继续跑。
    """
    seen = set()
    for p in model.parameters():
        seen.add(p.dtype)
        if len(seen) > 1:
            return True
    return False


def _load_model(device):
    """按 device 加载模型。CUDA/CPU 均优先 fp16 减半显存（8GB 卡防共享显存溢出），失败回退 fp32。

    2026-08-15：fp16 加载成功后再做参数 dtype 一致性检查（见 _param_dtype_mixed），
    混搭视为加载失败回退 fp32——防御长驻进程里偶发的半精度病态（检索报 dtype 错）。
    """
    try:
        m = _build_model(device, fp16=True)
        if _param_dtype_mixed(m):
            log(f"{device} fp16 加载后参数 dtype 混搭（Half/Float 并存），回退 fp32")
            raise RuntimeError("fp16 参数 dtype 混搭")
        return m, device
    except Exception as e:
        log(f"{device} fp16 加载失败，回退 fp32：{e}")
    return _build_model(device), device


def _try_switch_back_cuda():
    """CPU 模型中且探测到显存恢复：释放 CPU 模型 → 加载 CUDA；失败回滚并冷却。"""
    global _model, _device
    # 埋点②（问题 32）：CUDA 切回是完整的一段模型冷加载（fp16→fp32 回退时是
    # 两次串行加载），期间进度完全安静。宽限必须在进入加载前就位，且要盖住
    # 最坏路径——加载失败回滚 CPU 再恢复的全程都在这段宽限内，故放函数入口、
    # 先于 old=_model 与任何 _load_model 调用，而非加载语句旁边。失败回滚后
    # 由埋点③（fallback_to_cpu 收尾）另起一段新宽限接续。
    _stall_grace(STALL_GRACE_MODEL_LOAD,
                 message="显存恢复，尝试自动切回 CUDA...")
    old = _model
    try:
        _model = None  # 先释放 CPU 模型，避免新旧模型双份内存峰值
        new, _ = _load_model("cuda")
        _model, _device = new, "cuda"
        log("显存已恢复，自动切回 CUDA")
        _report_device("cuda", note="auto-switched-back")
        old = None  # 引用释放收尾（B1）：不用 del——except 回滚分支要靠 old
                    # 恢复 _model，收尾代码自己抛异常（log 管道断裂/
                    # _report_device 失败）绝不能让回滚名字解绑定
                    # （同红线1）。置于全部可抛操作之后，此后无失败路径。
    except Exception as e:
        _model, _device = old, "cpu"  # 回滚，继续用 CPU
        _cooldown_cuda(str(e))
        log(f"切回 CUDA 失败，保持 CPU：{e}")


def get_model():
    """懒加载 embedding 模型。CUDA 失败自动降级 CPU（含 import torch 失败）。

    显存恢复后自动切回：CPU 模型缓存期间每次调用先做毫秒级 GPU 探测，
    通过即切换——无硬性等待窗口，最多一个冷却期粒度。
    """
    global _model, _device
    if _model is not None:
        if _device == "cpu" and _cuda_ready():
            _try_switch_back_cuda()
        return _model
    if _cuda_ready():
        try:
            log("加载 embedding 模型（device=cuda）...")
            # 埋点①cuda（问题 32）：冷加载全程在宽限内。必须在缓存未命中的
            # 实际加载分支里、_load_model 调用之前写——放函数入口会随每次
            # get_model 调用（每批一次）反复刷新宽限，等于把停滞看门狗永久静音。
            _stall_grace(STALL_GRACE_MODEL_LOAD)
            _model, _device = _load_model("cuda")
            _report_device("cuda")
            return _model
        except Exception as e:
            log(f"CUDA 初始化失败（{e}），降级 CPU")
            _cooldown_cuda(str(e))
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
    log("加载 embedding 模型（device=cpu）...")
    # 埋点①cpu（问题 32）：同 cuda 分支——只在真实加载前写一次，禁止入口化。
    _stall_grace(STALL_GRACE_MODEL_LOAD)
    _model, _device = _load_model("cpu")
    _report_device("cpu", note="cuda-init-failed")
    return _model


def _is_memory_error(e):
    """是否内存/显存不足类错误（WinError 1455 页面文件不足、CUDA OOM、MemoryError）。"""
    text = str(e).lower()
    return isinstance(e, MemoryError) or any(k in text for k in (
        "out of memory", "cuda oom", "paging file", "1455", "commitment limit"))


# 模型不可重入：后台索引线程与检索（搜索时编码查询）可能并发，必须串行编码
_encode_lock = threading.Lock()

# Windows WDDM 显存溢出会静默排入共享显存（系统内存）而不抛错，只能靠耗时识别病态
SLOW_BATCH_SECONDS = 30.0  # CUDA 单批编码超过此值视为疑似共享显存溢出
_slow_batch_count = 0
_last_batch_cap = None  # 上次自动批次收紧值（仅变化时打日志，避免刷屏）


def _release_cuda_cache():
    """释放 CUDA 缓存分配器池（编码后调用，防高水位残留与共享显存溢出）。"""
    if _device == "cuda":
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def _auto_batch_size(desired):
    """按当前可用显存自动收紧批次（防共享显存溢出），配置值仅作上限。

    2026-08-13 校准教训：不要用固定线性公式猜批次。实测 Qwen3-Embedding-0.6B
    在长块（~700 字符）下 attention 显存随 batch×seq² 暴涨：
      bs=8→4.5GB  bs=16→5.9GB  bs=32→7.7GB（8GB 卡满载 → WDDM 溢出排入
      系统 RAM，页面文件被吃满、GPU 空转低功耗——即"100%但30W"病态）。
    bge-m3（纯 encoder）同批次仅需一半显存，公式曾对两者一刀切。
    改为：固定安全上限（8）+ 可用显存二次收紧。慢批看门狗仍兜底。
    """
    global _last_batch_cap
    if _device != "cuda":
        return desired
    import torch
    free_gb = torch.cuda.mem_get_info()[0] / 1024 ** 3
    cap = min(8, desired)  # Qwen 长块 bs=16 已近 6GB，固定 8 保安全
    if free_gb < 4.5:
        cap = min(cap, 4)  # 显存紧张再降
    if desired > cap:
        if cap != _last_batch_cap:
            log(f"可用显存 {free_gb:.1f}GB，批次 {desired} 收紧为 {cap}")
        _last_batch_cap = cap
        return cap
    _last_batch_cap = None
    return desired


def _vstack(arrays):
    """拼接分批嵌入结果（sentence_transformers 默认 numpy；个别配置返回 torch 张量）。"""
    import numpy as np
    if arrays[0].__class__.__module__.startswith("torch"):
        import torch
        return torch.cat(arrays, dim=0)
    return np.vstack(arrays)


def _encode(texts, batch_size):
    """执行一次编码；CUDA 内存不足（OOM/页面文件不足）→ 降级 CPU 重试一次。"""
    try:
        return get_model().encode(texts, normalize_embeddings=True,
                                  batch_size=batch_size, show_progress_bar=False)
    except (RuntimeError, OSError, MemoryError) as e:
        if _is_memory_error(e) and _device == "cuda":
            log(f"CUDA 编码内存不足（{e}），自动降级 CPU 重试...")
            fallback_to_cpu(str(e))
            return get_model().encode(texts, normalize_embeddings=True,
                                      batch_size=batch_size, show_progress_bar=False)
        raise


def encode_safe(texts, batch_size=None):
    """带 CUDA→CPU 自动降级的编码入口（索引与检索共用）。

    线程锁串行化编码（模型不可重入）；CUDA 内存不足（OOM/页面文件不足）
    → 标记失败、卸载模型、切 CPU 重试一次；仍失败（CPU 内存也不足）才抛出。
    Windows WDDM 显存溢出会静默排入共享显存而不报错，只能靠耗时识别病态：
    单批 >SLOW_BATCH_SECONDS → 告警；连续两批仍慢 → 主动降级 CPU。
    编码结束统一 empty_cache，防止分配器高水位残留（空闲占用 7.7GB 问题）。
    """
    with _encode_lock:
        if batch_size is None:
            batch_size = _auto_batch_size(CFG["encode_batch_size"])
        if _device == "cuda":
            global _slow_batch_count
            t0 = time.time()
            try:
                emb = _encode(texts, batch_size)
            finally:
                _release_cuda_cache()
            elapsed = time.time() - t0
            if elapsed > SLOW_BATCH_SECONDS:
                _slow_batch_count += 1
                log(f"CUDA 单批编码耗时 {elapsed:.1f}s（>{SLOW_BATCH_SECONDS:.0f}s），"
                    f"疑似共享显存溢出（连续 {_slow_batch_count} 次）")
                if _slow_batch_count >= 2:
                    log("连续慢批，主动降级 CPU，避免病态运行...")
                    fallback_to_cpu("slow-batch")
            else:
                _slow_batch_count = 0
            return emb
        return _encode(texts, batch_size)


def fallback_to_cpu(reason=""):
    """卸载当前模型并切 CPU（同进程内生效，无需重启）。"""
    global _model, _device
    _cooldown_cuda(reason or "未知原因")
    try:
        import torch
        if _model is not None:
            _model = None
            torch.cuda.empty_cache()
    except Exception as e:
        log(f"卸载 CUDA 模型失败（忽略）：{e}")
    _device = None
    log("已切换到 CPU 模式")
    # 埋点③（问题 32，降级收尾标记）：随后的批次重试会经 get_model 缓存未命中
    # 分支重新加载模型（埋点①cpu 以 max 合并续写宽限），但重载由调用方静默
    # 发生（见 _encode），若调用方是检索路径或索引已收尾则没有后续埋点——这里
    # 收尾补写一段，盖住「卸载 + 冷加载」整段安静。显式用 STALL_GRACE_MODEL_LOAD：
    # 降级后的首次重载与冷启动同量级。
    _stall_grace(STALL_GRACE_MODEL_LOAD,
                 message="CUDA 不可用，已切换 CPU 模式（正在重新加载模型）")


def split_by_headings(text):
    """按 Markdown 标题切块，每个 H1/H2/H3 起新块，维护嵌套标题路径。

    返回 [(heading_path, content)]；heading_path 形如
    "FLUENT 配置与求解设置 / 1. 湍流模型与壁面处理（SST k‑ω）"，
    子标题不脱离父标题（H3 块携带 H1/H2 祖先）。文件开头无标题部分
    heading_path 为空串。标题行本身不进正文（与历史行为一致）。

    2026-08-14：跳过 ``` / ~~~ 围栏代码块内的 # 行。此前 Python/Shell 注释
    会被当成标题，不只是多切一节——它会成为其后所有真实小节的"父标题"，
    而标题路径是要拼进嵌入文本的，等于把代码注释混进了正文向量（审计 F8）。
    """
    lines = text.splitlines()
    chunks = []
    path = []  # [(level, text)] 标题栈
    current_lines = []
    heading_re = re.compile(r"^(#{1,3})\s+(.+)$")
    fence_re = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
    fence_char = ""      # 当前围栏的字符（` 或 ~），空 = 不在围栏内
    fence_len = 0        # 开栏标记长度；闭栏须同字符且不短于它

    def flush():
        if current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                chunks.append((" / ".join(t for _, t in path), body))

    for line in lines:
        fm = fence_re.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = "", 0
            current_lines.append(line)
            continue
        if fence_char:
            current_lines.append(line)  # 围栏内一律当正文
            continue
        m = heading_re.match(line)
        if m:
            flush()
            level = len(m.group(1))
            htext = m.group(2).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, htext))
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return chunks


def split_paragraphs(text):
    """按空行切段落，去首尾空白。返回段落列表。"""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(text, max_len=None):
    """按句子边界切块，永不从句子中间剪断。

    边界：中文标点（。！？）后；英文 .!? 后必须紧跟空格 + 大写字母或数字
    （避免 Mr./e.g./3.14 等缩写/小数被误切）。常见缩写先保护再切。
    单句本身超过 max_len 时宁长勿断（整句保留），避免语义截断。
    max_len 默认取 config 的 chunk_char_limit。
    """
    if max_len is None:
        max_len = CFG["chunk_char_limit"]
    ABBR = {"mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.", "etc.", "vs.", "st.", "no.", "al."}
    for a in list(ABBR) + [a.capitalize() for a in ABBR]:
        text = text.replace(" " + a, " " + a.replace(".", "\x00"))
    parts = re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+(?=[A-Z0-9])", text)
    parts = [p.replace("\x00", ".") for p in parts]
    chunks = []
    cur = ""
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if cur and len(cur) + len(s) > max_len:
            chunks.append(cur)
            cur = ""
        cur += s
        cur += " "  # 还原被 \s+ 消费的分隔空格，避免 "dollars.This" 粘连
    if cur:
        chunks.append(cur.strip())
    return chunks


_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")


def is_list_block(text):
    """段落整体是否为列表体：非空行中列表项行（- * + 或 1. 等）占 ≥ 一半。

    用于"列表整体成块 / 按项边界切"，避免列表被段落切拆散或句子切裁断。
    单行列表项也算列表体（列表项常因空行被拆成单行段落，需跨段落合并）。
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    items = sum(1 for l in lines if _LIST_ITEM_RE.match(l))
    return items >= 1 and items * 2 >= len(lines)


def split_list_block(text, max_len=None):
    """按列表项边界切分列表块：永不从列表项中间剪断。

    新块只在列表项行处开启（当前块超出 max_len 时），非列表行（正文/空行）
    跟随当前项；末尾余块保留。max_len 默认 chunk_char_limit。
    """
    if max_len is None:
        max_len = CFG["chunk_char_limit"]
    chunks = []
    cur = []
    for line in text.splitlines():
        if not line.strip():
            cur.append(line)
            continue
        if _LIST_ITEM_RE.match(line) and cur and len("\n".join(cur)) + len(line) > max_len:
            chunks.append("\n".join(cur).strip())
            cur = []
        cur.append(line)
    if cur:
        chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c]


def _clean_scalar(s):
    return s.strip().strip('"').strip("'").strip()


def extract_frontmatter(text):
    """提取 frontmatter 元数据，返回 dict 和去掉 frontmatter 的正文。

    支持三种写法（值一律规整成逗号分隔的扁平字符串）：
        title: 火箭发动机笔记        → "火箭发动机笔记"
        aliases: [发动机, 引擎]      → "发动机, 引擎"
        tags:                        → "航天, CFD"
          - 航天
          - CFD
    2026-08-14：此前只认平铺标量，Obsidian 最常见的多行 tags 会解析成空串（审计 F19）。
    """
    meta = {}
    body = text
    if not text.startswith("---"):
        return meta, body
    end = text.find("\n---", 3)
    if end == -1:
        return meta, body
    fm = text[3:end]
    body = text[end + 4:]
    cur_key = None
    for line in fm.splitlines():
        if not line.strip():
            continue  # 空行不打断当前列表
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and cur_key:
            v = _clean_scalar(item.group(1))
            if v:
                meta[cur_key] = f"{meta[cur_key]}, {v}" if meta.get(cur_key) else v
            continue
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            cur_key = m.group(1)
            val = _clean_scalar(m.group(2))
            if val.startswith("[") and val.endswith("]"):
                val = ", ".join(_clean_scalar(p) for p in val[1:-1].split(",")
                                if _clean_scalar(p))
            meta[cur_key] = val
        else:
            cur_key = None  # 无法识别的行：结束当前键，避免误吞后续列表项
    return meta, body


def clean_wikilinks(text):
    """清洗 wiki 链接（[[...]]）：保留读者实际看到的文字，剥掉路径与锚点。

    - [[目标|别名]] / [[目标\\|别名]]（表格转义管道）→ 别名
    - [[目标]]                                    → 目标
    - [[folder/目标#标题]] / [[目标#^块id]]        → 目标（去路径、去锚点）
    - [[#标题]]（本文件锚点）                      → 标题
    - ![[嵌入]]（图片/附件嵌入）                    → 去除
    在切块前调用。

    2026-08-14：此前裸 [[目标]] 被整个删掉（返回空串）。Obsidian 里裸链接是
    主流写法，而链接目标恰恰是笔记里最高信号的概念词——等于把关键词同时从
    嵌入文本和 BM25 词表里抹掉（审计 F9）。
    """
    def _repl(m):
        if m.group(0).startswith("!"):
            return ""  # ![[...]] 是附件嵌入，不是正文
        inner = m.group(1).replace(r"\|", "|")  # 表格里 \| 是转义管道，还原为分隔符
        parts = inner.split("|")
        if len(parts) > 1:
            return parts[1].strip()
        target = parts[0].strip()
        head, _, anchor = target.partition("#")
        head = head.strip()
        if head:
            return head.rsplit("/", 1)[-1].strip()  # 去掉 folder/ 路径前缀
        anchor = anchor.strip()
        return "" if anchor.startswith("^") else anchor  # ^块id 无语义，标题保留

    return re.sub(r"!?\[\[([^\]]*)\]\]", _repl, text)


def extract_wikilink_targets(text):
    """抽取正文中出现的 wiki 链接目标笔记名（去重、排序），供双链关系图使用。

    与 clean_wikilinks 共用同一条 [[...]] 语法规则，但取「目标」而非「显示文字」：
    [[目标|别名]] 取目标，[[目标#标题]] 去锚点取目标，![[嵌入]] 视为附件不计入关系，
    [[#本文件标题]]（无目标头）不计入。不参与嵌入/BM25，纯粹是关系数据的旁路产出。
    """
    targets = set()
    for m in re.finditer(r"!?\[\[([^\]]*)\]\]", text):
        if m.group(0).startswith("!"):
            continue
        inner = m.group(1).replace(r"\|", "|")
        target = inner.split("|", 1)[0].strip()
        head = target.partition("#")[0].strip()
        if head:
            targets.add(head.rsplit("/", 1)[-1].strip())
    return sorted(targets)


def load_meta(path=INDEX_META):
    if Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def save_meta(meta, path=INDEX_META):
    """原子写：先写临时文件再 os.replace，防止写入中断损坏 meta。

    写入切块版本号（_version），下次运行时据此判断旧索引是否需要重嵌。
    """
    DATA_DIR.mkdir(exist_ok=True)
    data = dict(meta)
    data["_version"] = META_VERSION
    p = Path(path)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def resolve_note_relations(meta_file, target):
    """双链关系查询：给定笔记标识（库内相对路径，或不含扩展名的标题），
    返回其出链（本文链接到谁）与入链（谁链接到本文）。

    完全基于当前 meta 现算，不持久化 inlinks（入链是全局反向索引，任何一个
    文件的出链变化都会影响别的文件的入链，与其维护一份容易过期的反向缓存，
    不如每次现查——库规模是个人笔记量级，现算成本可忽略）。
    标题重名时按 meta 字典迭代顺序任取其一命中，与 Obsidian 本身对同名笔记
    的处理一样存在这种歧义，不追求消歧。
    """
    meta = load_meta(meta_file)
    real = {k: v for k, v in meta.items() if isinstance(v, dict)}
    by_stem = {}
    for rel in real:
        by_stem[Path(rel).stem] = rel  # 重名时后出现的覆盖前面的（顺序即 meta 顺序）

    def _resolve(name):
        if name in real:
            return name
        return by_stem.get(Path(name).stem)

    rel = _resolve(target)
    if rel is None:
        return {"resolved": False, "file": None, "outlinks": [], "inlinks": []}

    outlinks = set()
    for name in real[rel].get("links", []):
        r = _resolve(name)
        if r and r != rel:
            outlinks.add(r)

    inlinks = set()
    for other, info in real.items():
        if other == rel:
            continue
        for name in info.get("links", []):
            if _resolve(name) == rel:
                inlinks.add(other)
                break

    return {"resolved": True, "file": rel,
            "outlinks": sorted(outlinks), "inlinks": sorted(inlinks)}


def collect_md_files(vault, exclude_dirs=EXCLUDE_DIRS, exclude_files=STRUCTURE_FILES,
                     exclude_patterns=EXCLUDE_PATTERNS, extensions=None):
    """收集应纳入索引的文件（与索引使用同一套过滤规则；扩展名白名单，默认 md）。"""
    if not Path(vault).is_dir():
        return []
    exts = [e.lower().lstrip(".") for e in (extensions or ["md"])]
    pats = tuple(exclude_patterns or ())
    return [p for p in Path(vault).rglob("*")
            if p.is_file() and p.suffix.lower().lstrip(".") in exts
            and not any(any(ex in part for ex in exclude_dirs) for part in p.parts)
            and p.name not in exclude_files
            and not p.name.startswith(pats)]


def make_anchor(front, body):
    """中文锚点：文件 title/summary 含中文且正文以英文为主时，生成中文锚点文本。

    仅用于该文件的首块（i==0），避免锚点词频在多块间虚增、降低块级区分度。
    v3 起标题链已拼入块文本，此函数不再被调用（保留定义仅供历史参考）。
    """
    zh_parts = [front.get("title", ""), front.get("summary", "")]
    zh_parts = [p for p in zh_parts if p and re.search(r"[\u4e00-\u9fff]", p)]
    if not zh_parts:
        return ""
    non_zh = re.sub(r"[\u4e00-\u9fff]", "", body)
    ascii_chars = sum(1 for c in non_zh if c.isascii() and (c.isalpha() or c.isdigit()))
    if ascii_chars < max(len(non_zh) * 0.5, 40):
        return ""
    return "【" + "；".join(zh_parts) + "】\n"


def _chroma_count(collection_name=COLLECTION_NAME):
    """Chroma 实际块数（毫秒级，不加载模型）。失败返回 None（库损坏等）。"""
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        return client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"}
        ).count()
    except Exception as e:
        log(f"Chroma count 失败（忽略）：{e}")
        return None


def kb_stale(vault, meta_file=INDEX_META, collection_name=COLLECTION_NAME,
             exclude_dirs=EXCLUDE_DIRS, exclude_files=STRUCTURE_FILES,
             exclude_patterns=EXCLUDE_PATTERNS, extensions=None, tbd_ratio=0.0,
             agent_allowed=None):
    """指纹检查：先比 mtime+size（快速路径），变化才读全文 MD5。

    只读、不加载模型、不嵌入。返回 (是否过期, 统计)。
    额外校验 Chroma↔meta 一致性（崩溃自愈）：块数不符（如 --full 中途被杀
    导致 0 块）也视为过期，触发重建。
    库路径不存在：返回 stale=True 且 stats 带 missing=True（调用方据此跳过
    自动同步并保留旧索引，防止"路径消失 → 判全删 → 清空该库"）。
    库路径存在但一个文件都扫不到、而 meta 里有记录：带 emptied=True，同样跳过
    （2026-08-14 审计 F16：import.py --create 会主动建一个空目录，
    "空目录"不带 missing 标志，于是下一次检索的自动同步会判"文件全删"，
    把刚导入的索引清空。import.py:291 的警告正是这个场景，而 --create 结构性
    地保证了它成立）。
    切块逻辑版本（META_VERSION）变化：带 version_upgrade=True（审计 F14——
    此前 kb_stale 从不看 _version，版本号提升不会主动触发重建，要等到碰巧
    有文件改动才顺带生效）。
    """
    if not Path(vault).is_dir():
        return True, {"changed": 0, "added": 0, "removed": 0, "missing": True}
    meta = load_meta(meta_file)
    files = collect_md_files(vault, exclude_dirs, exclude_files, exclude_patterns, extensions)
    # 判空基准 = 真实条目（剔除 _version 哨兵与非 dict 脏数据）：
    # 只剩版本号的 meta 不是"有记录"，否则全删/清空后的收敛态会被 emptied 分支
    # 永远误报 stale。
    real_meta = {k: v for k, v in meta.items() if isinstance(v, dict)}
    if real_meta and not files:
        return True, {"changed": 0, "added": 0, "removed": 0, "emptied": True}
    if meta.pop("_version", 1) != META_VERSION:
        return True, {"changed": 0, "added": 0, "removed": 0, "version_upgrade": True}
    if not real_meta:
        # 从未索引过：有文件则全部待建；条目与文件双空 = 已收敛（如库内文件
        # 被全部删除后的稳态，不得每轮误报 stale 触发无效重建）
        if files:
            return True, {"changed": 0, "added": len(files), "removed": 0}
        return False, {"changed": 0, "added": 0, "removed": 0}
    seen = set()
    changed = 0
    added = 0
    xsig = current_backend_sig()
    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        if agent_allowed is not None and \
                fpath.suffix.lower().lstrip(".") not in agent_allowed:
            # Agent 未授权格式：冻结——保留既有条目（若有）防止被裁剪/清理，
            # 零 I/O、不计任何变更；无条目则视同不存在，待人类路径首建。
            if meta.get(rel) is not None:
                seen.add(rel)
            continue
        entry = meta.get(rel)
        if entry is not None and _backend_changed(entry, xsig):
            changed += 1  # OCR 能力已变化：零 I/O 判待重试，交由索引轮转正/再落终态
            continue
        try:
            st = fpath.stat()
        except OSError:
            continue  # 状态拿不到，本轮无法判定（下轮自然重试；不计 changed/removed）
        entry = meta.get(rel)
        # 快速路径：size+mtime 未变即稳——对 xfail/tbd 终态条目同等适用，
        # 终态持久化在 meta 里，O(1) 跳过，绝不读全文、绝不跑转换。
        if (entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime_ns
                and not _links_missing(entry)):
            seen.add(rel)
            continue
        text, bhash = _load_text(fpath)
        if bhash == _UNREADABLE:
            # 终态一致即稳定（罕见：mtime 变了但仍读不了）；否则计一次待重试
            if _entry_converged(entry, REASON_UNREADABLE):
                seen.add(rel)
                continue
            changed += 1
            continue
        suffix = fpath.suffix.lower().lstrip(".")
        if suffix in TEXT_EXTS and text is None:
            # 空文件落 empty 终态。历史上空正文 md 不落 meta，导致每轮重计
            # added → 每轮判 stale（「明确不做」备案的既有隐患，随终态机制一并收敛）
            if _entry_converged(entry, REASON_EMPTY):
                seen.add(rel)
                continue
            changed += 1
            continue
        if suffix in TEXT_EXTS and is_tbd_heavy(text or "", tbd_ratio):
            if _entry_converged(entry, REASON_TBD):
                seen.add(rel)
                continue
            changed += 1
            continue
        # 二进制源走到这里只做字节哈希比对（绝不提取）；文本源同旧语义。
        if entry and entry.get("hash") == bhash and not _links_missing(entry):
            seen.add(rel)
            continue
        if entry:
            changed += 1
        else:
            added += 1
    # removed 只统计"应当索引但缺失"的文件：终态条目（tbd/xfail）不属于应索引
    # 文件，稳定状态下留在 meta 里但不进应索引集合，不能每次都算 removed。
    meta_files = {k for k, v in meta.items()
                  if isinstance(v, dict) and not _skipped(v)}
    removed = len(meta_files - seen)
    stale = bool(changed or added or removed)

    expected = sum(info.get("chunks", 0) for info in meta.values()
                   if isinstance(info, dict) and not _skipped(info))
    actual = _chroma_count(collection_name)
    if actual is not None and actual != expected:
        log(f"索引一致性校验失败：Chroma {actual} 块 vs meta {expected} 块，触发重建")
        stale = True

    return stale, {"changed": changed, "added": added, "removed": removed}


def _open_lock_file(path):
    """打开/创建锁文件（r+b，非 append）。

    "a+b" 模式下 seek(0)+write 会被操作系统强制写到文件尾（O_APPEND 语义），
    _lock_record_holder 记录的 PID 永远追尾追加，旧内容留在开头——
    实测：锁文件内容变成 "999…旧PID" + "新PID"，_lock_holder_pid 读到旧值，
    超时报错的"疑似持有者"失效；单例 PID 校验读到旧值导致误判（2026-08-15）。
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    return os.fdopen(fd, "r+b")


def _lock_try_acquire(f):
    """非阻塞尝试获取文件锁。成功 True；锁被占用抛 OSError（Windows EACCES/EDEADLOCK，Linux EAGAIN/EACCES）。"""
    if _IS_WINDOWS:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return True


def _lock_release(f):
    """按平台释放文件锁（与 _lock_try_acquire 严格对称）。

    msvcrt 按当前文件指针位置锁定/解锁：必须先归位到 0，
    否则解锁位置 ≠ 锁定位置 → PermissionError。
    """
    f.seek(0)
    if _IS_WINDOWS:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _lock_holder_pid(f):
    """读锁文件首行记录的持有者 PID（无则 None）。"""
    try:
        f.seek(0)
        raw = f.read(64).strip()
        return int(raw) if raw.isdigit() else None
    except Exception:
        return None


def _lock_record_holder(f):
    """获锁成功后把本进程 PID 写入锁文件（供他人超时时定位持有者），写完归位指针。

    先 truncate 清空再写：旧内容若残留会与新 PID 直接拼接（如 "31008"+"999"→
    "31008999"），读取方 split 解析出错误 PID（2026-08-15 实测）。
    """
    try:
        f.seek(0)
        f.truncate(0)
        f.write(str(os.getpid()).encode())
        f.flush()
        f.seek(0)
    except Exception:
        pass


def _lock_acquire_with_timeout(f, timeout=LOCK_TIMEOUT_SECONDS, poll=LOCK_POLL_SECONDS):
    """获取写锁，带超时与死锁自愈：

    - 非阻塞尝试 + 轮询，超过 timeout 即放弃（不再无限死等）；
    - 超时后读取锁文件记录的持有者 PID：若该进程已死（锁随进程死亡自动释放，
      理论上不会发生，作兜底），清空锁文件重试一次；
    - 仍失败抛 LockBusyError（含持有者 PID 与处置建议），由上层转成明确报错。
    """
    deadline = time.time() + timeout
    while True:
        try:
            _lock_try_acquire(f)
            _lock_record_holder(f)
            return
        except OSError:
            if time.time() >= deadline:
                holder = _lock_holder_pid(f)
                if holder is not None and not _pid_alive(holder):
                    log(f"锁持有者 PID {holder} 已死（异常残留），清空锁文件后重试一次")
                    try:
                        f.seek(0)
                        f.truncate(0)
                        f.flush()
                        _lock_try_acquire(f)
                        _lock_record_holder(f)
                        return
                    except OSError:
                        pass
                who = f"（疑似持有者 PID {holder}）" if holder else ""
                raise LockBusyError(
                    f"写锁等待超时（>{timeout}s）{who}：可能仍有其他索引进程/残留实例在运行。"
                    f"请用 index_status 查看进度，或结束残留进程后重试。"
                ) from None
            time.sleep(poll)


def write_lock(timeout=LOCK_TIMEOUT_SECONDS):
    """进程级文件锁（Windows msvcrt / Linux fcntl），串行化 Chroma 写操作，防并发损坏。

    超时抛 LockBusyError 而非无限阻塞——由调用方捕获转成明确提示。
    """
    import contextlib

    @contextlib.contextmanager
    def _lock():
        DATA_DIR.mkdir(exist_ok=True)
        f = _open_lock_file(LOCK_FILE)
        acquired = False
        try:
            f.seek(0, 2)
            if f.tell() == 0:
                f.write(b"0")
            f.seek(0)
            _lock_acquire_with_timeout(f, timeout=timeout)
            acquired = True  # 只有真正拿到锁才允许释放（避免没锁却解锁 → PermissionError 覆盖原始错误）
            yield
        finally:
            if acquired:
                try:
                    _lock_release(f)
                except OSError:
                    pass
            f.close()

    return _lock()


def index_vault(vault, incremental=True, full=False):
    """旧单库入口（legacy）：按 config 全局配置索引（GUI/export 兼容）。"""
    return _index_core(vault, COLLECTION_NAME, INDEX_META,
                       EXCLUDE_DIRS, STRUCTURE_FILES, EXCLUDE_PATTERNS, ["md"],
                       CFG["chunk_char_limit"], CFG["short_doc_char_limit"],
                       library_label="",
                       incremental=incremental, full=full)  # 2026-08-14：此前两个参数都没往下传


def index_library(lib, incremental=True, full=False, agent_allowed=None):
    """按注册表库索引：独立 collection / 指纹文件 / 排除规则 / 切块粒度。lib = effective_config()。

    agent_allowed：Agent 门禁（人机分权）。None = 无限制（GUI/CLI 人类路径）；
    传后缀集合 = 仅处理这些格式，其余已配置格式的文件冻结（见 _index_core）。
    """
    return _index_core(lib["path"], lib["collection"], meta_path(lib["name"]),
                       lib["exclude_dirs"], lib["exclude_files"], lib["exclude_patterns"],
                       lib["extensions"], lib["chunk_char_limit"],
                       lib["short_doc_char_limit"], library_label=lib["name"],
                       incremental=incremental, full=full,
                       tbd_ratio=CFG.get("tbd_exclude_ratio", 0.0),
                       agent_allowed=agent_allowed)


def _index_core(vault, collection_name, meta_file, exclude_dirs, exclude_files,
                exclude_patterns, extensions, chunk_max, short_doc,
                library_label="", incremental=True, full=False, tbd_ratio=0.0,
                agent_allowed=None):
    tag = f"[{library_label}] " if library_label else ""
    if not Path(vault).is_dir():
        log(f"{tag}库路径不存在，跳过索引（保留现有索引）：{vault}")
        return
    files = collect_md_files(vault, exclude_dirs, exclude_files, exclude_patterns, extensions)
    progress_start(phase="scanning", files_total=len(files), message="扫描文件...",
                   library=library_label)

    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        collection = client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"}
        )

        meta = {} if full else load_meta(meta_file)
        # 切块/清洗逻辑升级检测：旧索引文本与当前逻辑不一致，强制全量重建
        if not full and meta.pop("_version", 1) != META_VERSION:
            log(f"{tag}切块逻辑版本升级（v{META_VERSION}），强制全量重建")
            meta = {}
        # 崩溃/丢失自愈（P7，v9 推广为通用一致性校验）：meta 期望块数与
        # Chroma 实际块数不符时，增量路径无法凭空补出缺失的块（指纹全命中
        # → new_ids 为空），必须按全量重建处理。覆盖两类场景：
        #   a) count==0：--full 中途被杀在清库窗口（原 P7）；
        #   b) 部分丢失：进程被杀致 Chroma WAL 段未持久化、或外部动库——
        #      此前会陷入「每轮判 stale → 每轮修不了」的死循环。
        # 全终态库（只有 xfail/tbd 条目）期望 0 块且实际 0 块，相等，不误伤。
        if not full and meta:
            _expected = sum(i.get("chunks", 0) for i in meta.values()
                            if isinstance(i, dict) and not _skipped(i))
            _actual = collection.count()
            if _actual != _expected:
                log(f"{tag}一致性校验失败：meta 期望 {_expected} 块 vs Chroma 实际 "
                    f"{_actual} 块，增量无法修复缺失块，自动转全量重建")
                meta = {}
        current_rels = set()  # 动态构建：本轮磁盘上应存在的文件（含持久化终态条目）

        new_ids, new_texts, new_metas = [], [], []
        converted = 0  # 本轮做过文档转换（pdf/docx→md）的文件数
        changed = 0
        unchanged = 0
        scan_start = time.time()

        update_progress(phase="scanning", message="比对指纹、切块...")
        xsig = current_backend_sig()
        for fpath in files:
            rel = str(fpath.relative_to(vault)).replace("\\", "/")
            if agent_allowed is not None and \
                    fpath.suffix.lower().lstrip(".") not in agent_allowed:
                # Agent 未授权格式：冻结——保留既有条目与块（不裁剪不清理），
                # 零 I/O、不转换、不计变更；无条目则视同不存在，待人类路径首建。
                if meta.get(rel) is not None:
                    current_rels.add(rel)
                continue
            try:
                st = fpath.stat()
            except OSError as e:
                log(f"{tag}无法获取文件状态，本轮跳过（下轮重试）：{rel}（{e}）")
                continue
            old = meta.get(rel)
            current_rels.add(rel)  # 本轮所有存活路径（终态/成功/未变）统一在此持久化
            # 快速路径：size+mtime 未变则免读全文/免提取（与 kb_stale 同一指纹策略），
            # 对 xfail/tbd 终态条目同等适用——终态稳定即 O(1) 收敛，绝不重复提取。
            # 例外：OCR 能力签名变化（启用后端/补 Key）→ 穿透快速路径重试转正。
            if (incremental and old and old.get("size") == st.st_size
                    and old.get("mtime") == st.st_mtime_ns
                    and not _backend_changed(old, xsig)
                    and not _links_missing(old)):
                unchanged += 1
                update_progress(files_done=unchanged + changed, message=f"扫描 {rel}")
                continue
            text, bhash = _load_text(fpath)

            # ---- 统一终态出口（封堵死循环入口）：None 判定严格先于 is_tbd_heavy ----
            if bhash == _UNREADABLE:
                meta[rel] = _terminal_entry(st, bhash, REASON_UNREADABLE)
                log(f"{tag}文件不可读（锁定/权限？），记入 unreadable 终态待重试：{rel}")
                changed += 1
                update_progress(files_done=unchanged + changed, message=f"跳过不可读 {rel}")
                continue
            suffix = fpath.suffix.lower().lstrip(".")
            is_text = suffix in TEXT_EXTS
            if is_text and text is None:
                # 空文件（含仅 frontmatter 的 md）落 empty 终态。历史上空正文不落
                # meta，导致每轮重计 added → 每轮误判 stale（备案隐患，就此收敛）
                meta[rel] = _terminal_entry(st, bhash, REASON_EMPTY)
                log(f"{tag}空文件（无正文），记入 empty 终态：{rel}")
                changed += 1
                update_progress(files_done=unchanged + changed, message=f"跳过空文件 {rel}")
                continue
            if (incremental and old and not _skipped(old) and old.get("hash") == bhash
                    and not _links_missing(old)):
                unchanged += 1
                update_progress(files_done=unchanged + changed, message=f"扫描 {rel}")
                continue

            front, body = {}, ""
            if is_text:
                if is_tbd_heavy(text or "", tbd_ratio):
                    # 占位重文件落 tbd 终态；其旧块由 chunks=0 驱动清理阶段删除
                    meta[rel] = _terminal_entry(st, bhash, REASON_TBD)
                    log(f"{tag}跳过占位重文件（TBD 占比 ≥ {tbd_ratio:.0%}）：{rel}")
                    changed += 1
                    update_progress(files_done=unchanged + changed,
                                    message=f"跳过占位重文件 {rel}")
                    continue
                front, body = extract_frontmatter(text or "")
                links = extract_wikilink_targets(body)
                body = clean_wikilinks(body)
            else:
                # 二进制源：转 Markdown 后走同一条切块管线。converting 相位在
                # progress_text 里豁免停滞告警（大文件转换可能超过 STALL_TIMEOUT）。
                update_progress(phase="converting", message=f"转换 {rel}...")
                converted += 1
                body, reason = extract_to_markdown(fpath)
                # G7 单点还原（问题 32）：converting 置位后有提取失败 / 空 body
                # 防御 / 成功切块三个出口，在此一处还原 scanning 即全覆盖——
                # 转换豁免窗口严格闭合于 extract_to_markdown 的真实耗时，绝不
                # 泄漏进后续相位（600s 级 MinerU 云端 OCR 也完全在这段窗口内）。
                update_progress(phase="scanning", message=f"已转换 {rel}")
                if body is None:
                    meta[rel] = _terminal_entry(st, bhash, reason or REASON_EXTRACT_FAILED,
                                                xsrc=xsig)
                    log(f"{tag}提取失败（{reason or 'extract-failed'}），记入终态待重试：{rel}")
                    changed += 1
                    update_progress(files_done=unchanged + changed, message=f"提取失败 {rel}")
                    continue
                links = extract_wikilink_targets(body)
                body = clean_wikilinks(body)
            # 防御分支：正常不应到达（文本空已归一为终态、提取器保证非空产出）
            if not body.strip():
                meta[rel] = _terminal_entry(st, bhash, REASON_EMPTY)
                changed += 1
                update_progress(files_done=unchanged + changed, message=f"跳过空内容 {rel}")
                continue
            # 两级切块：标题切 → 超长块降级段落切 → 超长段级句子剪（永不剪断句子）
            if len(body) <= short_doc:
                chunks = [(front.get("title", ""), body)]
            else:
                chunks = []
                for heading, text in split_by_headings(body):
                    if len(text) <= chunk_max:
                        chunks.append((heading, text))
                    else:
                        # 表格绑定上下文（评估选定方案）：表格与直接上文 + 直接下文整组绑定。
                        # 先并上文（引导句），再并下文（表格后的解释/结论段），保证表格始终
                        # 携带完整上下文；含表格的合并块整体保留（宁大勿断，避免表格被切断）。
                        # 评估结论：命中率与表格独立成块持平，但块语义完整性更好。
                        paras = []
                        prev = None
                        for p in split_paragraphs(text):
                            if p.lstrip().startswith("|") and prev is not None:
                                prev = prev + "\n\n" + p  # 表格并入上文（表格绑定）
                            elif is_list_block(p) and prev is not None and is_list_block(prev):
                                prev = prev + "\n\n" + p  # 连续列表项跨空行合并，避免拆散
                            else:
                                if prev is not None:
                                    paras.append(prev)
                                prev = p
                        if prev is not None:
                            paras.append(prev)
                        # 表格结尾段吞入下一段（下文绑定）
                        final = []
                        j = 0
                        while j < len(paras):
                            p = paras[j]
                            lines = p.splitlines()
                            if (lines and lines[-1].strip().startswith("|")
                                    and j + 1 < len(paras)):
                                final.append(p + "\n\n" + paras[j + 1])
                                j += 2
                            else:
                                final.append(p)
                                j += 1
                        for p in final:
                            if len(p) <= chunk_max:
                                chunks.append((heading, p))
                            elif any(l.strip().startswith("|") for l in p.splitlines()):
                                chunks.append((heading, p))  # 含表格整体保留
                            elif is_list_block(p):
                                # 列表按项边界切（永不从列表项中间剪断）
                                for sub in split_list_block(p, chunk_max):
                                    chunks.append((heading, sub))
                            else:
                                # 2026-08-14：此前这里漏传 chunk_max（相邻的
                                # split_list_block 传了），长无标点段落会突破块上限
                                for s in split_sentences(p, chunk_max):
                                    chunks.append((heading, s))
            anchor = ""  # v3 起标题链已拼入块文本，中文锚点机制停用

            # v6：文件级语义锚点 = 文件名 + frontmatter title + tags。
            # Obsidian 里文件名往往就是概念本体，此前这三样只写进 metadata，
            # 完全没进向量、也没进 BM25 词表（BM25 建在 documents 上）——
            # 与"裸 wikilink 被删"叠加后，笔记的概念层信息基本没进索引（审计 F20）。
            title = front.get("title", "")
            tags = front.get("tags", "")
            doc_parts = [Path(rel).stem, title, tags]

            def _ctx_for(heading):
                """文件级锚点 + 标题路径，逐段去重。

                去重是必要的：短文档走整篇成块时 heading 直接取 title，
                文件名与 title 也常常相同，不去重会得到
                "火箭发动机 / 火箭发动机设计笔记 / 航天 / 火箭发动机设计笔记"
                这种重复串，白占 token 还会让该词在块内词频虚高、扭曲 BM25。
                """
                out, seen = [], set()
                for part in doc_parts + (heading.split(" / ") if heading else []):
                    part = (part or "").strip()
                    key = part.lower()
                    if part and key not in seen:
                        seen.add(key)
                        out.append(part)
                return " / ".join(out)

            for i, (heading, chunk_text) in enumerate(chunks):
                cid = f"{rel}::{i}"
                new_ids.append(cid)
                # v3：标题链拼入每个块的嵌入/BM25 文本（标题是块所属主题的最强锚点；
                # 实测 FLUENT 文件干货块相似度 0.39→0.59）。
                # v6：前面再拼文件级锚点。metadata 记录完整前缀 ctx 供输出剥离
                # （hp 仍单独保留，_expand_parent 按它分组父节）。
                hp = heading
                ctx = _ctx_for(hp)
                new_texts.append((ctx + "\n" if ctx else "") + chunk_text)
                new_metas.append({
                    "file": rel,
                    "heading": heading,
                    "title": title,
                    "tags": tags,
                    "chunk": str(i),
                    "hp": hp,
                    "ctx": ctx,
                })
            meta[rel] = {"hash": bhash, "chunks": len(chunks), "size": st.st_size,
                         "mtime": st.st_mtime_ns, "tbd": False, "links": links}
            changed += 1
            update_progress(files_done=unchanged + changed, message=f"切块 {rel}")

        # 裁剪 meta：移除磁盘上已不存在的文件条目（Bug1 关键一步），
        # 这样下方 valid 集合不含已删文件，其块会在清理阶段被 collection.delete。
        meta = {rel: info for rel, info in meta.items() if rel in current_rels}

        # 嵌入在锁外完成（最耗时：全量 ~28s），锁内只做毫秒级写操作。
        # 分批嵌入（EMBED_BATCH_SIZE）提供逐批进度心跳，同时降低显存峰值。
        # 注意：模型不可重入，编码在 _encode_lock 内串行，且与写入必须串行，
        # 故先编码后持锁。encode_safe 自动处理 CUDA 内存不足 → CPU 降级。
        emb = None
        if new_ids:
            total_chunks = len(new_ids)
            update_progress(phase="embedding", message=f"嵌入 0/{total_chunks} 块...",
                            chunks_total=total_chunks, chunks_done=0)
            log(f"{tag}嵌入 {total_chunks} 个新块（{changed} 个文件变更，{unchanged} 个未变）...")
            emb_batches = []
            for start in range(0, total_chunks, EMBED_BATCH_SIZE):
                end = min(start + EMBED_BATCH_SIZE, total_chunks)
                emb_batches.append(encode_safe(new_texts[start:end]))
                update_progress(chunks_done=end,
                                message=f"嵌入 {end}/{total_chunks} 块（{changed} 文件变更）...")
            emb = _vstack(emb_batches)
            _release_cuda_cache()

        # 写锁包住全部写操作（delete/upsert/清理/save_meta）
        # 埋点④（问题 32）：等锁排队最长可达 lock_timeout_seconds（默认 60s，
        # 远超 STALL_TIMEOUT），且无变更路径会直达这里——先置 waiting-lock 相位
        # 并写宽限再开始排队；拿到锁后置 writing 相位并续一段宽限（upsert/清理/
        # save_meta 本身也可能超过 STALL_TIMEOUT）。waiting-lock 是新增 phase 值，
        # 全部消费方已核对安全降级（widgets stepper idx=-1 兜底、PHASE_COLOR.get
        # 默认值、progress_ratio 走 else 分支、server 不消费 phase）。
        update_progress(phase="waiting-lock", message="等待写锁...",
                        stall_grace_s=STALL_GRACE_WRITE)
        with write_lock():
            update_progress(phase="writing", message="写库与清理...",
                            stall_grace_s=STALL_GRACE_WRITE)
            if full:
                log(f"{tag}全量重建：清空旧库后写入...")
                client.delete_collection(collection_name)
                collection = client.get_or_create_collection(
                    name=collection_name, metadata={"hnsw:space": "cosine"}
                )

            if emb is not None:
                collection.upsert(ids=new_ids, embeddings=emb.tolist(), documents=new_texts, metadatas=new_metas)
            else:
                log(f"{tag}无变更（{unchanged} 个文件全部命中缓存）")

            # 精确清理：有效 id = meta 中每个文件按记录的块数生成。
            # 已删文件（不在 meta）与幽灵块（块数变少后超出 chunks 的旧 id）都会被清除。
            valid = set()
            for rel, info in meta.items():
                if not isinstance(info, dict):
                    continue
                for i in range(info.get("chunks", 0)):
                    valid.add(f"{rel}::{i}")
            all_ids = collection.get(include=[])["ids"]
            stale = [i for i in all_ids if i not in valid]
            if stale:
                collection.delete(ids=stale)
                log(f"{tag}清理 {len(stale)} 个失效块")

            save_meta(meta, meta_file)
            final_count = collection.count()
        conv_note = f"（转换文档 {converted} 个）" if converted else ""
        update_progress(phase="done", running=False,
                        message=f"完成。Chroma 现有 {final_count} 个块。{conv_note}")
        log(f"{tag}完成。Chroma 现有 {final_count} 个块。{conv_note}"
            f"耗时 {time.time() - scan_start:.1f}s。")
    except Exception as e:
        progress_error(e)
        raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=None, help="旧单库入口：直接指定路径（legacy）")
    ap.add_argument("--library", default="all", help="库名（或 all=全部注册库，默认）")
    ap.add_argument("--full", action="store_true", help="全量重建，忽略增量")
    args = ap.parse_args()

    if args.vault:
        index_vault(args.vault, incremental=not args.full, full=args.full)
        sys.exit(0)

    try:
        if args.library in ("", "all"):
            entries = load_registry()
        else:
            entries = resolve_entries(args.library, "")
    except ValueError as e:
        log(f"错误：{e}")
        sys.exit(1)
    if not entries:
        log("错误：没有已注册的库。请先用 library.py add <路径> 注册。")
        sys.exit(1)

    failed = 0
    for entry in entries:
        cfg = effective_config(entry)
        log(f"开始索引库：{cfg['name']} → {cfg['path']}")
        try:
            index_library(cfg, incremental=not args.full, full=args.full)
        except LockBusyError:
            raise  # 写锁被占影响所有库，继续跑其余库没有意义，直接上抛
        except Exception as e:
            failed += 1
            log(f"[{cfg['name']}] 索引失败（继续下一库）：{e}")
    if failed:
        log(f"全部索引任务结束：{failed} 个库失败，其余成功。")
        sys.exit(1)
    log("全部索引任务完成。")
