"""worker.py — 索引子进程管理：spawn、实时捕获输出、状态轮询、日志落盘。

索引任务跑在独立子进程（python index.py），与 GUI 进程解耦：
- 子进程崩溃不影响 GUI；GUI 关闭不影响子进程（任务继续跑完）。
- 输出捕获为内存环形缓冲（最近 LOG_MAX_LINES 行）+ 追加落盘 data/gui_index.log。
"""
import datetime
import os
import subprocess
import sys
import threading
from pathlib import Path

from store import PROJECT_DIR

LOG_FILE = PROJECT_DIR / "data" / "gui_index.log"
LOG_MAX_LINES = 1000


class IndexWorker:
    def __init__(self, on_output=None, on_exit=None):
        """on_output(line)：新日志行回调；on_exit(returncode)：进程退出回调。"""
        self.proc = None  # type: subprocess.Popen | None
        self.lines = []
        self._lock = threading.Lock()
        self.on_output = on_output
        self.on_exit = on_exit

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, full=False, library=""):
        """启动索引子进程（追加到现有日志文件）。已在运行时返回 False。

        library：空 = 全部库（index.py 默认）；否则 --library <名> 只索引该库。
        """
        if self.running:
            return False
        cmd = [str(sys.executable), "index.py"]
        if library:
            cmd += ["--library", library]
        if full:
            cmd += ["--full"]
        if os.name == "nt":
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        self._stamp(self._fmt("── 索引任务启动（pid=%s, 全量=%s%s）"
                              % (self.proc.pid, full,
                                 (", 库=%s" % library) if library else "")))
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._waiter, daemon=True).start()
        return True

    def _reader(self):
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line:
                self._stamp(line)

    def _waiter(self):
        if not self.proc:
            return
        rc = self.proc.wait()
        self._stamp(self._fmt("── 索引任务结束（退出码 %s）" % rc))
        if self.on_exit:
            self.on_exit(rc)

    def _stamp(self, line):
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > LOG_MAX_LINES:
                del self.lines[: len(self.lines) - LOG_MAX_LINES]
            if self.on_output:
                self.on_output(line)
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _fmt(text):
        return "%s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), text)

    def lines_since(self, cursor):
        """返回 cursor 之后的新行（列表 + 新游标）。cursor=None 返回全部。"""
        with self._lock:
            if cursor is None:
                return list(self.lines), len(self.lines)
            return self.lines[cursor:], len(self.lines)


def read_history():
    """读历史日志文件（存在时）。"""
    try:
        if LOG_FILE.exists():
            return LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        pass
    return []
