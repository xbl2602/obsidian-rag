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
  - 有文字层 PDF 按 pdf_text_backend 路由：
      local        → 本地 pymupdf4llm 直提（默认）
      mineru-cloud → 送 MinerU 云端换更准的版面/表格识别（is_ocr=False，不为已有文字重复
                      付 OCR 的钱）
  - 缓存键 = <字节md5>.<route>.v<EXTRACT_VERSION>（route 标记产出路径：
    local=本地直提 / ocr:mineru-cloud=扫描件云端OCR / mineru-text=文字层送MinerU换结构
    识别），换后端旧缓存天然失效；原子写（tmp 带 pid + os.replace）；写失败仅跳过缓存、
    照常返回结果；None 结果不写缓存。
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

EXTRACT_VERSION = 3  # 提取逻辑版本：v2 起缓存键含产出路由（local / ocr:后端 / mineru-text）；
                      # v3：MinerU 云端请求体新增 model_version 参数（问题33），旧缓存产出
                      # 用的是未指定版本时的服务端默认（较弱的 pipeline 模式），必须失效重提。

DEFAULT_CACHE_DIR = Path(__file__).parent / "data" / "extract_cache"

# 扫描件 OCR 后端（config.pdf_scan_backend 的合法值）
SCAN_BACKENDS = ("none", "mineru-cloud", "mineru-local")

# 有文字层 PDF 的提取后端（config.pdf_text_backend 的合法值）。
# mineru-local（本地部署模型，如 MinerU 本地 vlm/pipeline 模式、或未来可能接入的
# OpenDataLoader 等）：入口占位，尚未实现（问题33）。选中后安全退化为 local——
# 与扫描件分支的 mineru-local 处理不同：扫描件本地零处理能力，退化只能是"跳过不产出"；
# 文字层 PDF 本地 pymupdf4llm 本来就能产出内容，退化成"直接不处理"反而是倒退，
# 因此这里退化目标是 local 直提，而不是放弃产出。
TEXT_BACKENDS = ("local", "mineru-cloud", "mineru-local")

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


def get_text_backend():
    """有文字层 PDF 的提取后端（config.pdf_text_backend）。非法值回退 local。

    mineru-local 是尚未实现的占位选项（见 TEXT_BACKENDS 注释）；本函数只负责
    判断 config 里这个值合不合法，选中后具体怎么处理（退化到本地直提）是
    _extract_pdf 的路由职责，不在这里判断。
    """
    try:
        from config import CFG
        b = str(CFG.get("pdf_text_backend", "local")).lower()
        return b if b in TEXT_BACKENDS else "local"
    except Exception:
        return "local"


def get_model_version():
    """MinerU 云端解析用的模型版本（config.mineru_model_version）。非法值回退 vlm。

    问题33：此前请求体从未传这个参数，服务端会用未指定时的默认版本（较弱的
    pipeline 模式）——不是"选了 pipeline"，是"根本没选，官方文档写明推荐显式
    传 vlm"。默认值定为 vlm 而非 pipeline，是因为用户的核心场景（密集公式、
    电路图数字标注）恰恰是 vlm 更擅长的那类内容，pipeline 仅作为可选兜底
    （更快、更省每日解析配额）留给用户自己按需切换。
    """
    try:
        from config import CFG
        v = str(CFG.get("mineru_model_version", "vlm")).lower()
        return v if v in ("pipeline", "vlm") else "vlm"
    except Exception:
        return "vlm"


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


def sanitize_render_md(md):
    """渲染预览用的净化：把常见内联 HTML 标签转成 Markdown 等价物或剥除。

    提取产物（尤其 pymupdf4llm）会混入 <u>/<span> 等标签，flet 的 Markdown
    控件不渲染裸 HTML，会原样显示。仅用于预览渲染；源码页保持原样。
    """
    import re as _re
    md = _re.sub(r"</?(?:b|strong)>", "**", md)
    md = _re.sub(r"</?(?:i|em)>", "*", md)
    md = _re.sub(r"`?</?(?:code|kbd)>`?", "`", md)
    md = _re.sub(r"</?(?:u|s|del|ins|sub|sup|mark|small|span)[^>]*>", "", md)
    return md


