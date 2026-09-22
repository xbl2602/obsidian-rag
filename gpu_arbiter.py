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
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# 注意：本文件就在项目根目录（与 wemm_server.py 同级），ROOT 只能取
# .parent 一层。gui/*/tests/* 等子目录模块才用 .parent.parent——2026-09-08
# 实测本处曾误写 .parent.parent，PID/日志/拉起脚本路径全部指到上级目录：
# 拉起的子进程瞬间 "can't open file ...wemm_server.py" 死亡，调用方却空等
# 120s（每库×每文件），索引表现为"启动后无响应、0 CPU、0 显存"。
ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / "data" / "wemm_server.pid"
LOG_FILE = ROOT / "data" / "wemm_server.log"

WEMM_MIN_VRAM_GB = 5.5   # WeMM-2B bf16 + 激活余量；低于此 WEMM 不进显存
BGE_MIN_VRAM_GB = 3.5    # bge-m3 fp16 + CUDA context + 批次激活余量


# GPU 驻留变更总锁（问题59，多 Agent 并发调度：C 方案全局互斥）。
#
# 语义：同进程内一切"改变显存里住着谁"的操作——bge-m3 加载/释放、reranker
# 释放、WEMM/MinerU 的 evict、看图服务拉起前的让路判定——都必须先拿这把锁。
# 纯查询（向量检索、页库查询、已拉起服务的编码）不拿锁，天然可并发。
# 用 RLock（同线程可重入）：navigate 持锁做"查索引是否在跑 + 释放"原子
# 判定，其间调 release_* 会再次拿锁；plain Lock 会自死锁。
# 持有期纪律：只包"判定 + 快速变更"，绝不包 ensure_server 长等待（120s）；
# 拿不到锁的请求走降级路径（只查已活着的服务 / 回忙），不得无限等。
GPU_LOCK = threading.RLock()

# 请求路径拿锁的最长等待（秒）：超时 → 视为"此刻不适合抢显存"，走降级。
GPU_LOCK_ACQUIRE_TIMEOUT_S = 15.0


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

    # 启动脚本先验：脚本缺失时 Popen 照样"成功"（起的是解释器），子进程
    # 瞬间死亡，之后空等满 wait_s 毫无意义——2026-09-08 的 ROOT 指错即此类。
    script = ROOT / "wemm_server.py"
    if not script.is_file():
        return False, "无法拉起看图服务：启动脚本缺失（%s），请检查项目目录完整性" % script

    LOG_FILE.parent.mkdir(exist_ok=True)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    try:
        # 子进程会复制句柄，父进程 with 关闭自己的那份——否则每次拉起泄漏一个句柄，
        # 且测试的临时目录会因句柄占用删不掉
        with open(LOG_FILE, "ab") as logf:
            proc = subprocess.Popen(
                [python_exe, str(script),
                 "--port", str(_parse_port(url))],
                stdout=logf, stderr=logf, cwd=str(ROOT), **kwargs)
    except OSError as e:
        return False, "拉起失败（%s）：确认 wemm_python=%r 指向装了 torch 的全局 Python" % (
            type(e).__name__, python_exe)
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    # 早夭检查：合理解释器/脚本问题会让子进程几秒内退出——此时等满 wait_s
    # 纯属空耗（一次拉起即 120s 假死）。正常冷启动（torch import 数十秒）
    # 进程一直存活，不受此分支影响；之后仍走完整 _wait_health。
    _poll = getattr(proc, "poll", None)
    for _ in range(50):
        if server_alive(url):
            break
        try:
            dead = _poll() is not None if callable(_poll) else False
        except Exception:
            dead = False
        if dead:
            msg = ("看图服务进程启动后 5s 内退出（解释器/依赖/脚本问题），"
                   "详见 %s" % LOG_FILE)
            if log is not None:
                log(msg)
            return False, msg
        time.sleep(0.1)
    ok = _wait_health(url, wait_s)
    if log is not None:
        log("WEMM 看图服务已按需拉起（PID %d）" % proc.pid if ok
            else "WEMM 看图服务拉起后未就绪，详见 %s" % LOG_FILE)
    return (ok, "已按需拉起并就绪（PID %d）" % proc.pid if ok
            else "已拉起但 %ds 内未就绪，详见 data/wemm_server.log" % wait_s)


# ---- MinerU 本地解析服务（R3b） ----
# 与 WEMM 同构：按需拉起（幂等、分离进程、日志落盘）、用完即卸、双向抢占。
# 三点不同：① 默认端口 9102，被占时自动顺延（用户拍板）；② python 解释器是
# uv tool 的隔离环境（非全局 Python），路径可配置、可自动探测；③ HF_HOME 只
# 作用于子进程环境（C 盘 15GB 现有 HF 缓存原样不动，模型下到 E:\models\hf）。

