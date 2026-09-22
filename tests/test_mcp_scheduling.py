"""test_mcp_scheduling.py — 多 Agent 并发调度与资源管理回归（问题59 / GOAL C2）。

风格对齐 audit_regression_test.py：标准库、逐用例 PASS/FAIL、`_run_all()` 运行器。
隔离：假加载器/假服务/临时目录，全程不碰真实模型/Chroma/真库/网络。
覆盖 B1/B3/B4/B5/B6/B7 + GPU_LOCK 单例 + CLI 跨进程警示（B2 代码侧）。

运行：.venv\\Scripts\\python tests\\test_mcp_scheduling.py（退出码 0=全绿）。
注意：本文件暂不进 tests/run.py SUITES——注册会把全量计数 14 改成 15，
会打破 GOAL C1 的 `14/14` 断言；C2 单独跑本文件。
"""
import importlib.machinery
import json
import shutil
import signal as _signal
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index  # noqa: E402
import gpu_arbiter  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r is not None:
        _r(encoding="utf-8", errors="replace")


PASS = 0
FAIL = 0
_FAILED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        _FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def test_gpu_lock_singleton_rlock():
    """C 总锁：单例 RLock（同线程可重入，navigate 持锁调 release 不自死锁）。"""
    ok("lock: 单例", isinstance(gpu_arbiter.GPU_LOCK, type(threading.RLock())))
    import gpu_arbiter as ga2
    ok("lock: 跨导入同一对象", ga2.GPU_LOCK is gpu_arbiter.GPU_LOCK)
    ok("lock: 有超时常量", gpu_arbiter.GPU_LOCK_ACQUIRE_TIMEOUT_S == 15.0)
    got = gpu_arbiter.GPU_LOCK.acquire(timeout=5)
    try:
        ok("lock: 可重入", gpu_arbiter.GPU_LOCK.acquire(blocking=False))
    finally:
        if got:
            gpu_arbiter.GPU_LOCK.release()
        try:
            gpu_arbiter.GPU_LOCK.release()
        except RuntimeError:
            pass


def test_corrupt_cache_falls_back_online():
    """B1：本地损坏（非 OSError）回退联网；两边都坏则给清理指引。"""
    def factory_online(mid, **kw):
        if kw.get("local_files_only"):
            raise ValueError("config.json truncated")
        return "ONLINE"

    try:
        r = index._load_pretrained(factory_online, "X/repo")
        ok("B1: 损坏缓存回退联网", r == "ONLINE", repr(r))
    except Exception as e:
        ok("B1: 损坏缓存回退联网", False, f"{type(e).__name__}: {e}")

    def factory_dead(mid, **kw):
        raise ValueError("bad")

    try:
        index._load_pretrained(factory_dead, "X/repo")
        ok("B1: 双坏给清理指引", False, "未抛异常")
    except RuntimeError as e:
        ok("B1: 双坏给清理指引", "快照" in str(e), str(e)[:120])
    except Exception as e:
        ok("B1: 双坏给清理指引", False, f"错异常 {type(e).__name__}: {e}")


def _patch_index_for_fake_load():
    saved = {n: getattr(index, n) for n in
             ("_model", "_device", "_load_model", "_cuda_ready",
              "_report_device", "_cooldown_cuda")}
    index._model = None
    index._device = "cpu"
    index._cuda_ready = lambda: False
    index._report_device = lambda *a, **k: None
    index._cooldown_cuda = lambda *a, **k: None
    return saved


def _restore_index(saved):
    for n, v in saved.items():
        setattr(index, n, v)


def test_get_model_single_flight():
    """B6：并发冷加载只载一份（GPU_LOCK 单飞 + 锁内双重检查）。"""
    saved = _patch_index_for_fake_load()
    state = {"n": 0}
    errs = []

    def fake_load(device):
        state["n"] += 1
        time.sleep(0.2)
        return (object(), device)

    index._load_model = fake_load
    try:
        def run():
            try:
                index.get_model()
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=run) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        ok("B6: 并发冷加载只载一份", state["n"] == 1 and not errs,
           f"loads={state['n']} errs={errs!r}")
        ok("B6: 复用同一对象", index._model is not None)
    finally:
        _restore_index(saved)


def test_release_during_load_safe():
    """B6 下半：加载途中释放不炸（串行化为 happens-before 任一顺序）。"""
    saved = _patch_index_for_fake_load()
    state = {"n": 0}
    errs = []

    def fake_load(device):
        state["n"] += 1
        time.sleep(1.0)
        return (object(), device)

    index._load_model = fake_load
    try:
        def run():
            try:
                index.get_model()
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.1)
        try:
            index.release_model()
            rel_err = None
        except Exception as e:  # noqa: BLE001
            rel_err = e
        t.join(timeout=30)
        try:
            m = index.get_model()
        except Exception as e:  # noqa: BLE001
            m = None
            errs.append(e)
        ok("B6: 加载途中释放不抛异常", not errs and rel_err is None,
           f"errs={errs!r} rel_err={rel_err!r}")
        ok("B6: 事后模型可用", m is not None, f"loads={state['n']}")
    finally:
        _restore_index(saved)


