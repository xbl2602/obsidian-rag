"""gpu_arbiter.py — GPU 显存仲裁（问题41：同一时刻只让一个模型驻留显存）。

背景：8GB 卡上 WeMM(5.1GB) 与 bge-m3+reranker(~3GB) 无法共存，同时在线 =
WDDM 溢出共享显存、整机性能骤降。本模块实现三件事：

  1. 显存探测：vram_free_gb()（torch 优先，nvidia-smi 兜底）；
  2. WEMM 服务生命周期：ensure_server() 按需拉起（幂等、分离进程、日志落盘）、
     evict_wemm() 检索优先抢占；
  3. 等待让路：wait_for_vram()（WEMM 加载前等 bge-m3 让出显存）。

**fail-open 铁律**：显存探测失败（无 torch 也无 nvidia-smi）返回 None，所有
调用方必须视作"无法判断 → 不阻塞不抢占"，绝不让仲裁本身卡死正常路径。
被 .venv（index.py/server.py）与全局 Python（wemm_server.py）共同 import，
因此本模块只允许标准库依赖。
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "data" / "wemm_server.pid"
LOG_FILE = ROOT / "data" / "wemm_server.log"

WEMM_MIN_VRAM_GB = 5.5   # WeMM-2B bf16 + 激活余量；低于此 WEMM 不进显存
BGE_MIN_VRAM_GB = 3.5    # bge-m3 fp16 + CUDA context + 批次激活余量


def vram_free_gb(max_age=5.0):
    """当前空闲显存（GB）。探测失败返回 None（fail-open）。

    torch 在 .venv 与全局 Python 都可用；nvidia-smi 作双保险（torch 不可用
    但驱动在的场景）。max_age 缓存：探测有开销，短窗内复用。
    """
    global _cache
    now = time.time()
    if _cache[1] and now - _cache[1] < max_age:
        return _cache[0]
    val = None
    try:
        import torch
        if torch.cuda.is_available():
            val = torch.cuda.mem_get_info()[0] / 2 ** 30
    except Exception:
        pass
    if val is None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=5)
            nums = [float(x) for x in out.stdout.decode("utf-8", "replace").split()]
            if nums:
                val = nums[0] / 1024.0
        except Exception:
            return None
    _cache = (val, now)
    return val


_cache = (None, 0.0)


def wait_for_vram(min_free_gb, timeout_s=900.0, poll_s=10.0, log=None):
    """阻塞等待空闲显存 ≥ min_free_gb。等到返回 True；超时返回 False。

    用于 WEMM 加载前：bge-m3 在线时显存不足，等检索侧空闲卸载（server
    空闲 10 分钟自动让路）或 evict 抢占完成。log 为 None 时静默。
    """
    deadline = time.time() + timeout_s
    announced = False
    while True:
        free = vram_free_gb(max_age=0.0)
        if free is None:
            return True  # fail-open：探测不到就放行
        if free >= min_free_gb:
            return True
        if time.time() >= deadline:
            return False
        if log is not None and not announced:
            announced = True
            log(f"空闲显存 {free:.1f}GB < 需求 {min_free_gb:.1f}GB，"
                f"等待其他模型让路（最多 {timeout_s:.0f}s）…")
        time.sleep(poll_s)


# ---- WEMM 服务生命周期 ----

def health(url, timeout=3.0):
    """探测 WEMM 服务；失败抛异常。返回 dict（loaded/model/dim/gpu_mem_gb…）。"""
    req = urllib.request.Request(url.rstrip("/") + "/health")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError("health 异常")
    return data


def server_alive(url):
    try:
        health(url)
        return True
    except Exception:
        return False


def evict_wemm(url, timeout=15.0):
    """请求 WEMM 立即卸载模型释放显存（检索优先抢占）。

    WEMM 正在编码时会等当前一条编完再卸——那一条的成果保留，其后批次由
    页索引按失败终态记账、下轮自动重试（问题39 的可重试语义兜底）。
    服务不在/请求失败返回 False（fail-open：绝不阻塞检索）。
    """
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/evict", data=b"{}",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def _pid_alive(pid):
    """进程是否存活（与 index._pid_alive 同款；本模块不能 import index——
    index 拖 chromadb，全局 Python 没有）。Windows 禁用 os.kill 语义同 index。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
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
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    import os
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _parse_port(url, default=9101):
    try:
        from urllib.parse import urlparse
        return urlparse(url).port or default
    except Exception:
        return default


def _wait_health(url, wait_s):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if server_alive(url):
            return True
        time.sleep(1.0)
    return False


def ensure_server(python_exe=None, url=None, wait_s=120.0, log=None):
    """按需拉起 WEMM 看图服务（幂等，问题41）。

    已在运行 → 直接 True；PID 记录的实例还活着（可能在加载）→ 只等不重拉；
    否则用全局 Python 分离拉起（日志 → data/wemm_server.log），等 health 就绪。
    返回 (ok, detail)。拉不起/等不到不抛异常，调用方给 AI 提示即可。
    """
    try:
        from config import CFG
        url = url or CFG.get("wemm_url") or "http://127.0.0.1:9101"
        python_exe = python_exe or CFG.get("wemm_python") or "python"
    except Exception:
        url = url or "http://127.0.0.1:9101"
        python_exe = python_exe or "python"

    if server_alive(url):
        return True, "看图服务已在运行"

    pid = _read_pid()
    if pid and _pid_alive(pid):
        # 有实例但 health 不通：可能在冷加载/端口未起，只等不重复拉起
        ok = _wait_health(url, wait_s)
        return (ok, "已有实例（PID %d）%s" % (pid, "已就绪" if ok else "等待超时"))

    LOG_FILE.parent.mkdir(exist_ok=True)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    try:
        # 子进程会复制句柄，父进程 with 关闭自己的那份——否则每次拉起泄漏一个句柄，
        # 且测试的临时目录会因句柄占用删不掉
        with open(LOG_FILE, "ab") as logf:
            proc = subprocess.Popen(
                [python_exe, str(ROOT / "wemm_server.py"),
                 "--port", str(_parse_port(url))],
                stdout=logf, stderr=logf, cwd=str(ROOT), **kwargs)
    except OSError as e:
        return False, "拉起失败（%s）：确认 wemm_python=%r 指向装了 torch 的全局 Python" % (
            type(e).__name__, python_exe)
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    ok = _wait_health(url, wait_s)
    if log is not None:
        log("WEMM 看图服务已按需拉起（PID %d）" % proc.pid if ok
            else "WEMM 看图服务拉起后未就绪，详见 %s" % LOG_FILE)
    return (ok, "已按需拉起并就绪（PID %d）" % proc.pid if ok
            else "已拉起但 %ds 内未就绪，详见 data/wemm_server.log" % wait_s)
