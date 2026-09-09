"""test_gpu_arbiter.py — GPU 显存仲裁（问题41）回归测试。

风格对齐：标准库、非 pytest、逐用例 PASS/FAIL、`_run_all()` 运行器。
全程不碰真实 GPU 服务/进程：探测函数与 HTTP 全部 mock，落盘路径重定向临时目录。

覆盖：
  - vram_free_gb：探测失败返回 None（fail-open）——仲裁绝不阻塞正常路径
  - wait_for_vram：充足立即放行 / 不足超时返回 False / 探测失败 fail-open 放行
  - evict_wemm：正常应答 True / 异常折叠 False（绝不抛异常）
  - ensure_server：已在运行不重复拉起 / PID 存活只等不重拉 / 冷启动拉起并等就绪 /
    拉起失败折叠为 (False, 提示)
  - _parse_port / _pid_alive 边界
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gpu_arbiter as ga  # noqa: E402

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


def test_vram_free_gb_fail_open():
    """探测失败必须返回 None（fail-open 铁律），绝不抛异常。"""
    with patch.object(ga, "subprocess") as fake_sp, \
         patch.dict("sys.modules", {"torch": None}):
        fake_sp.run.side_effect = OSError("no nvidia-smi")
        ga._cache = (None, 0.0)
        v = ga.vram_free_gb(max_age=0.0)
    ok("probe: 探测失败 → None", v is None, repr(v))
    with patch.object(ga, "subprocess") as fake_sp, \
         patch.dict("sys.modules", {"torch": None}):
        fake_sp.run.return_value.stdout = b"6133\n"
        ga._cache = (None, 0.0)
        v2 = ga.vram_free_gb(max_age=0.0)
    ok("probe: nvidia-smi 兜底解析", v2 == 6133 / 1024.0, repr(v2))


def test_wait_for_vram_branches():
    """充足立即放行；不足超时 False；探测失败 fail-open 放行。"""
    with patch.object(ga, "vram_free_gb", return_value=7.9):
        ok("wait: 显存充足立即 True", ga.wait_for_vram(5.5, timeout_s=1, poll_s=0.05))
    with patch.object(ga, "vram_free_gb", return_value=2.0):
        t0 = __import__("time").time()
        r = ga.wait_for_vram(5.5, timeout_s=0.3, poll_s=0.05)
        ok("wait: 不足超时 False", r is False, repr(r))
        ok("wait: 超时按时返回", __import__("time").time() - t0 < 2.0)
    with patch.object(ga, "vram_free_gb", return_value=None):
        ok("wait: 探测失败 fail-open True",
           ga.wait_for_vram(5.5, timeout_s=1, poll_s=0.05))


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._p.encode("utf-8")


def test_evict_wemm_ok_and_fold():
    with patch.object(ga, "urllib") as fake_u:
        fake_u.request.urlopen.return_value = _FakeResp('{"ok": true}')
        ok("evict: 正常应答 True", ga.evict_wemm("http://127.0.0.1:9101") is True)
    with patch.object(ga, "urllib") as fake_u:
        fake_u.request.urlopen.side_effect = OSError("refused")
        ok("evict: 异常折叠 False", ga.evict_wemm("http://127.0.0.1:9101") is False)


def test_server_alive():
    with patch.object(ga, "urllib") as fake_u:
        fake_u.request.urlopen.return_value = _FakeResp('{"ok": true}')
        ok("alive: health ok → True", ga.server_alive("http://x:1") is True)
    with patch.object(ga, "urllib") as fake_u:
        fake_u.request.urlopen.side_effect = OSError("down")
        ok("alive: 不可达 → False", ga.server_alive("http://x:1") is False)


def test_parse_port():
    ok("port: 标准地址", ga._parse_port("http://127.0.0.1:9101") == 9101)
    ok("port: 无端口回默认", ga._parse_port("http://127.0.0.1") == 9101)
    ok("port: 垃圾输入回默认", ga._parse_port("garbage") == 9101)


def test_pid_alive_edges():
    ok("pid: 非法输入 False", ga._pid_alive(None) is False)
    ok("pid: 负数 False", ga._pid_alive(-1) is False)
    ok("pid: 0 False", ga._pid_alive(0) is False)
    # 不 assert 真实 pid 的存活与否（环境相关），只验证不抛异常且有返回值
    r = ga._pid_alive(999999999)
    ok("pid: 大 PID 返回布尔不抛异常", isinstance(r, bool))


def test_ensure_server_already_alive():
    """服务活着 → 直接 True，绝不重复拉起。"""
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ga, "server_alive", return_value=True), \
             patch.object(ga, "subprocess") as fake_sp:
            spawned = []
            fake_sp.Popen.side_effect = lambda *a, **k: spawned.append(a)
            okd, detail = ga.ensure_server(url="http://127.0.0.1:9101")
            ok("ensure: 已运行 → True", okd is True, detail)
            ok("ensure: 已运行不拉起", not spawned)
            ok("ensure: detail 人话", "已在运行" in detail, detail)


def test_ensure_server_spawns_when_dead():
    """无实例 → 拉起并等就绪；PID 落盘。"""
    with tempfile.TemporaryDirectory() as td:
        pidfile = Path(td) / "wemm_server.pid"
        calls = []

        def fake_popen(args, **kw):
            calls.append(args)
            class _P:
                pid = 4242
            return _P()

        alive_calls = {"n": 0}

        def fake_alive(url):
            # 问题46 起 ensure 内有早夭轮询，health 会被多探几次：
            # 用状态函数而非固定列表（第 2 次起就绪），耗尽即 StopIteration
            # 的写法与新轮询不兼容。
            alive_calls["n"] += 1
            return alive_calls["n"] >= 2

        with patch.object(ga, "PID_FILE", pidfile), \
             patch.object(ga, "server_alive", side_effect=fake_alive), \
             patch.object(ga, "_pid_alive", return_value=False), \
             patch.object(ga, "subprocess") as fake_sp, \
             patch.object(ga, "LOG_FILE", Path(td) / "s.log"):
            fake_sp.Popen.side_effect = fake_popen
            okd, detail = ga.ensure_server(url="http://127.0.0.1:9101",
                                           python_exe="globalpy", wait_s=2)
            ok("ensure: 冷启动拉起 → True", okd is True, detail)
            ok("ensure: 调用了全局 Python + wemm_server.py",
               len(calls) == 1 and calls[0][0] == "globalpy"
               and calls[0][1].endswith("wemm_server.py"), str(calls))
            ok("ensure: PID 记录落盘", pidfile.exists()
               and pidfile.read_text().strip() == "4242")
            ok("ensure: detail 带 PID", "4242" in detail, detail)


def test_ensure_server_existing_pid_waits_not_spawns():
    """PID 存活但 health 未通 → 只等不重拉（防双实例）。"""
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ga, "PID_FILE", Path(td) / "p.pid"), \
             patch.object(ga, "server_alive", return_value=False), \
             patch.object(ga, "_read_pid", return_value=777), \
             patch.object(ga, "_pid_alive", return_value=True), \
             patch.object(ga, "_wait_health", return_value=False), \
             patch.object(ga, "subprocess") as fake_sp:
            spawned = []
            fake_sp.Popen.side_effect = lambda *a, **k: spawned.append(a)
            okd, detail = ga.ensure_server(url="http://127.0.0.1:9101", wait_s=0.5)
            ok("ensure: 存活实例只等不拉", not spawned)
            ok("ensure: 等待超时如实报告", okd is False and "777" in detail, detail)


def test_ensure_server_spawn_failure_folds():
    """拉起抛 OSError（解释器不存在等）→ 折叠为 (False, 提示)，绝不抛异常。"""
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ga, "PID_FILE", Path(td) / "p.pid"), \
             patch.object(ga, "server_alive", return_value=False), \
             patch.object(ga, "_read_pid", return_value=None), \
             patch.object(ga, "subprocess") as fake_sp, \
             patch.object(ga, "LOG_FILE", Path(td) / "s.log"):
            fake_sp.Popen.side_effect = OSError("winerror 2")
            okd, detail = ga.ensure_server(url="http://127.0.0.1:9101",
                                           python_exe="no-such-python", wait_s=1)
            ok("ensure: 拉起失败折叠 False", okd is False)
            ok("ensure: 提示指向 wemm_python", "wemm_python" in detail, detail)


def test_root_points_at_project():
    """ROOT 必须指项目根（问题46）：本文件与 wemm_server.py 同级，只能 .parent。

    2026-09-08 实测 .parent.parent 把 PID/日志/拉起脚本全指到上级目录，
    子进程瞬间死亡、调用方空等 120s——索引"启动后无响应、0 CPU、0 显存"。
    """
    import pathlib
    ok("root: 与模块同级目录", ga.ROOT == pathlib.Path(ga.__file__).resolve().parent,
       str(ga.ROOT))
    ok("root: 下有 wemm_server.py", (ga.ROOT / "wemm_server.py").is_file())
    ok("root: 下有 index.py", (ga.ROOT / "index.py").is_file())
    ok("root: PID/日志落在项目 data", ga.PID_FILE.parent == ga.ROOT / "data"
       and ga.LOG_FILE.parent == ga.ROOT / "data",
       f"{ga.PID_FILE} / {ga.LOG_FILE}")


def test_ensure_server_missing_script_fails_fast():
    """启动脚本缺失 → 立刻 False，不 spawn、不空等（问题46 同因加固）。"""
    import time as _t
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ga, "ROOT", Path(td)), \
             patch.object(ga, "PID_FILE", Path(td) / "p.pid"), \
             patch.object(ga, "server_alive", return_value=False), \
             patch.object(ga, "subprocess") as fake_sp:
            spawned = []
            fake_sp.Popen.side_effect = lambda *a, **k: spawned.append(a)
            t0 = _t.time()
            okd, detail = ga.ensure_server(url="http://127.0.0.1:9101",
                                           python_exe="globalpy", wait_s=120)
            dt = _t.time() - t0
            ok("ensure: 脚本缺失立刻 False", okd is False, detail)
            ok("ensure: 脚本缺失不 spawn", not spawned)
            ok("ensure: 脚本缺失不空等", dt < 10, "%.1fs" % dt)


def test_ensure_server_early_exit_fails_fast():
    """子进程秒退（早夭）→ 5s 内 False，不等满 wait_s（问题46 同因加固）。"""
    import time as _t
    with tempfile.TemporaryDirectory() as td:
        class _Dead:
            pid = 9999

            def poll(self):
                return 1  # 已退出

        with patch.object(ga, "PID_FILE", Path(td) / "p.pid"), \
             patch.object(ga, "LOG_FILE", Path(td) / "s.log"), \
             patch.object(ga, "server_alive", return_value=False), \
             patch.object(ga, "subprocess") as fake_sp:
            fake_sp.Popen.side_effect = lambda *a, **k: _Dead()
            t0 = _t.time()
            okd, detail = ga.ensure_server(url="http://127.0.0.1:9101",
                                           python_exe="globalpy", wait_s=120)
            dt = _t.time() - t0
            ok("ensure: 早夭 False", okd is False, detail)
            ok("ensure: 早夭不等满 120s", dt < 30, "%.1fs" % dt)


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            _FAILED.append(t.__name__)
            import traceback
            print(f"  FAIL  {t.__name__}  异常: {e}")
            traceback.print_exc()
    print(f"\n===== GPU Arbiter: {PASS} passed, {FAIL} failed =====")
    if _FAILED:
        print("失败用例: " + ", ".join(_FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_run_all())
