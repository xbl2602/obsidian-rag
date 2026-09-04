"""patch_dropdown_fix.py v2 — 行级安全替换：Dropdown 事件名 + 按钮 label 字段。"""
from pathlib import Path

p = Path("gui/widgets.py")
t = p.read_text(encoding="utf-8")

old_dd = """                     ft.DropdownOption(key="mineru-cloud", text="MinerU 云端 OCR")],
            on_change=lambda _e: self._refresh_hint())"""
new_dd = """                     ft.DropdownOption(key="mineru-cloud", text="MinerU 云端 OCR")])
        # flet 0.86：Dropdown 事件名为 on_select（构造器不接受 on_change）
        self._dd_backend.on_select = lambda _e: self._refresh_hint()"""
assert t.count(old_dd) == 1, f"Dropdown 锚点 count={t.count(old_dd)}"
t = t.replace(old_dd, new_dd)

n1 = t.count('self._btn_run.text = "提取中…"')
n2 = t.count('self._btn_run.text = "开始提取"')
assert n1 == 1 and n2 == 3, f"text 赋值点异常：{n1}/{n2}"
t = t.replace('self._btn_run.text = "提取中…"',
              'self._btn_run.content = "提取中…"')
t = t.replace('self._btn_run.text = "开始提取"',
              'self._btn_run.content = "开始提取"')

assert 'self._btn_run.text' not in t, "仍有 btn_run.text 残留"
assert t.count("self._dd_backend.on_select = ") == 1
compile(t, "gui/widgets.py", "exec")
p.write_text(t, encoding="utf-8", newline="")
print(f"OK：dd×1、提取中×{n1}、开始提取×{n2}；行数 {len(t.splitlines())}")
