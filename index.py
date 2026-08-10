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
META_VERSION = 2


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

_heartbeat_thread = None
_heartbeat_stop = threading.Event()


def _pid_alive(pid):
    """进程是否存活（signal 0 探测）。"""
    try:
        os.kill(pid, 0)
        return True
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
    """
    global _progress
    now = time.time()
    with _progress_lock:
        base = dict(_progress)
        base.update(fields)
        base.setdefault("pid", os.getpid())
        base.setdefault("started_at", now)
        base["updated_at"] = now
        base["last_advance_at"] = now
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


def progress_start(phase, files_total, message=""):
    """索引开始：重置进度，置 running，启动心跳线程。"""
    global _heartbeat_thread, _heartbeat_stop
    _heartbeat_stop = threading.Event()
    update_progress(running=True, phase=phase, message=message,
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
        if gap > HEARTBEAT_TIMEOUT:
            tip = ""
            if (p.get("elapsed_s") or 0) < 300 and (p.get("chunks_done") or 0) == 0:
                tip = "（任务早期：首次加载 embedding 模型可能耗时 1-2 分钟，若进程 CPU 仍活跃属正常）"
            lines.append(f"  ⚠ 疑似卡死：心跳已停 {int(gap)}s（> {int(HEARTBEAT_TIMEOUT)}s）{tip}")
            lines.append(f"  建议检查 PID {p.get('pid')} 是否存活；确认卡死可结束该进程后重试。")
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


def _load_model(device):
    """按 device 加载模型。CUDA/CPU 均优先 fp16 减半显存（8GB 卡防共享显存溢出），失败回退 fp32。"""
    try:
        return _build_model(device, fp16=True), device
    except Exception as e:
        log(f"{device} fp16 加载失败，回退 fp32：{e}")
    return _build_model(device), device


def _try_switch_back_cuda():
    """CPU 模型中且探测到显存恢复：释放 CPU 模型 → 加载 CUDA；失败回滚并冷却。"""
    global _model, _device
    old = _model
    try:
        _model = None  # 先释放 CPU 模型，避免新旧模型双份内存峰值
        new, _ = _load_model("cuda")
        _model, _device = new, "cuda"
        del old
        log("显存已恢复，自动切回 CUDA")
        _report_device("cuda", note="auto-switched-back")
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

    经验校准（fp16 + bge-m3，8GB 移动卡实测）：固定开销约 3GB
    （模型+上下文+工作区），长块（1500 字）激活约 0.4GB/块。
    """
    global _last_batch_cap
    if _device != "cuda":
        return desired
    import torch
    free_gb = torch.cuda.mem_get_info()[0] / 1024 ** 3
    cap = int((free_gb - 3.0) / 0.4)
    cap = max(2, min(32, cap))
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


def split_by_headings(text):
    """按 Markdown 标题切块，每个 H1/H2/H3 起新块。返回 [(heading_path, content)]"""
    lines = text.splitlines()
    chunks = []
    current_heading = ""
    current_lines = []
    heading_re = re.compile(r"^(#{1,3})\s+(.+)$")

    def flush():
        if current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                chunks.append((current_heading, body))

    for line in lines:
        m = heading_re.match(line)
        if m:
            flush()
            current_heading = m.group(2).strip()
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


def extract_frontmatter(text):
    """提取 frontmatter 元数据，返回 dict 和去掉 frontmatter 的正文。"""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4 :]
            for line in fm.splitlines():
                m = re.match(r"^([\w]+):\s*(.*)$", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return meta, body


def clean_wikilinks(text):
    """清洗 wiki 链接（[[...]]）：别名保留、纯导航引用去除、路径垃圾剔除。

    - [[目标|别名]] / [[目标\\|别名]]（表格转义管道）→ 保留别名（读者实际看到的文字）
    - [[目标]] / [[目标#标题]] / [[目标#^块]] / ![[嵌入]] → 去除
    在切块前调用，避免纯引用标签污染嵌入与检索（引用方不应因标签被命中）。
    """
    def _repl(m):
        inner = m.group(1).replace(r"\|", "|")  # 表格里 \| 是转义管道，还原为分隔符
        parts = inner.split("|")
        if len(parts) > 1:
            return parts[1].strip()
        return ""

    return re.sub(r"!?\[\[([^\]]*)\]\]", _repl, text)


def load_meta():
    if INDEX_META.exists():
        return json.loads(INDEX_META.read_text(encoding="utf-8"))
    return {}


def save_meta(meta):
    """原子写：先写临时文件再 os.replace，防止写入中断损坏 meta。

    写入切块版本号（_version），下次运行时据此判断旧索引是否需要重嵌。
    """
    DATA_DIR.mkdir(exist_ok=True)
    data = dict(meta)
    data["_version"] = META_VERSION
    tmp = INDEX_META.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INDEX_META)