def _import_server_isolated():
    """导 server 但隔离 mcp SDK 的信号处理副作用（见 server_singleton_test 头注释）。

    返回 server 模块；调用方负责在 finally 里还原打过的补丁（本函数只动信号，
    且 import 后立刻还原）。
    """
    watched = [s for s in ("SIGINT", "SIGTERM", "SIGBREAK")
               if hasattr(_signal, s)]
    saved = {s: _signal.getsignal(getattr(_signal, s)) for s in watched}
    try:
        import server as _srv
        return _srv
    finally:
        for s, h in saved.items():
            try:
                _signal.signal(getattr(_signal, s), h)
            except Exception:
                pass


def test_navigate_vetoes_while_indexing():
    """B3：索引在跑时 navigate 不抢模型（veto），只查已活着的服务。

    真跑 navigate_knowledge（server 经信号隔离后导入），全程打桩：
    _index_running / resolve_entries / wemm_search / release_* / ensure/alive。
    """
    try:
        srv = _import_server_isolated()
    except ImportError as e:
        ok("B3: veto（server 可导入）", False, f"mcp 未安装？{e}")
        return
    releases = []
    ensures = []
    saved = {
        "running": srv._index_running,
        "resolve": srv.resolve_entries,
        "search": srv.wemm_search,
        "rr": srv.release_reranker,
        "rm": srv.release_model,
        "ensure": gpu_arbiter.ensure_server,
        "alive": gpu_arbiter.server_alive,
    }
    rows = [([("L", "a.pdf", "C:/v/a.pdf", 0, 0.9)], None)]
    try:
        srv.resolve_entries = lambda *a, **k: [{"name": "L"}]
        srv.wemm_search = lambda *a, **k: rows[0]
        srv.release_reranker = lambda: releases.append("r")
        srv.release_model = lambda: releases.append("m")
        gpu_arbiter.ensure_server = lambda *a, **k: (ensures.append(1), (True, "ok"))[1]
        # A：索引在跑 + 服务活着 → 不释放不拉起，直接查
        srv._index_running = lambda: True
        gpu_arbiter.server_alive = lambda url: True
        out = srv.navigate_knowledge("q")
        ok("B3: veto 不释放", releases == [], repr(releases))
        ok("B3: veto 不拉起", ensures == [], repr(ensures))
        ok("B3: veto 照常出结果", "a.pdf" in out, out[:200])
        # B：索引在跑 + 服务没活 → 回忙，不抢
        releases.clear()
        gpu_arbiter.server_alive = lambda url: False
        out = srv.navigate_knowledge("q")
        ok("B3: veto 无服务回忙", ("稍后重试" in out) and releases == [], out[:200])
        # C：索引没跑 → 照常让路 + 拉起 + 查
        releases.clear()
        ensures.clear()
        srv._index_running = lambda: False
        gpu_arbiter.server_alive = lambda url: True
        out = srv.navigate_knowledge("q")
        ok("B3: 空闲照常让路", releases != [], repr(releases))
        ok("B3: 空闲照常拉起", ensures != [], repr(ensures))
        ok("B3: 空闲照常出结果", "a.pdf" in out, out[:200])
    finally:
        srv._index_running = saved["running"]
        srv.resolve_entries = saved["resolve"]
        srv.wemm_search = saved["search"]
        srv.release_reranker = saved["rr"]
        srv.release_model = saved["rm"]
        gpu_arbiter.ensure_server = saved["ensure"]
        gpu_arbiter.server_alive = saved["alive"]


def test_auto_batch_probe_fail_open():
    """B4：CUDA 探针失败不绕过降级链（取保守上限继续走正常流程）。"""
    saved_mod = sys.modules.get("torch")
    saved_dev = index._device
    fake = types.ModuleType("torch")
    fake.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)

    class _Cuda:
        @staticmethod
        def mem_get_info():
            raise RuntimeError("CUDA context dead")

    fake.cuda = _Cuda()
    sys.modules["torch"] = fake
    index._device = "cuda"
    try:
        try:
            cap = index._auto_batch_size(32)
            ok("B4: 探针失败取保守上限", cap == 8, repr(cap))
        except Exception as e:  # noqa: BLE001
            ok("B4: 探针失败取保守上限", False, f"{type(e).__name__}: {e}")
    finally:
        if saved_mod is not None:
            sys.modules["torch"] = saved_mod
        else:
            sys.modules.pop("torch", None)
        index._device = saved_dev


