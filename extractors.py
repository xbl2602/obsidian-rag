"""extractors.py — 多格式文档提取（R1 范围：DOCX + 文字层 PDF；扫描件 OCR 计划末轮）。

单一入口 extract_to_markdown(path)：把二进制文档转成 Markdown 字符串，
交给既有的整条切块管线。设计红线（实施时不得偏离）：
  - 绝不抛异常：任何失败都折叠成 (None, reason)，reason ∈
    {"unreadable", "extract-failed", "empty", "scanned"}（"tbd" 由 index 层判定）；
  - 绝不写源目录：所有落盘只发生在 data/extract_cache 缓存目录；
  - 懒加载：import extractors 不引入 pymupdf/python-docx，
    只索引 md/txt 的进程永远不会触碰这两个库；
  - 扫描件 PDF（文字层覆盖率 < 0.5 页占比）在 R1 明确不支持：
    返回 (None, "scanned")，由 index 记持久化终态防重建死循环；
  - 缓存键 = <文件原始字节 md5>.v<EXTRACT_VERSION>：升提取器旧缓存天然失效；
    原子写（tmp 带 pid + os.replace）；写失败仅跳过缓存、照常返回结果；
    None 结果不写缓存（失败重试廉价，且不给永久失败钉死）。
"""
import hashlib
import os
import sys
from pathlib import Path

# 单一事实来源：index 的后缀路由与 library 的 extensions 白名单校验都引用这里，
# 禁止在任何一处重新硬编码这份清单。
TEXT_EXTS = {"md", "txt"}
BINARY_EXTS = {"pdf", "docx"}
SUPPORTED_EXTS = TEXT_EXTS | BINARY_EXTS

EXTRACT_VERSION = 1  # 提取逻辑版本：升级后旧缓存自动失效（进缓存键）

DEFAULT_CACHE_DIR = Path(__file__).parent / "data" / "extract_cache"

_cache_dir = None  # 测试注入覆盖；None = 用默认
_swept = False     # 孤儿 tmp 清扫每进程至多一次
_warned = set()


def log(*args):
    print("[extractors]", *args, file=sys.stderr)


def _warn_once(key, msg):
    """同一类告警每进程只打一次（几百个扫描件不该刷几百行同样的提示）。"""
    if key in _warned:
        return
    _warned.add(key)
    log(msg)


def set_cache_dir(p):
    """注入缓存目录（测试隔离用）；传 None 恢复默认。"""
    global _cache_dir, _swept
    _cache_dir = Path(p) if p else None
    _swept = False


def get_cache_dir():
    return _cache_dir or DEFAULT_CACHE_DIR


def _cache_path(key):
    return get_cache_dir() / f"{key}.v{EXTRACT_VERSION}.md"


def _sweep_orphan_tmp():
    """清扫 >24h 的孤儿 *.tmp（崩溃残留的原子写半成品）。失败静默（纯卫生措施）。"""
    global _swept
    _swept = True
    try:
        import time
        cutoff = time.time() - 24 * 3600
        for t in get_cache_dir().glob("*.tmp"):
            try:
                if t.stat().st_mtime < cutoff:
                    t.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _cache_get(key):
    try:
        text = _cache_path(key).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def _cache_put(key, md):
    if not _swept:
        _sweep_orphan_tmp()
    try:
        d = get_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.v{EXTRACT_VERSION}.{os.getpid()}.tmp"
        tmp.write_text(md, encoding="utf-8")
        os.replace(tmp, _cache_path(key))
    except OSError as e:
        log(f"提取缓存写失败（跳过缓存，不影响本次结果）：{e}")