def _extract_full(path, backend=None):
    """完整提取过程。返回 (markdown|None, reason, route|None, cached)。

    backend：试验台等场景的单次后端覆盖（None=跟随全局配置）。
    """
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    # 缓存按产出路由分键。扫描件相关路由（ocr:*）之间仍然互斥——一份文件是否
    # 扫描件由内容本身确定性判定，与配置无关，因此这两个可以无条件全查、
    # 后端切换不丢历史成果。但 local / mineru-text 不再是这种关系（问题30起）：
    # 同一份有文字层的 PDF 在不同 pdf_text_backend 下会产生两种都合法、但内容
    # 不同的成功结果（本地原文 vs MinerU 结构识别版），缓存命中必须只认"当前
    # 配置实际会选中的那一个"，否则切换 pdf_text_backend 会被另一个后端的历史
    # 缓存假命中，切换永远不生效——只查与当前 backend 覆盖/全局配置一致的那一个
    # 文字层路由标签（scanned 分支两个路由不受影响，逻辑不变）。
    if ext == "pdf":
        text_route = ("mineru-text" if (backend or get_text_backend()) == "mineru-cloud"
                      else "local")
        routes = ["ocr:mineru-cloud", "ocr:mineru-local", text_route]
    else:
        routes = ["local"]
    try:
        key = _file_md5(path)
    except OSError:
        return None, "unreadable", None, False
    hit, hit_route = _cache_get(key, routes)
    if hit is not None:
        return hit, "", hit_route or "local", True
    if ext == "docx":
        md, reason, route = _extract_docx(path)
    elif ext == "pdf":
        md, reason, route = _extract_pdf(path, backend=backend)
    else:
        return None, "extract-failed", None, False  # 路由层只应送 BINARY_EXTS，防御分支
    if md is None:
        return None, reason, None, False
    md = md.strip()
    if not md:
        return None, "empty", None, False
    route = route or "local"
    _cache_put(key, md, route)
    return md, "", route, False


def extract_to_markdown(path):
    """唯一入口：二进制文档 → Markdown。返回 (markdown|None, reason)。

    成功：(str, "")。失败：(None, reason)，reason 取值见模块 docstring。
    本函数绝不抛异常、绝不写源目录。
    """
    md, reason, _route, _cached = _extract_full(path)
    return md, reason


def extract_preview(path, backend=None):
    """提取试验台用：单文件提取并返回过程信息（不落索引终态）。

    backend：单次覆盖扫描件 OCR 后端（None=跟随全局配置）。
    返回 dict：md/reason/route/cached/elapsed/chars。
    """
    t0 = time.monotonic()
    md, reason, route, cached = _extract_full(path, backend=backend)
    return {"md": md, "reason": reason,
            "route": route or "-",
            "cached": bool(cached),
            "elapsed": round(time.monotonic() - t0, 2),
            "chars": len(md) if md else 0}


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


