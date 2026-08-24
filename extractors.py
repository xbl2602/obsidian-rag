"""extractors.py — 多格式文档提取（DOCX + 文字层 PDF 直提；扫描件经 OCR 后端）。

单一入口 extract_to_markdown(path)：把二进制文档转成 Markdown 字符串，
交给既有的整条切块管线。设计红线（实施时不得偏离）：
  - 绝不抛异常：任何失败都折叠成 (None, reason)，reason ∈
    {"unreadable", "extract-failed", "empty", "scanned"}（"tbd" 由 index 层判定）；
  - 绝不写源目录：所有落盘只发生在 data/extract_cache 缓存目录；
  - 懒加载：import extractors 不引入 pymupdf/python-docx/requests，
    只索引 md/txt 的进程永远不会触碰这些库；
  - 扫描件 PDF（文字层覆盖率 < 0.5 页占比）按 pdf_scan_backend 路由：
      none         → (None, "scanned") 跳过并记终态（默认）
      mineru-cloud → MinerU 云端 API OCR，成功则照常入索引
  - 缓存键 = <字节md5>.<route>.v<EXTRACT_VERSION>（route 标记产出路径：
    local=本地直提 / ocr:mineru-cloud=云端 OCR），升提取器或换后端旧缓存
    天然失效；原子写（tmp 带 pid + os.replace）；写失败仅跳过缓存、照常返回
    结果；None 结果不写缓存。
"""
import hashlib
import os
import sys
import time
from pathlib import Path

# 单一事实来源：index 的后缀路由与 library 的 extensions 白名单校验都引用这里，
# 禁止在任何一处重新硬编码这份清单。
TEXT_EXTS = {"md", "txt"}
BINARY_EXTS = {"pdf", "docx"}
SUPPORTED_EXTS = TEXT_EXTS | BINARY_EXTS

EXTRACT_VERSION = 2  # 提取逻辑版本：v2 起缓存键含产出路由（local / ocr:后端）

DEFAULT_CACHE_DIR = Path(__file__).parent / "data" / "extract_cache"

# 扫描件 OCR 后端（config.pdf_scan_backend 的合法值）
SCAN_BACKENDS = ("none", "mineru-cloud", "mineru-local")

_MINERU_BASE = "https://mineru.net/api/v4"
_POLL_INTERVAL = 3.0  # 云端任务轮询间隔（秒）


def get_scan_backend():
    """当前生效的扫描件 OCR 后端（读 config，懒加载避免导入环）。非法值回退 none。"""
    try:
        from config import CFG
        b = str(CFG.get("pdf_scan_backend", "none")).lower()
        return b if b in SCAN_BACKENDS else "none"
    except Exception:
        return "none"


def current_backend_sig():
    """OCR 能力签名：进二进制终态条目的 xsrc 字段。

    签名变化（如 none→mineru-cloud、或补配了 api_key）= 能力变化，
    index 层据此对 scanned/extract-failed 终态自动重试转正。
    """
    b = get_scan_backend()
    has_key = False
    if b == "mineru-cloud":
        try:
            from config import CFG
            has_key = bool(str(CFG.get("mineru_api_key", "")).strip())
        except Exception:
            has_key = False
    return f"{b}:{'key' if has_key else 'nokey'}"

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


def _route_fs(route):
    """路由标签 → 文件系统安全片段（Windows 禁止文件名含冒号）。"""
    return str(route).replace(":", "-")


def _cache_path(key, route="local"):
    return get_cache_dir() / f"{key}.{_route_fs(route)}.v{EXTRACT_VERSION}.md"


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


def _cache_get(key, routes):
    """按候选路由顺序查缓存。同一文件的字节只会由一条路由成功产出，命中即真。

    返回 (markdown, 命中路由)；未命中返回 (None, None)。
    """
    for r in routes:
        try:
            text = _cache_path(key, r).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.strip():
            return text, r
    return None, None