def test_prune_keeps_base_meta():
    """B5：prune 存活集含 legacy 基线 index_meta.json（旧单库入口缓存不误删）。"""
    tmp = Path(tempfile.mkdtemp(prefix="rag-sched-prune-"))
    try:
        data_d = tmp / "data"
        cache_d = tmp / "cache"
        chroma_d = tmp / "chroma"
        data_d.mkdir(parents=True)
        cache_d.mkdir(parents=True)
        chroma_d.mkdir(parents=True)
        h = "d41d8cd98f00b204e9800998ecf8427e"
        (data_d / "index_meta.json").write_text(
            json.dumps({"f.pdf": {"hash": h}}), encoding="utf-8")
        (cache_d / f"{h}.md").write_text("old entry", encoding="utf-8")
        orphan = "e" * 32 + ".md"
        (cache_d / orphan).write_text("orphan", encoding="utf-8")
        n = index.prune_unreferenced_data(
            data_dir=data_d, cache_dir=cache_d, chroma_dir=chroma_d,
            entries=[{"name": "L"}], log=lambda *a, **k: None)
        ok("B5: base 指纹缓存保留", (cache_d / f"{h}.md").exists(), f"prune={n}")
        ok("B5: 真孤儿照常清理", not (cache_d / orphan).exists(), f"prune={n}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _import_mineru_stubbed():
    """mineru_server 顶层 import mineru 包（缺失则 sys.exit）——测试时预塞 stub。

    返回 (模块, 复原函数)。"""
    keys = ("mineru", "mineru.cli", "mineru.cli.api_client", "mineru_server")
    saved = {k: sys.modules.get(k) for k in keys}
    pkg = types.ModuleType("mineru")
    cli = types.ModuleType("mineru.cli")
    api = types.ModuleType("mineru.cli.api_client")

    class _StubInner:
        def __init__(self, *a, **k):
            self._server = None

    api.ReusableLocalAPIServer = _StubInner
    sys.modules["mineru"] = pkg
    sys.modules["mineru.cli"] = cli
    sys.modules["mineru.cli.api_client"] = api
    sys.modules.pop("mineru_server", None)
    import mineru_server as ms

    def _restore():
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v
            else:
                sys.modules.pop(k, None)

    return ms, _restore


def test_mineru_idle_respects_inflight():
    """B7：在途解析中不卸载；/evict 先拿 _API_LOCK 再拿 _INNER_LOCK（同序不死锁）。"""
    try:
        ms, restore_mod = _import_mineru_stubbed()
    except SystemExit as e:
        ok("B7: mineru_server 可导入", False, f"exit={e}")
        return
    try:
        saved = {n: getattr(ms, n) for n in
                 ("_active_requests", "_last_use", "_stop_inner_locked",
                  "_inner_alive")}
        calls = []
        ms._inner_alive = lambda: True
        ms._stop_inner_locked = lambda: calls.append(1)
        try:
            ms._active_requests = 1
            ms._last_use = 0.0
            ms._check_idle_unload()
            ok("B7: 在途不卸载", calls == [], repr(calls))
            ms._active_requests = 0
            ms._check_idle_unload()
            ok("B7: 空闲照常卸载", calls == [1], repr(calls))
        finally:
            for n, v in saved.items():
                setattr(ms, n, v)
        import inspect
        src = inspect.getsource(ms._Handler.do_POST)
        i_evict = src.index('"/evict"')
        i_api = src.index("with _API_LOCK:", i_evict)
        i_inner = src.index("with _INNER_LOCK:", i_api)
        ok("B7: evict 先API锁后INNER锁", i_evict < i_api < i_inner)
    finally:
        restore_mod()


def test_cli_warns_on_low_vram():
    """B2 代码侧：CLI 跨进程争用先警告（独立进程，before_serve 够不着 MCP 侧）。"""
    import wemm_indexer as wi
    saved_free = gpu_arbiter.vram_free_gb
    saved_log = wi.log
    msgs = []
    try:
        gpu_arbiter.vram_free_gb = lambda max_age=0.0: 1.0
        wi.log = lambda *a: msgs.append(" ".join(str(x) for x in a))
        wi._warn_if_vram_low()
        ok("B2: 低显存警告", any("错峰" in m for m in msgs), repr(msgs))
        msgs.clear()
        gpu_arbiter.vram_free_gb = lambda max_age=0.0: 7.0
        wi._warn_if_vram_low()
        ok("B2: 显存充足静默", msgs == [], repr(msgs))
    finally:
        gpu_arbiter.vram_free_gb = saved_free
        wi.log = saved_log


# ---------- 运行器 ----------

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            global FAIL
            FAIL += 1
            _FAILED.append(t.__name__)
            import traceback
            print(f"  FAIL  {t.__name__}  异常: {e}")
            traceback.print_exc()
    print(f"\n===== MCP Scheduling: {PASS} passed, {FAIL} failed =====")
    if _FAILED:
        print("失败用例: " + ", ".join(_FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