def _extract_pdf(path, backend=None):
    """PDF 提取路由：文字层 → 本地直提或按 pdf_text_backend 送 MinerU（is_ocr=False）；
    扫描件 → 按 pdf_scan_backend 分发（is_ocr=True）。

    返回 (markdown|None, reason, route)：route 标记产出路径，进缓存键
    （local=本地直提 / ocr:mineru-cloud=扫描件云端OCR / mineru-text=文字层送MinerU换结构识别），
    换后端旧缓存天然失效。
    backend：单次覆盖（试验台用）。扫描件分支按 pdf_scan_backend 语义解释
    （none/mineru-cloud/mineru-local），文字层分支按 pdf_text_backend 语义解释
    （local/mineru-cloud）；None 时各自跟随对应全局配置，两个分支互不干扰。
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
            # 扫描件：按配置（或单次覆盖）路由 OCR 后端
            scan_backend = backend or get_scan_backend()
            if scan_backend == "none":
                _warn_once("scanned",
                           "发现扫描件 PDF（无文字层），当前未启用 OCR 后端，已跳过"
                           "（可在设置中把 pdf_scan_backend 设为 mineru-cloud）")
                return None, "scanned", f"ocr:{scan_backend}"
            if scan_backend == "mineru-local":
                _warn_once("mineru-local",
                           "mineru-local（本地部署）属 R3b 尚未支持，扫描件继续跳过")
                return None, "scanned", f"ocr:{scan_backend}"
            md, reason = _mineru_cloud_extract(path, is_ocr=True)
            return md, reason, f"ocr:{scan_backend}"

        # 有文字层：默认本地直提；可选送 MinerU 只买版面/结构识别（不为已有文字重复付 OCR 的钱）
        text_backend = backend or get_text_backend()
        if text_backend == "mineru-local":
            # 入口占位，尚未实现（问题33）：与扫描件分支不同，这里不能直接放弃
            # 产出——pymupdf4llm 本来就能处理文字层 PDF，"选了本地模型但没实现"
            # 不该让用户的课件从有内容退化成 unreadable，因此安全退化到 local
            # 直提（下方 pymupdf4llm.to_markdown 分支），只警告一次，不改变
            # route 标签（仍记 "local"，因为产出内容确实是本地直提的结果）。
            _warn_once("mineru-local-text",
                       "pdf_text_backend=mineru-local 尚未实现，已退化为本地直提"
                       "（pymupdf4llm）。如需云端结构识别，请改用 mineru-cloud。")
        elif text_backend == "mineru-cloud":
            md, reason = _mineru_cloud_extract(path, is_ocr=False)
            return md, reason, "mineru-text"
        try:
            out = pymupdf4llm.to_markdown(doc)
        except Exception as e:
            _warn_once(f"pdf-conv:{e.__class__.__name__}",
                       f"PDF 转 Markdown 失败（按提取失败处理）：{e}")
            return None, "extract-failed", "local"
    except Exception as e:
        # 外层兜底：page_count / 逐页 get_text / OCR 路由这几段一旦抛异常，
        # 必须折叠成终态，绝不外抛（模块契约 + AGENTS.md 架构红线 1）。
        _warn_once(f"pdf-scan:{e.__class__.__name__}",
                   f"PDF 逐页扫描/路由异常（按提取失败处理）：{e}")
        return None, "extract-failed", "local"
    finally:
        try:
            doc.close()
        except Exception:
            pass
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

def _mineru_cloud_extract(path, is_ocr):
    """MinerU 云端 API：申请批任务 → 预签名 PUT 上传 → 轮询 → 下载 zip 取正文 .md。

    契约（mineru.net 官方文档 https://mineru.net/apiManage/docs，2026-08-26 实测校正）：
    POST {BASE}/file-urls/batch 携带 Bearer Token 取得 batch_id 与预签名上传地址；
    PUT 上传原始字节（无鉴权头）；GET {BASE}/extract-results/batch/{id} 轮询
    extract_result[0].state 至 done，取 full_zip_url 下载 zip，正文取其中最大的 .md。
    （2026-08-26 修：此前两处路径误写成 file-protocol/batch[/{id}]，实测服务器对该
    路径返回 HTTP 404 纯文本 "page not found"——路由层面不存在，不是鉴权/参数错误，
    导致该功能自上线以来任何真实调用都会失败，被下方异常折叠机制悄悄吞成
    scanned/extract-failed 终态，从未真正 OCR 成功过一次；详见 TASK_LOG 问题30。）
    is_ocr：调用方显式传入，不设默认值——扫描件分支传 True；文字层 PDF 分支传
    False（只买版面/表格结构识别，不为已有文字重复付 OCR 的钱）。
    model_version（问题33，2026-09-02）：请求体新增 config.mineru_model_version
    （get_model_version()，默认 vlm）。此前从未传这个字段，服务端会用未声明时的
    默认版本（较弱的 pipeline 模式）——这不是"选择了 pipeline"，是压根没做选择；
    官方文档建议显式传 vlm 以获得更高精度。此改动不影响 URL/鉴权，只影响服务端
    实际用哪个模型解析，因此不需要新的缓存路由标签，只靠 EXTRACT_VERSION 递增
    让旧缓存（用未指定版本时的默认模式产出的结果）整体失效重提。
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
        # 缺 Key 的落地 reason 按调用分支区分：扫描件本地零处理能力，"scanned"
        # 语义仍然成立（且是既有行为，不动）；文字层 PDF 本地能提取，缺 Key 只是
        # 拿不到云端结构识别这个可选增值项，绝不能沿用"scanned"——那会让 GUI
        # 提示"发现扫描件 PDF...或改用文字层版本"，而这份文件本来就是文字层，
        # 这条建议对用户是自相矛盾的误导。
        if is_ocr:
            _warn_once("mineru-key",
                       "pdf_scan_backend=mineru-cloud 但 mineru_api_key 为空，扫描件继续跳过")
            return None, "scanned"
        _warn_once("mineru-key-text",
                   "pdf_text_backend=mineru-cloud 但 mineru_api_key 为空，"
                   "文字层 PDF 云端结构识别失败（请在设置中补齐 Key，或把"
                   "pdf_text_backend 改回 local）")
        return None, "extract-failed"
    try:
        budget = float(CFG.get("mineru_timeout_seconds") or 600)
    except Exception:
        budget = 600.0
    deadline = time.monotonic() + max(30.0, budget)

    fname = Path(path).name
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.post(
            f"{_MINERU_BASE}/file-urls/batch",
            headers={**headers, "Content-Type": "application/json"},
            json={"enable_formula": True, "enable_table": True,
                  "model_version": get_model_version(),
                  "files": [{"name": fname, "is_ocr": is_ocr, "data_id": "doc"}]},
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
                f"{_MINERU_BASE}/extract-results/batch/{batch_id}",
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


def _preview_job(q, path_str, backend=None, cache_dir=None):
    """子进程入口（GUI 提取试验台用）：结果经队列返回父进程。

    独立进程彻底绕开 GIL——重转换期间 UI 线程零争抢；
    超时/取消由父进程 terminate() 即时强杀，无残留状态可担心。

    缓存隔离：预览全程用一次性临时缓存目录，读写都不碰生产缓存
    （data/extract_cache）。否则试验台里手选 mineru-cloud 的产物会写进生产缓存，
    之后即使全局 pdf_scan_backend=none，正式索引也会在查缓存那步直接命中这份
    云端 OCR 产物、跳过 backend=none 本该走的"跳过"分支——一次随手预览变成了
    正式索引里不可追溯、不可撤销的既成事实。读也要隔离：预览的意图是"看这次
    用这个后端会提取出什么"，读到别的后端产出的历史缓存同样会误导用户。

    临时目录归属（2026-08-25 修订）：调用方传了 cache_dir 就直接用、绝不删除——
    父进程随时会 terminate() 强杀本进程（超时/用户取消），TerminateProcess 不给
    任何 Python 层收尾机会，"子进程退出即自删"的承诺必然落空，装着云端 OCR 文字
    产物的目录会永久残留在 %TEMP%。所以清理责任上移给必然活着的父进程；
    只有在 cache_dir=None（不经 GUI 的直调，如测试）时才自建自清。

    q.put 紧跟在拿到 info 之后：收尾（还原 cache_dir / 自清目录）一律放 finally，
    否则收尾里的 rmtree 失败（Windows 上杀软、索引服务短暂占用是真实情况）
    会被 except 捕获，把一次已经成功、结果已到手的提取误报成失败。
    """
    import shutil
    import tempfile
    own_dir = cache_dir is None
    if own_dir:
        cache_dir = tempfile.mkdtemp(prefix="extract_preview_")
    prev = _cache_dir  # 直接读模块全局：None（=用默认目录）也能原样还原
    try:
        set_cache_dir(cache_dir)
        info = extract_preview(Path(path_str), backend=backend)
        q.put({"ok": True, "info": info})
    except Exception as e:
        q.put({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
    finally:
        set_cache_dir(prev)
        if own_dir:
            shutil.rmtree(cache_dir, ignore_errors=True)