def collect_md_files(vault):
    """收集应纳入索引的 .md 文件（与索引使用同一套过滤规则）。"""
    return [p for p in Path(vault).rglob("*.md")
            if not any(part in EXCLUDE_DIRS for part in p.parts)
            and p.name not in STRUCTURE_FILES
            and not p.name.startswith(EXCLUDE_PATTERNS)]


def make_anchor(front, body):
    """中文锚点：文件 title/summary 含中文且正文以英文为主时，生成中文锚点文本。

    仅用于该文件的首块（i==0），避免锚点词频在多块间虚增、降低块级区分度。
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


def _chroma_count():
    """Chroma 实际块数（毫秒级，不加载模型）。失败返回 None（库损坏等）。"""
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        return client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        ).count()
    except Exception as e:
        log(f"Chroma count 失败（忽略）：{e}")
        return None


def kb_stale(vault):
    """指纹检查：先比 mtime+size（快速路径），变化才读全文 MD5。

    只读、不加载模型、不嵌入。返回 (是否过期, 统计)。
    额外校验 Chroma↔meta 一致性（崩溃自愈）：块数不符（如 --full 中途被杀
    导致 0 块）也视为过期，触发重建。
    """
    meta = load_meta()
    if not meta:
        return True, {"changed": 0, "added": len(collect_md_files(vault)), "removed": 0}
    files = collect_md_files(vault)
    seen = set()
    changed = 0
    added = 0
    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        seen.add(rel)
        st = fpath.stat()
        entry = meta.get(rel)
        if entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime_ns:
            continue
        content = fpath.read_text(encoding="utf-8", errors="replace")
        h = hashlib.md5(content.encode("utf-8")).hexdigest()
        if entry and entry.get("hash") == h:
            continue
        if entry:
            changed += 1
        else:
            added += 1
    removed = len(set(meta) - seen)
    stale = bool(changed or added or removed)

    expected = sum(info.get("chunks", 0) for info in meta.values())
    actual = _chroma_count()
    if actual is not None and actual != expected:
        log(f"索引一致性校验失败：Chroma {actual} 块 vs meta {expected} 块，触发重建")
        stale = True

    return stale, {"changed": changed, "added": added, "removed": removed}


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
    """获锁成功后把本进程 PID 写入锁文件（供他人超时时定位持有者），写完归位指针。"""
    try:
        f.seek(0)
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
        f = open(LOCK_FILE, "a+b")
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
    files = collect_md_files(vault)
    progress_start(phase="scanning", files_total=len(files), message="扫描 Vault 文件...")

    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

        meta = {} if full else load_meta()
        # 切块/清洗逻辑升级检测：旧索引文本与当前逻辑不一致，强制全量重建
        if not full and meta.pop("_version", 1) != META_VERSION:
            log(f"切块逻辑版本升级（v{META_VERSION}），强制全量重建")
            meta = {}
        # 崩溃自愈（P7）：meta 有数据但 Chroma 空（如 --full 中途被杀在清库窗口）
        # → 增量指纹全命中时 new_ids 为空，普通增量路径无法补数据，需强制全量重嵌。
        if not full and meta and collection.count() == 0:
            log(f"检测到索引库为空（meta 记录 {sum(i.get('chunks', 0) for i in meta.values())} 块），自动全量重建")
            meta = {}
        current_rels = {str(f.relative_to(vault)).replace("\\", "/") for f in files}

        new_ids, new_texts, new_metas = [], [], []
        changed = 0
        unchanged = 0
        scan_start = time.time()

        update_progress(phase="scanning", message="比对指纹、切块...")
        for fpath in files:
            rel = str(fpath.relative_to(vault)).replace("\\", "/")
            st = fpath.stat()
            old = meta.get(rel)
            # 快速路径：size+mtime 未变则免读全文（与 kb_stale 同一指纹策略）
            if incremental and old and old.get("size") == st.st_size and old.get("mtime") == st.st_mtime_ns:
                unchanged += 1
                update_progress(files_done=unchanged + changed, message=f"扫描 {rel}")
                continue
            content = fpath.read_text(encoding="utf-8", errors="replace")
            fhash = hashlib.md5(content.encode("utf-8")).hexdigest()
            if incremental and old and old.get("hash") == fhash:
                unchanged += 1
                update_progress(files_done=unchanged + changed, message=f"扫描 {rel}")
                continue

            front, body = extract_frontmatter(content)
            body = clean_wikilinks(body)
            # 两级切块：标题切 → 超长块降级段落切 → 超长段级句子剪（永不剪断句子）
            short_doc = CFG["short_doc_char_limit"]
            chunk_max = CFG["chunk_char_limit"]
            if len(body) <= short_doc:
                chunks = [(front.get("title", ""), body)]
            else:
                chunks = []
                for heading, text in split_by_headings(body):
                    if len(text) <= chunk_max:
                        chunks.append((heading, text))
                    else:
                        # 每个段落独立降级：段落超长才句子剪，短段落保持完整
                        for p in split_paragraphs(text):
                            if len(p) <= chunk_max:
                                chunks.append((heading, p))
                            else:
                                for s in split_sentences(p):
                                    chunks.append((heading, s))
            anchor = make_anchor(front, body) if chunks else ""

            for i, (heading, chunk_text) in enumerate(chunks):
                cid = f"{rel}::{i}"
                new_ids.append(cid)
                # 锚点只加首块：既有中文检索锚点，又不虚增全文件词频
                new_texts.append(anchor + chunk_text if i == 0 else chunk_text)
                new_metas.append({
                    "file": rel,
                    "heading": heading,
                    "title": front.get("title", ""),
                    "tags": front.get("tags", ""),
                    "chunk": str(i),
                    "anchor": anchor if i == 0 else "",
                })
            meta[rel] = {"hash": fhash, "chunks": len(chunks), "size": st.st_size, "mtime": st.st_mtime_ns}
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
            log(f"嵌入 {total_chunks} 个新块（{changed} 个文件变更，{unchanged} 个未变）...")
            emb_batches = []
            for start in range(0, total_chunks, EMBED_BATCH_SIZE):
                end = min(start + EMBED_BATCH_SIZE, total_chunks)
                emb_batches.append(encode_safe(new_texts[start:end]))
                update_progress(chunks_done=end,
                                message=f"嵌入 {end}/{total_chunks} 块（{changed} 文件变更）...")
            emb = _vstack(emb_batches)
            _release_cuda_cache()

        # 写锁包住全部写操作（delete/upsert/清理/save_meta）
        with write_lock():
            update_progress(phase="writing", message="写库与清理...")
            if full:
                log("全量重建：清空旧库后写入...")
                client.delete_collection(COLLECTION_NAME)
                collection = client.get_or_create_collection(
                    name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
                )

            if emb is not None:
                collection.upsert(ids=new_ids, embeddings=emb.tolist(), documents=new_texts, metadatas=new_metas)
            else:
                log(f"无变更（{unchanged} 个文件全部命中缓存）")

            # 精确清理：有效 id = meta 中每个文件按记录的块数生成。
            # 已删文件（不在 meta）与幽灵块（块数变少后超出 chunks 的旧 id）都会被清除。
            valid = set()
            for rel, info in meta.items():
                for i in range(info.get("chunks", 0)):
                    valid.add(f"{rel}::{i}")
            all_ids = collection.get(include=[])["ids"]
            stale = [i for i in all_ids if i not in valid]
            if stale:
                collection.delete(ids=stale)
                log(f"清理 {len(stale)} 个失效块")

            save_meta(meta)
            final_count = collection.count()
        update_progress(phase="done", message=f"完成。Chroma 现有 {final_count} 个块。", running=False)
        log(f"完成。Chroma 现有 {final_count} 个块。耗时 {time.time() - scan_start:.1f}s。")
    except Exception as e:
        progress_error(e)
        raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--full", action="store_true", help="全量重建，忽略增量")
    args = ap.parse_args()
    index_vault(args.vault, incremental=not args.full, full=args.full)
