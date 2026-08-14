"""singleton.py — 进程单例守卫（供 server.py 使用，独立成模块以便无 mcp 依赖的单测）。

背景：opencode 启动 MCP 时可能连续拉起多个 server 实例（观察到启动后 1s 内
双实例），双实例 = 双份模型常驻（~4GB）+ 索引写锁竞争。本模块用 PID 文件
保证同一时刻只有一个实例服务：后启动者探测到存活实例立即退出。

用法：
    from singleton import acquire_singleton
    if __name__ == "__main__":
        acquire_singleton()
        ...  // 进程退出时 atexit 自动清理 PID 文件
"""
import atexit
import os
import sys
from datetime import datetime
from pathlib import Path

from index import DATA_DIR, _pid_alive

SERVER_PID_FILE = DATA_DIR / "server.pid"


def pid_alive(pid):
    """进程是否存活（复用 index._pid_alive 的跨平台安全探测）。

    2026-08-14（审计 F5）：这里原本自己写了一份 os.kill(pid, 0)。在 Windows 上
    CPython 的 os.kill 对非 CTRL_* 信号一律 OpenProcess + TerminateProcess，
    于是"单例守卫"会先杀掉正在服务的那个 server，然后本进程再 sys.exit(0)——
    两个都没了，MCP 直接不可用。改为复用 index 里的只读探测（Windows 走
    OpenProcess(SYNCHRONIZE) + WaitForSingleObject）。
    """
    return _pid_alive(pid)


def release_singleton(pid_file=SERVER_PID_FILE):
    """退出清理：仅当 PID 文件是本进程记录时才删除（不误删后来者的记录）。"""
    pid_file = pid_file or SERVER_PID_FILE
    try:
        if Path(pid_file).exists():
            cur = int(Path(pid_file).read_text(encoding="utf-8").strip().split()[0])
            if cur == os.getpid():
                Path(pid_file).unlink()
    except (ValueError, OSError):
        pass


def acquire_singleton(pid_file=SERVER_PID_FILE):
    """单例守卫：已有存活实例则退出；否则记录本进程 PID 并在退出时清理。"""
    pid_file = pid_file or SERVER_PID_FILE
    try:
        p = Path(pid_file)
        p.parent.mkdir(exist_ok=True)
        if p.exists():
            try:
                other = int(p.read_text(encoding="utf-8").strip().split()[0])
            except (ValueError, OSError):
                other = None
            if other and other != os.getpid() and pid_alive(other):
                print(f"检测到已有 server 实例运行（PID {other}），本实例退出（单例守卫）。",
                      file=sys.stderr)
                sys.exit(0)
        p.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}",
                     encoding="utf-8")
        atexit.register(release_singleton, pid_file)
        return True
    except OSError as e:
        print(f"单例守卫失败（继续启动）：{e}", file=sys.stderr)
        return True
