"""check_flet_kwargs.py — 对照运行时签名校验试验台用到的构造器 kwargs。"""
import inspect

import flet as ft


def params(cls):
    try:
        sig = inspect.signature(cls.__init__)
        ps = set(sig.parameters)
        has_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        return ps, has_kw
    except Exception as e:
        return {"ERR:" + str(e)}, False


CHECKS = [
    (ft.Dropdown, ["value", "width", "options", "on_change"]),
    (ft.Checkbox, ["label", "value", "disabled", "on_change"]),
    (ft.Markdown, ["value", "selectable", "extension_set"]),
    (ft.ProgressBar, ["value", "visible", "bar_height", "color", "bgcolor"]),
    (ft.TextField, ["value", "multiline", "read_only", "border", "filled",
                    "text_size", "expand", "text_style"]),
    (ft.FilePicker, []),
]

for cls, kws in CHECKS:
    ps, has_kw = params(cls)
    bad = [k for k in kws if k not in ps and not has_kw]
    print(f"{cls.__name__}: 未知参数={bad if bad else '无'}  (**kwargs={has_kw})")

print()
print("Dropdown 事件字段:", [a for a in dir(ft.Dropdown) if a.startswith("on_")])
print("DropdownOption:", str(inspect.signature(ft.DropdownOption.__init__))[:180])
ps_text, _ = params(ft.Text)
print("Text: max_lines", "max_lines" in ps_text, "| overflow", "overflow" in ps_text,
      "| expand", "expand" in ps_text)
ps_row, _ = params(ft.Row)
print("Row: wrap", "wrap" in ps_row, "| spacing", "spacing" in ps_row)
ps_col, _ = params(ft.Column)
print("Column: scroll", "scroll" in ps_col, "| expand", "expand" in ps_col)
ps_btn, _ = params(ft.FilledButton)
print("FilledButton: text", "text" in ps_btn, "| disabled", "disabled" in ps_btn,
      "| icon", "icon" in ps_btn)
ps_tb, _ = params(ft.TextButton)
print("TextButton: text", "text" in ps_tb)
ps_ob, _ = params(ft.OutlinedButton)
print("OutlinedButton: tooltip", "tooltip" in ps_ob, "| icon", "icon" in ps_ob)
ps_cont, _ = params(ft.Container)
print("Container: visible", "visible" in ps_cont, "| expand", "expand" in ps_cont,
      "| bgcolor", "bgcolor" in ps_cont)
ps_al, _ = params(ft.AlertDialog)
print("AlertDialog: modal/actions/actions_alignment/shape:",
      all(k in ps_al for k in ("modal", "actions", "actions_alignment", "shape")))
