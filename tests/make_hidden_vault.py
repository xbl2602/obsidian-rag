"""make_hidden_vault.py — 生成固定隐藏测试库 tests/hidden-vault/（标准库 + pymupdf/python-docx）。

用法：python tests/make_hidden_vault.py
幂等：每次全量重造，内容确定性（时间戳不写进文件内容）。

隐身契约：
- 本目录永远不注册进 libraries.json，生产索引/检索只认注册表路径，
  因此正常索引与检索都扫不到它，只有测试显式指向它。
- 新增文件枚举必须走 index.collect_md_files 漏斗，禁止私自扫盘（问题44红线）。

覆盖清单（每种至少 1 个）：
- 笔记.md：frontmatter / [[双链]] / 围栏代码块里的 # / 死图链 / “免费证书”
  （verify_export_import 的检索词，隐藏库必须命中）
- plain.txt / empty.md（空文件终态）/ 深路径 20-Projects/深路径笔记.md
- 文字层.pdf（2 页真文字，含“免费证书”）/ 扫描件.pdf（空白无文字层）/
  混合课件.pdf（文字页+空白页混装）/ 大写.PDF / 青苹果菜单.pdf（中文名）
- 表格文档.docx（标题层级+管道表+单元格换行）/ 空文档.docx /
  大写.Docx / 坏文档.docx（非 zip）/ 坏文件.pdf（非 pdf）
"""
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent / "hidden-vault"

NOTE_MD = """---
title: 隐藏库笔记
tags: [测试, 免费证书]
---

# 隐藏库笔记

免费证书的申请流程见下文，用于 verify 检索词命中。

裸双链保留目标词：[[深路径笔记]]。

```python
# 这行在围栏代码块里，不是标题
print("hi")
```

# 真实小节

正文段落：混合检索与重排。

死图链（应被清洗）：![gone](http://example.com/gone.png)

![本地图](a.png)
"""

DEEP_MD = """# 深路径笔记

被双链指向的目标，内容随意。
"""


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_text_pdf(path, pages, lines):
    import pymupdf
    d = pymupdf.open()
    for i in range(pages):
        pg = d.new_page()
        pg.insert_text((72, 72), lines[i % len(lines)].format(i=i + 1))
    d.save(str(path))
    d.close()


def _make_blank_pdf(path, pages=1):
    import pymupdf
    d = pymupdf.open()
    for _ in range(pages):
        d.new_page()
    d.save(str(path))
    d.close()


def _make_table_docx(path):
    import docx
    d = docx.Document()
    d.add_heading("发动机总览", level=1)
    d.add_paragraph("概述段落。免费证书相关说明。")
    d.add_heading("燃烧室", level=2)
    d.add_paragraph("细节段落。")
    d.add_heading("深层标题", level=4)  # >3 级应钳到 ###
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "参数"
    t.cell(0, 1).text = "数值"
    t.cell(1, 0).text = "a|b"
    t.cell(1, 1).text = "多\n行"
    d.save(str(path))


def main():
    VAULT.mkdir(parents=True, exist_ok=True)
    # 文本类
    _write_text(VAULT / "笔记.md", NOTE_MD)
    _write_text(VAULT / "plain.txt", "纯文本文件，用于 txt 路由。免费证书。\n")
    _write_text(VAULT / "empty.md", "")
    _write_text(VAULT / "20-Projects" / "深路径笔记.md", DEEP_MD)
    # PDF 类
    _make_text_pdf(VAULT / "文字层.pdf", 2,
                   ["免费证书申请指南 page {i}.", "Fluent mixing model basics page {i}."])
    _make_blank_pdf(VAULT / "扫描件.pdf")
    import shutil
    import pymupdf
    # 混合课件：2 文字页 + 1 空白页
    d = pymupdf.open()
    for i in range(2):
        pg = d.new_page()
        pg.insert_text((72, 72), f"Mixed text page {i + 1}.")
    d.new_page()
    d.save(str(VAULT / "混合课件.pdf"))
    d.close()
    shutil.copy2(VAULT / "文字层.pdf", VAULT / "大写.PDF")
    _make_text_pdf(VAULT / "青苹果菜单.pdf", 1, ["青苹果菜单 page {i}."])
    # DOCX 类
    _make_table_docx(VAULT / "表格文档.docx")
    import docx
    docx.Document().save(str(VAULT / "空文档.docx"))
    shutil.copy2(VAULT / "表格文档.docx", VAULT / "大写.Docx")
    (VAULT / "坏文档.docx").write_bytes(b"PK\x03\x04 not really a zip")
    (VAULT / "坏文件.pdf").write_bytes(b"%PDF-1.7 broken not really a pdf")
    names = sorted(p.relative_to(VAULT).as_posix() for p in VAULT.rglob("*") if p.is_file())
    print(f"hidden-vault 就绪：{VAULT}（{len(names)} 个文件）")
    for n in names:
        print(f"  - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
