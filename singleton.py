"""singleton.py — 进程单例守卫（供 server.py 使用，独立成模块以便无 mcp 依赖的单测）。

背景：opencode 启动 MCP 时可能连续拉起多个 server 实例（观察到启动后 1s 内
双实例），双实例 = 双份模型常驻（~4GB）+ 索引写锁竞争。本模块用 PID 文件
保证同一时刻只有一个实例服务：后启动者探测到存活实例立即退出。

2026-08-15 重写：旧实现是"读 PID 文件 → 比较存活 → 写入"三步，无原子性。
opencode 双拉起时两个实例几乎同时启动，都能通过"文件不存在/对方未写"检查，
双双常驻（实测双进程并存，一个 5MB 僵尸空等）。现改为对 PID 文件本身加
非阻塞字节锁（复用 index 的跨平台锁原语）：抢到锁的实例写 PID 并持锁到进程
退出，后启动者拿不到锁立即退出——判定原子化，不再依赖检查-写入窗口。
锁文件用 r+b 模式打开（"a+b" 的 O_APPEND 会把 PID 追加到文件尾，seek(0)
写入无效，实测旧内容留在开头导致 PID 校验读到旧值）。

用法：
    from singleton import acquire_singleton
    if __name__ == "__main__":
        acquire_singleton()
        ...  // 进程退出时 atexit 自动释放锁并清理 PID 文件
"""
import atexit
import os
import sys
from pathlib import Path

from index import DATA_DIR, _lock_record_holder, _lock_try_acquire, _open_lock_file, _pid_alive

SERVER_PID_FILE = DATA_DIR / "server.pid"

_singleton_f = None  # 持有锁的文件对象（保持打开到进程退出）


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
    """退出清理：释放字节锁 + 仅当 PID 文件是本进程记录时才删除（不误删后来者的记录）。"""
    global _singleton_f
    pid_file = pid_file or SERVER_PID_FILE
    try:
        if _singleton_f is not None:
            _singleton_f.close()  # 关闭即释放文件锁（msvcrt/fcntl 锁随 fd 生命周期）
            _singleton_f = None
    except OSError:
        pass
    try:
        if Path(pid_file).exists():
            cur = int(Path(pid_file).read_text(encoding="utf-8").strip().split()[0])
            if cur == os.getpid():
                Path(pid_file).unlink()
    except (ValueError, OSError):
        pass


def acquire_singleton(pid_file=SERVER_PID_FILE):
    """单例守卫：文件锁原子判定。已有存活实例（持锁）→ 本进程立即退出。

    两段判定：
    1) 预检：PID 文件已记录"存活 PID"→ 视为已有实例（防御持锁进程死亡后
       锁自动释放、但文件记录的 PID 仍存活之类的陈旧残留场景）。
    2) 文件锁：对 PID 文件加非阻塞字节锁，抢不到锁说明其他实例持锁运行中。
       锁判定原子化——旧实现"读文件→比存活→写入"三步有竞态窗口，opencode
       双拉起时两个实例能同时通过检查双双常驻（实测双进程并存 4GB）。

    失败（锁原语不可用等极端情况）时放行继续启动，避免单例机制本身成为故障点。
    """
    global _singleton_f
    pid_file = pid_file or SERVER_PID_FILE
    try:
        raw = Path(pid_file).read_text(encoding="utf-8").strip().split()[0]
        if raw.isdigit() and int(raw) != os.getpid() and _pid_alive(int(raw)):
            print("检测到已有 server 实例运行，本实例退出（单例守卫）。", file=sys.stderr)
            sys.exit(0)
    except (ValueError, OSError):
        pass
    try:
        pid_file.parent.mkdir(exist_ok=True)
        f = _open_lock_file(pid_file)
        try:
            f.seek(0, 2)
            if f.tell() == 0:
                f.write(b"0")  # 保证 ≥1 字节，字节锁才有可锁范围（与 index.write_lock 同策略）
                f.flush()
        except OSError:
            pass  # 并发启动时对方可能已持锁锁住首字节，写失败不要紧，锁判定会兜底
        f.seek(0)
    except OSError as e:
        print(f"单例守卫失败（继续启动）：{e}", file=sys.stderr)
        return True
    try:
        _lock_try_acquire(f)
        _lock_record_holder(f)
    except OSError:
        try:
            f.close()
        except OSError:
            pass
        print("检测到已有 server 实例运行，本实例退出（单例守卫）。", file=sys.stderr)
        sys.exit(0)
    _singleton_f = f  # 持锁到进程退出；atexit 负责释放与清理
    atexit.register(release_singleton, pid_file)
    return True