def _cache_put(key, md, route="local"):
    if not _swept:
        _sweep_orphan_tmp()
    try:
        d = get_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.{_route_fs(route)}.{os.getpid()}.tmp"
        tmp.write_text(md, encoding="utf-8")
        os.replace(tmp, _cache_path(key, route))
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
    # 缓存按产出路由分键；同一文件的字节只会由一条路由成功产出（确定性），
    # 因此 pdf 恒查全部路由、与当前配置无关——后端切换不丢历史成果
    routes = ["ocr:mineru-cloud", "ocr:mineru-local", "local"] if ext == "pdf" else ["local"]
    try:
        key = _file_md5(path)
    except OSError:
        return None, "unreadable"
    hit, _hit_route = _cache_get(key, routes)
    if hit is not None:
        return hit, ""
    if ext == "docx":
        md, reason, route = _extract_docx(path)
    elif ext == "pdf":
        md, reason, route = _extract_pdf(path)
    else:
        return None, "extract-failed"  # 路由层只应送 BINARY_EXTS，防御分支
    if md is None:
        return None, reason
    md = md.strip()
    if not md:
        return None, "empty"
    _cache_put(key, md, route or "local")
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
        return None, "extract-failed", "local"
    try:
        d = docx.Document(str(path))
    except Exception as e:
        _warn_once(f"docx-open:{e.__class__.__name__}",
                   f"DOCX 打开失败（损坏或加密？按提取失败处理）：{e}")
        return None, "extract-failed", "local"
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
        return None, "extract-failed", "local"
    md = "\n".join(lines).strip()
    if not md:
        return None, "empty", "local"
    return md, "", "local"


# ---------- PDF ----------

# 单页至少含这么多字符才算「有文字层的页」：页码/水印级别的零星字符不算，
# 但一两行的真实短内容要算（10 字符以下仍判无文字层）
_TEXT_PAGE_MIN_CHARS = 10
# 有文字层的页占比达到此值的 PDF 按「文字层 PDF」处理，否则判扫描件
_TEXT_PAGE_RATIO = 0.5


def _extract_pdf(path):
    """PDF 提取路由：文字层 → 本地直提；扫描件 → 按 pdf_scan_backend 分发。

    返回 (markdown|None, reason, route)：route 标记产出路径，进缓存键
    （local=本地直提 / ocr:mineru-cloud=云端 OCR），换后端旧缓存天然失效。
    """
    try:
        import pymupdf
        import pymupdf4llm
    except ImportError:
        _warn_once("pdf-dep", "pymupdf/pymupdf4llm 未安装，PDF 提取不可用"
                              "（pip install pymupdf pymupdf4llm）")
        return None, "extract-failed", "local"
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        _warn_once(f"pdf-open:{e.__class__.__name__}",
                   f"PDF 打开失败（损坏或加密？按提取失败处理）：{e}")
        return None, "extract-failed", "local"
    try:
        if doc.page_count <= 0:
            return None, "extract-failed", "local"

        def _page_text(pg):
            # get_text("text") 运行时恒为 str；stub 标了联合返回值，这里收窄
            t = pg.get_text("text")
            return t if isinstance(t, str) else ""

        text_pages = sum(1 for pg in doc
                         if len(_page_text(pg).strip()) >= _TEXT_PAGE_MIN_CHARS)
        if text_pages / doc.page_count < _TEXT_PAGE_RATIO:
            # 扫描件：按配置路由 OCR 后端
            backend = get_scan_backend()
            if backend == "none":
                _warn_once("scanned",
                           "发现扫描件 PDF（无文字层），当前未启用 OCR 后端，已跳过"
                           "（可在设置中把 pdf_scan_backend 设为 mineru-cloud）")
                return None, "scanned", f"ocr:{backend}"
            if backend == "mineru-local":
                _warn_once("mineru-local",
                           "mineru-local（本地部署）属 R3b 尚未支持，扫描件继续跳过")
                return None, "scanned", f"ocr:{backend}"
            md, reason = _mineru_cloud_extract(path)
            return md, reason, f"ocr:{backend}"
        try:
            out = pymupdf4llm.to_markdown(doc)
        except Exception as e:
            _warn_once(f"pdf-conv:{e.__class__.__name__}",
                       f"PDF 转 Markdown 失败（按提取失败处理）：{e}")
            return None, "extract-failed", "local"
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
    return (md, "", "local") if md.strip() else (None, "empty", "local")


