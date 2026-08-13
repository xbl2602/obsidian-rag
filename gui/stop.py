"""stop.py — 停止语义搜索控制台（GUI），整树终止 + 命令行兜底匹配。

背景：flet 桌面应用是双进程（父子 pythonw 命令行相同），且 PID 探测
（os.kill(pid,0)）在 pythonw 上不可靠。本脚本按两层策略终止：
1. PID 文件记录的 PID（存在时）→ taskkill /T 整树；
2. 兜底：WMI 扫描命令行含 gui/app.py 的所有 python/pythonw 进程全杀
   （覆盖 PID 文件缺失/误判场景）。
并删除 PID 文件。python.exe 启动会带 CMD 黑窗，建议用 pythonw 启动。

用法（AI/CLI 均可）：
    python gui/stop.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DATA_DIR  # noqa: E402

GUI_PID_FILE = DATA_DIR / "gui.pid"

_IS_WINDOWS = os.name == "nt"


def read_pid_file():
    """读 PID 文件；缺失/损坏返回 None。"""
    try:
        if GUI_PID_FILE.exists():
            return int(GUI_PID_FILE.read_text(encoding="utf-8").strip().split()[0])
    except (ValueError, OSError):
        pass
    return None


def wmi_gui_processes():
    """WMI 扫描与 GUI 相关的进程（命令行特征匹配）：

    - python*：命令行含 gui/app.py（GUI 主/子 python 进程）；
    - flet.exe：Flutter 渲染窗口进程，命令行含本项目根目录（第三个参数
      是 assets 路径）。flet.exe 由 python 子进程 spawn，父进程死亡后成
      孤儿，PID 文件匹配不到，必须按命令行兜底。
    """
    out = []
    proj = str(Path(__file__).resolve().parent.parent).replace("\\", "\\\\")
    try:
        import subprocess as sp
        ps = sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { ($_.Name -like 'python*' -and "
             "$_.CommandLine -match 'gui[\\\\/]app\\.py') -or "
             "($_.Name -eq 'flet.exe' -and "
             "$_.CommandLine -match '%s') } | "
             "ForEach-Object { Write-Output $_.ProcessId }" % proj],
            capture_output=True, text=True, timeout=20)
        for line in ps.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
    except Exception:
        pass
    return out


def taskkill(pid):
    """taskkill /T /F 终止整棵进程树；成功返回 True。"""
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def cleanup_pid_file(pid=None):
    """删除 PID 文件（仅当无 pid 参数或记录一致时）。"""
    try:
        if GUI_PID_FILE.exists():
            if pid is None:
                GUI_PID_FILE.unlink()
                return
            cur = int(GUI_PID_FILE.read_text(encoding="utf-8").strip().split()[0])
            if cur == pid:
                GUI_PID_FILE.unlink()
    except (ValueError, OSError):
        pass


def main():
    pid_file_pid = read_pid_file()
    targets = []
    if pid_file_pid:
        targets.append(pid_file_pid)
    found = wmi_gui_processes()
    for pid in found:
        if pid not in targets:
            targets.append(pid)

    if not targets:
        print("未发现运行中的 GUI 进程（PID 文件缺失且命令行无匹配）。")
        cleanup_pid_file()
        return 0

    print("准备终止：%s" % ", ".join(str(p) for p in targets))
    killed = 0
    for pid in targets:
        if taskkill(pid):
            killed += 1
    time.sleep(1.0)
    left = wmi_gui_processes()
    cleanup_pid_file(pid_file_pid)
    if left:
        print("警告：仍有进程残留：%s。请手动在任务管理器结束。" % ", ".join(str(p) for p in left))
        return 1
    print("已停止（%d 个进程树，无残留）。" % killed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