def _file_md5(path):
    """流式读原始字节算 MD5（对大文件不整载）。OSError 上抛由调用方处理。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_to_markdown(path):
    """唯一入口：二进制文档 → Markdown。返回 (markdown|None, reason)。

    成功：(str, "")。失败：(None, reason)，reason 取值见模块 docstring。
    本函数绝不抛异常、绝不写源目录。
    """
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    try:
        key = _file_md5(path)
    except OSError:
        return None, "unreadable"
    hit = _cache_get(key)
    if hit is not None:
        return hit, ""
    if ext == "docx":
        md, reason = _extract_docx(path)
    elif ext == "pdf":
        md, reason = _extract_pdf(path)
    else:
        return None, "extract-failed"  # 路由层只应送 BINARY_EXTS，防御分支
    if md is None:
        return None, reason
    md = md.strip()
    if not md:
        return None, "empty"
    _cache_put(key, md)
    return md, ""


# ---------- DOCX ----------

_HEADING_STYLE_RE = None  # 惰性编译占位（保持模块导入零重量）


def _heading_level(style_name):
    """样式名 → 标题级别（1-3）；非标题样式返回 0。

    兼容英文模板（Heading 1）与中文 Word 模板（标题 1）；Title 视为 H1；
    >3 级钳到 ###（切块管线只认 # 到 ### 三级）。
    """
    import re
    if not style_name:
        return 0
    name = style_name.strip()
    if name.lower() == "title":
        return 1
    m = re.match(r"^(?:Heading|标题)\s*(\d+)$", name, re.IGNORECASE)
    if not m:
        return 0
    return max(1, min(3, int(m.group(1))))


def _table_md(table):
    """python-docx Table → Markdown 管道表（首行作表头）。

    单元格内换行压成空格、竖线转义（管道表语法要求）；行内多段落合并。
    """
    rows = table.rows
    if not rows:
        return ""
    out = []
    ncols = 0
    for i, row in enumerate(rows):
        cells = [(c.text or "").replace("\r\n", " ").replace("\n", " ").strip()
                 .replace("|", "\\|") for c in row.cells]
        if not any(cells):
            continue
        ncols = max(ncols, len(cells))
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("|" + "---|" * len(cells))
    return "\n".join(out)


def _extract_docx(path):
    """DOCX → Markdown。按 body 子元素顺序遍历（段落/表格混排保序）。"""
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        _warn_once("docx-dep", "python-docx 未安装，DOCX 提取不可用"
                               "（pip install python-docx）")
        return None, "extract-failed"
    try:
        d = docx.Document(str(path))
    except Exception as e:
        _warn_once(f"docx-open:{e.__class__.__name__}",
                   f"DOCX 打开失败（损坏或加密？按提取失败处理）：{e}")
        return None, "extract-failed"
    lines = []
    try:
        for child in d.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
            if tag == "p":
                para = Paragraph(child, d)
                text = (para.text or "").strip()
                if not text:
                    continue
                try:
                    lvl = _heading_level(getattr(para.style, "name", ""))
                except Exception:
                    lvl = 0
                lines.append(("#" * lvl + " " + text) if lvl else text)
                lines.append("")
            elif tag == "tbl":
                md = _table_md(Table(child, d))
                if md:
                    lines.extend([md, ""])
    except Exception as e:
        _warn_once(f"docx-walk:{e.__class__.__name__}",
                   f"DOCX 正文遍历异常（按提取失败处理）：{e}")
        return None, "extract-failed"
    md = "\n".join(lines).strip()
    if not md:
        return None, "empty"
    return md, ""


# ---------- PDF ----------

# 单页至少含这么多字符才算「有文字层的页」：页码/水印级别的零星字符不算，
# 但一两行的真实短内容要算（10 字符以下仍判无文字层）
_TEXT_PAGE_MIN_CHARS = 10
# 有文字层的页占比达到此值的 PDF 按「文字层 PDF」处理，否则判扫描件
_TEXT_PAGE_RATIO = 0.5


def _extract_pdf(path):
    """文字层 PDF → Markdown；扫描件 → (None, "scanned")。

    判定：fitz 逐页数「有效文字页」，占比 ≥ 0.5 走 pymupdf4llm 转 Markdown；
    否则视为扫描件（R1 无 OCR 后端，明确不支持并给出指引）。
    """
    try:
        import pymupdf
        import pymupdf4llm
    except ImportError:
        _warn_once("pdf-dep", "pymupdf/pymupdf4llm 未安装，PDF 提取不可用"
                              "（pip install pymupdf pymupdf4llm）")
        return None, "extract-failed"
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        _warn_once(f"pdf-open:{e.__class__.__name__}",
                   f"PDF 打开失败（损坏或加密？按提取失败处理）：{e}")
        return None, "extract-failed"
    try:
        if doc.page_count <= 0:
            return None, "extract-failed"

        def _page_text(pg):
            # get_text("text") 运行时恒为 str；stub 标了联合返回值，这里收窄
            t = pg.get_text("text")
            return t if isinstance(t, str) else ""

        text_pages = sum(1 for pg in doc
                         if len(_page_text(pg).strip()) >= _TEXT_PAGE_MIN_CHARS)
        if text_pages / doc.page_count < _TEXT_PAGE_RATIO:
            _warn_once("scanned",
                       "发现扫描件 PDF（无文字层），R1 暂不支持 OCR，已跳过"
                       "（OCR 计划末轮接入 MinerU）")
            return None, "scanned"
        try:
            out = pymupdf4llm.to_markdown(doc)
        except Exception as e:
            _warn_once(f"pdf-conv:{e.__class__.__name__}",
                       f"PDF 转 Markdown 失败（按提取失败处理）：{e}")
            return None, "extract-failed"
    finally:
        doc.close()
    if isinstance(out, str):
        md = out
    elif isinstance(out, list):
        # 兼容不同版本返回形态：list[str]（整页串）或 list[dict]（page_chunks 带
        # 元数据的页记录，正文在 "text" 键）
        md = "\n".join(
            item if isinstance(item, str)
            else str(item.get("text", "")) if isinstance(item, dict)
            else str(item)
            for item in out
        )
    else:
        md = str(out)
    return (md, "") if md.strip() else (None, "empty")
