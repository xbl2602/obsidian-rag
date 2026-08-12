"""server_singleton_test.py — 单例守卫（singleton.py）纯逻辑单元测试。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\server_singleton_test.py
注意：不 import server（其 mcp SDK 依赖会改动信号处理，干扰测试进程）。
"""
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from singleton import acquire_singleton, pid_alive, release_singleton  # noqa: E402

# opencode 环境会周期性向子进程注入 Ctrl+C（KeyboardInterrupt 出现在随机位置，
# 与代码无关）：测试进程忽略 SIGINT 免疫之。
signal.signal(signal.SIGINT, signal.SIG_IGN)


def test_acquire_creates_pid_file():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "server.pid"
        assert acquire_singleton(f)
        assert f.exists()
        assert str(os.getpid()) == f.read_text(encoding="utf-8").split()[0]
        release_singleton(f)
        assert not f.exists()


def test_acquire_blocked_by_live_instance():
    """子进程验证：已有存活实例时 acquire_singleton 以 0 退出且不破坏记录。

    不用同进程捕获 SystemExit（Python 3.14 在 opencode 环境捕获 sys.exit(0)
    后，后续 write_text 会偶发 KeyboardInterrupt——生产场景退出即终，无影响）。
    """
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "server.pid"
        f.write_text(f"{os.getppid()} 2026-08-12T10:00:00", encoding="utf-8")
        code = (
            "import signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN);"
            "sys.path.insert(0, r'C:\\Users\\xbl26\\projects\\obsidian-rag');"
            f"from singleton import acquire_singleton;"
            f"acquire_singleton(r'{f}')"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert r.returncode == 0, r.stderr[-300:]
        assert "已有 server 实例" in r.stderr
        assert f.exists()  # 原有记录不被破坏


def test_acquire_overwrites_stale():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "server.pid"
        f.write_text("999999999 2026-08-12T09:00:00", encoding="utf-8")
        assert acquire_singleton(f)
        assert str(os.getpid()) == f.read_text(encoding="utf-8").split()[0]
        release_singleton(f)
        assert not f.exists()


def test_release_keeps_others_pid():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "server.pid"
        f.write_text(f"{os.getppid()} 2026-08-12T10:00:00", encoding="utf-8")
        release_singleton(f)  # 不是本进程记录 → 不删
        assert f.exists()


def test_pid_alive_self():
    assert pid_alive(os.getpid())
    assert not pid_alive(999999999)


def _run_all():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