MINERU_MIN_VRAM_GB = 4.5  # pipeline 后端约 4GB + 0.5 余量（待冒烟实测校准）
MINERU_PID_FILE = ROOT / "data" / "mineru_server.pid"
MINERU_LOG_FILE = ROOT / "data" / "mineru_server.log"
MINERU_PORT_TRIES = 3     # 端口顺延次数：base、base+1、base+2


def _port_in_use(port):
    """127.0.0.1:port 是否已被监听（连接成功=被占；拒绝=空闲）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _resolve_mineru_python(explicit=None):
    """mineru tool 环境的 python.exe。explicit（config.mineru_python）优先；
    空则按 uv tool 默认落点自动探测；都找不到返回 None（调用方报错提示安装）。"""
    if explicit and Path(str(explicit)).is_file():
        return str(explicit)
    cands = []
    try:
        appdata = os.environ.get("APPDATA")
        if appdata:
            cands.append(Path(appdata) / "uv" / "tools" / "mineru" / "Scripts"
                         / "python.exe")
        home = Path.home()
        cands.append(home / ".local" / "share" / "uv" / "tools" / "mineru"
                     / "bin" / "python")
        cands.append(home / ".local" / "share" / "uv" / "tools" / "mineru"
                     / "Scripts" / "python.exe")
    except Exception:
        pass
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except Exception:
            continue
    return None


def _mineru_env():
    """子进程环境：os.environ 全量继承（checker E1：禁整块替换，丢 PATH 必死），
    再增量覆盖 HF_HOME（模型目录作用域隔离，见 E2）。"""
    env = os.environ.copy()
    if not env.get("HF_HOME") and not env.get("HF_HUB_CACHE"):
        # 默认模型家：E:\models\hf（C 盘现有 15GB 缓存不动）。目录不存在就建，
        # 建不起也不阻塞（回退默认缓存，拉起日志里警告一次）。
        default = Path("E:/models/hf")
        try:
            default.mkdir(parents=True, exist_ok=True)
            env["HF_HOME"] = str(default)
        except Exception:
            pass
    return env


def _acquire_ensure_lock(timeout_s=30.0):
    """拿 MinerU 拉起跨进程锁（check-and-launch 之间不许第二个进程插队开壳）。

    生产实测：一次重建触发两次并发 ensure，开出两个同端口外壳（allow_reuse
    下共存、pid 文件互踩、显存 double）。Windows 用 msvcrt.locking 非阻塞轮询；
    持有者崩溃时系统自动释放锁，无 stale 死锁。返回持锁文件对象（调用方负责
    解锁关闭），超时/失败返回 None（调用方只做只读复用检查，不拉起）。
    """
    lock_path = ROOT / "data" / "mineru_ensure.lock"
    try:
        lock_path.parent.mkdir(exist_ok=True)
        f = open(lock_path, "a+b")
    except OSError:
        return None
    if sys.platform == "win32":
        try:
            import msvcrt
            deadline = time.time() + timeout_s
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    return f
                except OSError:
                    if time.time() >= deadline:
                        try:
                            f.close()
                        except Exception:
                            pass
                        return None
                    time.sleep(0.2)
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            return None
    try:
        import fcntl
        deadline = time.time() + timeout_s
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                if time.time() >= deadline:
                    try:
                        f.close()
                    except Exception:
                        pass
                    return None
                time.sleep(0.2)
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        return None


def _release_ensure_lock(f):
    if f is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            f.close()
        except Exception:
            pass


def evict_mineru(url, timeout=15.0):
    """请求 MinerU 停内服务释放显存（双向抢占，checker B1：bge/WEMM 加载前调用）。

    正在解析时会等当前一份走完再停——那一份的成果保留。服务不在/请求失败
    返回 False（fail-open：绝不阻塞调用方）。
    """
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/evict", data=b"{}",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def _read_mineru_pid():
    try:
        return int(MINERU_PID_FILE.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def ensure_mineru(python_exe=None, url=None, wait_s=120.0, log=None):
    """按需拉起 MinerU 本地解析服务（幂等，R3b）。

    端口顺延（用户拍板）：base → base+1 → base+2，逐个试：
      health 通 = 已有实例，直接用；端口空闲 = 在此拉起；端口被占但 health
      不通 = 被别的东西占着，换下一个。全部失败返回 (False, None, detail)。
    返回 (ok, url_used|None, detail)。拉不起/等不到不抛异常（fail-open：
    调用方回退记 scanned 终态，下轮重试）。
    """
    try:
        from config import CFG
        url = url or CFG.get("mineru_local_url") or "http://127.0.0.1:9102"
        python_exe = python_exe or CFG.get("mineru_python") or None
        try:
            max_pages = int(CFG.get("mineru_local_max_pages") or 200)
        except (TypeError, ValueError):
            max_pages = 200
    except Exception:
        url = url or "http://127.0.0.1:9102"
        max_pages = 200

    script = ROOT / "mineru_server.py"
    if not script.is_file():
        return False, None, "无法拉起本地解析服务：启动脚本缺失（%s）" % script

    resolved_py = _resolve_mineru_python(python_exe)
    if not resolved_py:
        return False, None, ("找不到 mineru tool 环境的 Python（mineru_python 未配且自动"
                             "探测失败）：请先跑 uv tool install --python 3.12 -U "
                             "\"mineru[all]\"，详见 AI_GUIDE")

    try:
        from urllib.parse import urlparse
        base_port = urlparse(url).port or 9102
        host = urlparse(url).hostname or "127.0.0.1"
    except Exception:
        base_port, host = 9102, "127.0.0.1"

    last_detail = "未知错误"
    lock = _acquire_ensure_lock(timeout_s=30.0)
    try:
        if lock is None:
            # 锁超时：别家正在拉起，只做只读复用检查，绝不自己再开一个
            for i in range(MINERU_PORT_TRIES):
                try_url = "http://%s:%d" % (host, base_port + i)
                if server_alive(try_url):
                    return True, try_url, "本地解析服务已在运行（%s）" % try_url
            return False, None, "拉起锁等待超时且未见可用实例（可能别家拉起失败），请重试"
        for i in range(MINERU_PORT_TRIES):
            port = base_port + i
            try_url = "http://%s:%d" % (host, port)
            if server_alive(try_url):
                return True, try_url, "本地解析服务已在运行（%s）" % try_url
            if _port_in_use(port):
                last_detail = "%s 端口被占（非 MinerU 服务），顺延" % try_url
                continue
            ok, detail = _launch_mineru(resolved_py, script, port, try_url,
                                       max_pages, wait_s, log)
            if ok:
                return True, try_url, detail
            last_detail = detail
            # 拉起失败换下一个端口再试（当前端口可能处于 TIME_WAIT 等瞬态）
        return False, None, "本地解析服务拉起失败（已试 %d 个端口）：%s" % (
            MINERU_PORT_TRIES, last_detail)
    finally:
        _release_ensure_lock(lock)


def _launch_mineru(python_exe, script, port, url, max_pages, wait_s, log):
    """在指定空闲端口拉起一次。返回 (ok, detail)。"""
    pid = _read_mineru_pid()
    if pid and _pid_alive(pid):
        # 有实例但 health 不通：可能在冷加载，只等不重复拉起
        ok = _wait_health(url, wait_s)
        return (ok, "已有实例（PID %d）%s" % (pid, "已就绪" if ok else "等待超时"))

    MINERU_LOG_FILE.parent.mkdir(exist_ok=True)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    try:
        with open(MINERU_LOG_FILE, "ab") as logf:
            proc = subprocess.Popen(
                [python_exe, str(script),
                 "--port", str(port),
                 "--max-pages", str(max_pages)],
                stdout=logf, stderr=logf, cwd=str(ROOT),
                env=_mineru_env(), **kwargs)
    except OSError as e:
        return False, "拉起失败（%s）：确认 mineru_python 指向 uv tool 环境" % type(e).__name__
    try:
        MINERU_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    _poll = getattr(proc, "poll", None)
    for _ in range(50):
        if server_alive(url):
            break
        try:
            dead = _poll() is not None if callable(_poll) else False
        except Exception:
            dead = False
        if dead:
            msg = ("本地解析服务进程启动后 5s 内退出（解释器/依赖/脚本问题），"
                   "详见 %s" % MINERU_LOG_FILE)
            if log is not None:
                log(msg)
            return False, msg
        time.sleep(0.1)
    ok = _wait_health(url, wait_s)
    if log is not None:
        log("MinerU 本地解析服务已按需拉起（PID %d，%s）" % (proc.pid, url) if ok
            else "MinerU 本地解析服务拉起后未就绪，详见 %s" % MINERU_LOG_FILE)
    return (ok, "已按需拉起并就绪（PID %d，%s）" % (proc.pid, url) if ok
            else "已拉起但 %ds 内未就绪，详见 data/mineru_server.log" % wait_s)