# ---------- 扫描件 OCR：MinerU 云端 API（R3a） ----------

def _mineru_cloud_extract(path):
    """MinerU 云端 API：申请批任务 → 预签名 PUT 上传 → 轮询 → 下载 zip 取正文 .md。

    契约（mineru.net 官方文档，2026-08）：POST {BASE}/file-protocol/batch 携带
    Bearer Token 取得 batch_id 与预签名上传地址；PUT 上传原始字节（无鉴权头）；
    GET {BASE}/file-protocol/batch/{id} 轮询 extract_result[0].state 至 done，
    取 full_zip_url 下载 zip，正文取其中最大的 .md。
    所有异常折叠为 (None, reason)；任何日志绝不包含 api_key 与响应体全文。
    """
    try:
        import requests
    except ImportError:
        _warn_once("requests-dep",
                   "requests 未安装，云端 OCR 不可用（pip install requests）")
        return None, "extract-failed"
    from config import CFG
    api_key = str(CFG.get("mineru_api_key", "")).strip()
    if not api_key:
        _warn_once("mineru-key",
                   "pdf_scan_backend=mineru-cloud 但 mineru_api_key 为空，扫描件继续跳过")
        return None, "scanned"
    try:
        budget = float(CFG.get("mineru_timeout_seconds") or 600)
    except Exception:
        budget = 600.0
    deadline = time.monotonic() + max(30.0, budget)

    fname = Path(path).name
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.post(
            f"{_MINERU_BASE}/file-protocol/batch",
            headers={**headers, "Content-Type": "application/json"},
            json={"enable_formula": True, "enable_table": True,
                  "files": [{"name": fname, "is_ocr": True, "data_id": "doc"}]},
            timeout=30)
        data = resp.json()
        if resp.status_code != 200 or data.get("code") not in (0, 200):
            raise RuntimeError(f"任务提交异常 HTTP {resp.status_code} code={data.get('code')}")
        batch_id = (data.get("data") or {}).get("batch_id")
        put_url = ((data.get("data") or {}).get("file_urls") or [None])[0]
        if not batch_id or not put_url:
            raise RuntimeError("提交响应缺少 batch_id/file_urls")
        put = requests.put(put_url, data=Path(path).read_bytes(), timeout=120)
        if put.status_code not in (200, 201):
            raise RuntimeError(f"文件上传失败 HTTP {put.status_code}")

        zip_url = None
        while time.monotonic() < deadline:
            poll = requests.get(
                f"{_MINERU_BASE}/file-protocol/batch/{batch_id}",
                headers=headers, timeout=30)
            pdata = poll.json()
            if poll.status_code != 200 or pdata.get("code") not in (0, 200):
                raise RuntimeError(f"轮询异常 HTTP {poll.status_code}")
            items = (pdata.get("data") or {}).get("extract_result") or []
            state = items[0].get("state") if items else "pending"
            if state == "done":
                zip_url = items[0].get("full_zip_url")
                break
            if state in ("failed", "error"):
                raise RuntimeError(f"云端解析失败 state={state}")
            time.sleep(_POLL_INTERVAL)
        if not zip_url:
            raise RuntimeError("轮询超时（mineru_timeout_seconds）")

        zreq = requests.get(zip_url, timeout=120)
        if zreq.status_code != 200:
            raise RuntimeError(f"结果下载失败 HTTP {zreq.status_code}")
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(zreq.content))
        mds = [zi for zi in zf.infolist() if zi.filename.lower().endswith(".md")]
        if not mds:
            raise RuntimeError("结果包中没有 .md")
        best = max(mds, key=lambda zi: zi.file_size)  # 最大者=正文主文档
        md = zf.read(best).decode("utf-8", "replace")
        return (md, "") if md.strip() else (None, "empty")
    except Exception as e:
        # 只记类型与摘要：api_key 与响应体全文绝不进日志
        _warn_once(f"mineru:{e.__class__.__name__}",
                   f"MinerU 云端 OCR 失败（按提取失败处理，待重试）：{e}")
        return None, "extract-failed"
