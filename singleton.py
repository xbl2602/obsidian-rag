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

from index import DATA_DIR

SERVER_PID_FILE = DATA_DIR / "server.pid"


def pid_alive(pid):
    """进程是否存活（signal 0 探测，与 index.py 同一策略）。"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
