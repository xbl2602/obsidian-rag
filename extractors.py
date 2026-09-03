"""extractors.py — 多格式文档提取（DOCX + 文字层 PDF 直提；扫描件经 OCR 后端）。

单一入口 extract_to_markdown(path)：把二进制文档转成 Markdown 字符串，
交给既有的整条切块管线。设计红线（实施时不得偏离）：
  - 绝不抛异常：任何失败都折叠成 (None, reason)，reason ∈
    {"unreadable", "extract-failed", "empty", "scanned"}（"tbd" 由 index 层判定）；
  - 绝不写源目录：所有落盘只发生在 data/extract_cache 缓存目录；
  - 懒加载：import extractors 不引入 pymupdf/python-docx/requests，
    只索引 md/txt 的进程永远不会触碰这些库；
  - 含图片页的 PDF 整本按 pdf_scan_backend 路由（问题34：纯扫描件与「文字层 +
    扫描图」混合型一体对待——只要存在任何一页无文字层，整本走同一条流程，
    废除旧「文字层页占比 < 0.5 才算扫描件」的整本二分，它会把混合型课件的
    图片页内容静默丢掉）：
      非 mineru-cloud → (None, "scanned") 整本跳过并记终态（默认 none；宁可诚实
                         空缺也不产出半份拼接内容，开启云端后经 xsrc 自愈自动重试）
      mineru-cloud   → MinerU 云端 API OCR（is_ocr=True，vlm 统一视觉认字，
                       产出一份连贯完整的 Markdown），成功则照常入索引
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
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

# 单一事实来源：index 的后缀路由与 library 的 extensions 白名单校验都引用这里，
# 禁止在任何一处重新硬编码这份清单。
TEXT_EXTS = {"md", "txt"}
BINARY_EXTS = {"pdf", "docx"}
SUPPORTED_EXTS = TEXT_EXTS | BINARY_EXTS

EXTRACT_VERSION = 4  # 提取逻辑版本：v2 起缓存键含产出路由（local / ocr:后端 / mineru-text）；
                      # v3：MinerU 云端请求体新增 model_version 参数（问题33），旧缓存产出
                      # 用的是未指定版本时的服务端默认（较弱的 pipeline 模式），必须失效重提。
                      # v4：分拣规则改为「存在图片页即整本按扫描件路由」（问题34）——v3 及
                      # 更早版本对混合型 PDF 会走文字层直提产出半份内容（图片页静默丢失），
                      # 这类文件在 v4 下要么整本送云端、要么落 scanned 终态，产出语义不同；
                      # 旧缓存（含 local 路由的半份结果）必须整体失效，防止 v4 的 text_route
                      # 候选误命中 v3 时代的半份 local 缓存。

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
# 但一两行的真实短内容要算（10 字符以下仍判无文字层）。
# 问题34 起判定只到「页」粒度：图片页是否存在决定整本走哪条流程，不再有
# 「页占比阈值」这种整本一票表决的中间层。
_TEXT_PAGE_MIN_CHARS = 10


def _extract_pdf(path, backend=None):
    """PDF 提取路由（问题34 起：按页检测、整本分派）。

    逐页探测文字层（_TEXT_PAGE_MIN_CHARS），然后整本二分：
      - 存在任何图片页（含纯扫描件与「文字层+扫描图」混合型）→ 整本按
        pdf_scan_backend 分派：仅 mineru-cloud 送云端（is_ocr=True，vlm 统一
        视觉认字，产出一份连贯完整的 Markdown）；其余取值整本跳过落
        "scanned" 终态——本地对图片页零认字能力，宁可诚实空缺待自愈，
        也不产出「文字页直提 + 图片页缺失」的半份内容。
      - 整本每一页都有文字层 → 按 pdf_text_backend：local 本地直提 /
        mineru-cloud 送 MinerU 换结构识别（is_ocr=False）/ mineru-local 占位退化。

    返回 (markdown|None, reason, route)：route 标记产出路径，进缓存键
    （local=本地直提 / ocr:mineru-cloud=整本云端OCR / mineru-text=文字层送MinerU换结构识别），
    换后端旧缓存天然失效。
    backend：单次覆盖（试验台用）。None 时扫描件分支跟随 pdf_scan_backend、
    文字层分支跟随 pdf_text_backend，两个分支互不干扰。
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
        if text_pages < doc.page_count:
            # 存在图片页：整本按扫描件流程（问题34）。仅 mineru-cloud 有产出能力，
            # 其余任何取值（none 默认 / mineru-local 占位 / 未知值 / 试验台覆盖）
            # 一律收拢到「跳过」——收拢同时封死旧代码「未匹配取值 fall-through
            # 到云端调用」的口子（如试验台把文字层语义的 local 覆盖进扫描分支）。
            scan_backend = backend or get_scan_backend()
            if scan_backend != "mineru-cloud":
                _warn_once("scanned",
                           "PDF 含图片页（无文字层），当前未启用云端 OCR 后端，已整本跳过"
                           "（可在设置中把 pdf_scan_backend 设为 mineru-cloud）")
                return None, "scanned", f"ocr:{scan_backend}"
            md, reason = _mineru_cloud_extract(path, is_ocr=True)
            return md, reason, "ocr:mineru-cloud"

        # 整本每一页都有文字层：默认本地直提；可选送 MinerU 只买版面/结构识别
        # （不为已有文字重复付 OCR 的钱）
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
# ---------- 并行批量基础设施（问题35） ----------

# 提交错误类别（问题35，按 mineru.net 官方错误码表分类，不能一套重试逻辑应付所有情况）
_TOKEN_CODES = frozenset({"A0202", "A0211"})              # Token 错误/过期 → 停整批
_FATAL_CODES = frozenset({"-60002", "-60004", "-60005", "-60006"})  # 格式/空文件/超限 → 不重试
_TRANSIENT_CODES = frozenset({"-10001", "-60007", "-60009"})        # 服务异常/模型不可用/队列满
_SUBMIT_MAX_ATTEMPTS = 4   # 1 次首发 + 3 次重试（退避 1/2/4s + 抖动；429 尊重 Retry-After）


class _MineruSubmitError(RuntimeError):
    """MinerU 提交阶段错误：携带官方错误码与类别（kind）。

    transient → 值得重试（网络异常 / 429 限流 / 服务异常类错误码）；
    fatal     → 重试无意义（超 200MB / 超 200 页 / 格式问题 / 空文件），立即失败；
    token     → Token 错误或过期——重试只会烧频控配额，且同一批后续请求大概率
                全部失败，置全局失效标志让调度方停掉剩余任务。
    """

    def __init__(self, kind, code, msg):
        super().__init__(msg)
        self.kind = kind
        self.code = code


def _classify_mineru_code(code):
    """官方错误码 → 提交错误类别。未知码按可重试处理（宁可多试一次，不误杀）。"""
    c = str(code).upper()
    if c in _TOKEN_CODES:
        return "token"
    if c in _FATAL_CODES:
        return "fatal"
    if c in _TRANSIENT_CODES or c == "429":
        return "transient"
    return "transient"


def _backoff_delay(attempt, retry_after=None):
    """指数退避 + 抖动（问题35 调研结论的行业标准做法）：1s→2s→4s→8s…，
    叠加 0~基数的随机抖动，避免并发请求踩同一时间点重试形成重试风暴。
    带 Retry-After 头时优先按它等（封顶 60s）。"""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, 60.0)
    base = float(min(2 ** max(0, attempt - 1), 60))
    return base + random.uniform(0, base)


def _retry_after_of(resp):
    """响应的 Retry-After 头 → 秒数（缺失/解析失败返回 None）。"""
    if resp is None:
        return None
    try:
        v = float(resp.headers.get("Retry-After"))
        return v if v > 0 else None
    except Exception:
        return None


# ---- 提交限速（官方三个提交接口共用 50 个文件/分钟滚动频控，mineru.net 文档） ----

_rate_lock = threading.Lock()
_submit_stamps = []   # 最近 60s 内已提交时刻（time.monotonic 秒）——测试可直接操纵


def get_rate_per_minute():
    """config.mineru_rate_per_minute：每分钟最多提交多少个文件；<=0 = 不限速。"""
    try:
        from config import CFG
        v = int(CFG.get("mineru_rate_per_minute", 45) or 0)
    except Exception:
        v = 45
    return v


def _window_delay(now=None):
    """纯计算（便于测试）：距下一个可提交槽位还需等待的秒数；0 = 立即可提交。

    滑动窗口计数器：只看"最近 60s 内已提交数"，不做令牌桶——个人库量级下
    这不是要突破的瓶颈，是"万一触顶体面退让"的节奏器。
    """
    limit = get_rate_per_minute()
    if limit <= 0:
        return 0.0
    now = time.monotonic() if now is None else now
    window = 60.0
    live = [t for t in _submit_stamps if t > now - window]
    if len(live) < limit:
        return 0.0
    return max(0.0, min(live) + window - now)


def _submit_gate():
    """提交闸门：占一个滑动窗口槽位，必要时睡眠等待。

    持锁等待 = 提交节奏串行化，正是限速的目的；等待时长受窗口长度约束有界。
    每次提交尝试（含重试）各占一个槽位——重试也是一次真实提交。
    """
    with _rate_lock:
        while True:
            delay = _window_delay()
            if delay <= 0:
                now = time.monotonic()
                _submit_stamps[:] = [t for t in _submit_stamps if t > now - 60.0]
                _submit_stamps.append(now)
                return
            time.sleep(min(delay, 1.0))


# ---- Token 失效全局标志（问题35） ----
# A0202/A0211 一旦出现，同一批后续请求大概率全部失败，继续提交只是烧频控配额。
# 置位后：worker 快速失败不发请求；index 调度方取消尚未启动的任务。索引每轮
# 云端段开始时调用 mineru_token_reset() 归零（长驻 server 进程跨轮次复用）。
_token_invalid = threading.Event()


def mineru_token_invalid():
    """本进程内是否已发现 MinerU Token 失效。"""
    return _token_invalid.is_set()


def mineru_token_reset():
    """清除 Token 失效标志（每轮索引的云端段开始时调用）。"""
    _token_invalid.clear()


# ---- 在途任务簿记（问题35，断点恢复）：data/extract_cache/mineru_pending.json ----
# 提交成功并上传完成后、进入轮询前落盘 {batch_id: {path, route, md5}}。进程被杀
# 后条目留存；下一轮对同一文件提取时按 path+md5 匹配 → 续接轮询拿结果，不重复
# 提交（服务器端任务独立于本进程存活，重复提交纯烧配额）。簿记文件放在提取缓存
# 目录下：试验台的隔离缓存目录天然隔离（预览中断的条目随临时目录销毁，绝不会
# 污染生产簿记——架构红线 7 的同一教训）。
_pending_lock = threading.Lock()


def _pending_path():
    return get_cache_dir() / "mineru_pending.json"


def _quota_path():
    return get_cache_dir() / "mineru_quota.json"


def _json_load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _json_save(path, d):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log(f"{Path(path).name} 状态写失败（忽略，仅影响断点恢复/计数）：{e}")


def _pending_load():
    return _json_load(_pending_path())


def _pending_save(d):
    _json_save(_pending_path(), d)


def _pending_add(batch_id, path_str, route, key):
    with _pending_lock:
        d = _pending_load()
        d[batch_id] = {"path": path_str, "route": route, "md5": key}
        _pending_save(d)


def _pending_remove(batch_id):
    with _pending_lock:
        d = _pending_load()
        if d.pop(str(batch_id), None) is not None:
            _pending_save(d)


def _pending_match(path_str, key):
    """按 文件路径 + 字节指纹 匹配在途任务（md5 不一致 = 文件已变，续接无意义）。"""
    with _pending_lock:
        d = _pending_load()
        for bid, e in d.items():
            if e.get("path") == path_str and e.get("md5") == key:
                return {"batch_id": bid, "path": e.get("path"),
                        "route": e.get("route") or "ocr:mineru-cloud",
                        "md5": e.get("md5")}
    return None


def mineru_pending_prune(alive_md5):
    """清理在途簿记孤儿：文件不在本轮待处理集合、或字节已变化的条目。

    alive_md5：{文件绝对路径字符串: 当前字节 md5}。只做本地簿记清理、不查询
    服务器——孤儿任务的结果即使服务器完成也没有归宿（md5 对不上任何当前文件）。
    """
    with _pending_lock:
        d = _pending_load()
        kept = {bid: e for bid, e in d.items()
                if alive_md5.get(e.get("path")) == e.get("md5")}
        if len(kept) != len(d):
            _pending_save(kept)


# ---- 每日配额计数（问题35：仅提醒用，非硬门禁） ----
# 官方：每账号每日 1000 页最高优先级额度，超出降优先级但仍处理（非拒绝）。
# 个人库量级几乎不可能触顶；计数只用于接近额度时打日志提醒"接下来会被降优先级、
# 处理变慢"，避免用户困惑于莫名变慢。不做成硬门禁：降级 ≠ 失败，阻断反而制造问题。

def mineru_quota_add(files, pages):
    """按日累计云端提交量（文件数/页数）；日期翻篇自动清零。"""
    today = time.strftime("%Y-%m-%d")
    with _pending_lock:
        d = _json_load(_quota_path())
        if not isinstance(d, dict) or d.get("date") != today:
            d = {"date": today, "files": 0, "pages": 0}
        d["files"] = int(d.get("files", 0)) + int(files)
        d["pages"] = int(d.get("pages", 0)) + int(pages)
        _json_save(_quota_path(), d)
        return d


def mineru_quota_today():
    """读取今日累计 {date, files, pages}（无记录/跨天返回零值结构）。"""
    d = _json_load(_quota_path())
    today = time.strftime("%Y-%m-%d")
    if not isinstance(d, dict) or d.get("date") != today:
        return {"date": today, "files": 0, "pages": 0}
    return d


def _cloud_key_ready():
    """mineru_api_key 是否已配置（分流预判用：没 Key 的云端调用是快速失败，不值得并行）。"""
    try:
        from config import CFG
        return bool(str(CFG.get("mineru_api_key", "")).strip())
    except Exception:
        return False


def classify_extraction(path, backend=None, md5=None):
    """并行分流预判（问题35）：这份文件的提取会不会走 MinerU 云端网络往返。

    md5：调用方已持有的文件字节指纹（如 index 的 bhash），传入可免重复读盘；
    未传则现算。

    返回 (kind, pages, is_ocr, route)：
      kind="cloud"  → 需要云端往返，值得攒批并行；is_ocr/route 给出实际路由
                      （worker 用它们调 mineru_cloud_extract_for_parallel，
                      不再重开 PDF——PyMuPDF 不保证多线程安全，worker 线程
                      绝不触碰 pymupdf）。
      kind="inline" → 本地快速路径（缓存秒回 / 本地直提 / 云端未启用的快速失败），
                      当场跑完即可。pages = 页数（非 PDF 或打不开为 0）。
    判定与 _extract_pdf 的路由条件严格同源；漂移只影响并行收益（该并行的没并行 /
    不该并行的多绕一道），绝不影响产出正确性——真正执行仍走 extract_to_markdown
    的完整路径（缓存、终态语义均以它为准）。
    """
    path = Path(path)
    if path.suffix.lower().lstrip(".") != "pdf":
        return "inline", 0, None, None
    if isinstance(md5, str) and md5 and md5 != "unreadable":
        key = md5
    else:
        try:
            key = _file_md5(path)
        except OSError:
            return "inline", 0, None, None
    text_backend = backend or get_text_backend()
    text_route = "mineru-text" if text_backend == "mineru-cloud" else "local"
    hit, _ = _cache_get(key, ["ocr:mineru-cloud", "ocr:mineru-local", text_route])
    if hit is not None:
        return "inline", 0, None, None
    try:
        import pymupdf
        doc = pymupdf.open(str(path))
    except Exception:
        return "inline", 0, None, None
    try:
        pages = doc.page_count
        if pages <= 0:
            return "inline", 0, None, None

        def _pt(pg):
            t = pg.get_text("text")
            return t if isinstance(t, str) else ""

        text_pages = sum(1 for pg in doc
                         if len(_pt(pg).strip()) >= _TEXT_PAGE_MIN_CHARS)
        if text_pages < pages:
            # 与 _extract_pdf 扫描分支同源（问题34）：有图片页 → 仅 mineru-cloud
            # 且 Key 已配时值得并行（没 Key 是快速失败）
            if (backend or get_scan_backend()) == "mineru-cloud" and _cloud_key_ready():
                return "cloud", pages, True, "ocr:mineru-cloud"
            return "inline", pages, None, None
        if text_backend == "mineru-cloud" and _cloud_key_ready():
            return "cloud", pages, False, "mineru-text"
        return "inline", pages, None, None
    except Exception:
        return "inline", 0, None, None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def mineru_cloud_extract_for_parallel(path, is_ocr, key=None):
    """云端并行 worker 入口（问题35）＝ _mineru_cloud_extract 的显式公开面。

    路由与 is_ocr 已由主线程 classify_extraction 判定；worker 只做纯网络 I/O
    （提交/上传/轮询/下载），缓存与断点簿记在 _mineru_cloud_extract 内部完成。
    key：主线程已持有的文件字节指纹（免重复读盘）。失败折叠 (None, reason)，
    与 extract_to_markdown 同一契约。
    """
    return _mineru_cloud_extract(path, is_ocr=is_ocr, key=key)


def _mineru_resume(job, headers, budget):
    """断点续接（问题35）：对中断前已提交的任务按服务器端状态收尾。

    - done → 下载解包 → 写缓存（按条目记录的 md5/route）→ 移除条目 → 返回正文；
    - 超时 / 下载失败 → 保留条目（服务器端结果仍可能取回），返回提取失败；
    - failed / gone / no-md → 移除条目，返回提取失败（下一轮正常重提）；
    - 正文为空 → 移除条目，返回 (None, "empty")。
    """
    import requests
    deadline = time.monotonic() + max(30.0, budget)
    md, why = _mineru_poll_result(job["batch_id"], headers, deadline, requests)
    if md is not None:
        _cache_put(job["md5"], md, job["route"])
        _pending_remove(job["batch_id"])
        return md, ""
    if why in ("timeout", "download"):
        return None, "extract-failed"          # 保留条目，下轮续接
    _pending_remove(job["batch_id"])
    return None, "empty" if why == "empty" else "extract-failed"


def _mineru_poll_result(batch_id, headers, deadline, requests_mod):
    """轮询 batch 至终态并下载解包取正文 .md（问题35 自 _mineru_cloud_extract 抽出，
    新鲜提交与断点续接共用同一条轮询路径）。

    返回 (md, why)：md 非 None 时 why="done"；md 为 None 时 why ∈
      timeout  — 本地预算耗尽（服务器端可能仍在跑，调用方应保留 pending 条目）
      failed   — 服务器报任务失败（调用方移除条目，下一轮正常重提）
      gone     — 服务器不认识该 batch（含 404/非 JSON 响应；移除条目；429/5xx
                 属瞬时异常不算 gone，deadline 内退避续询）
      download — 结果包下载失败（保留条目，下轮重取）
      no-md    — 结果包里没有 .md（移除条目）
      empty    — 提取成功但正文为空（移除条目）
    网络异常上抛，由调用方的统一异常折叠处理（有 pending 条目时保留）。
    """
    zip_url = None
    while time.monotonic() < deadline:
        poll = requests_mod.get(
            f"{_MINERU_BASE}/extract-results/batch/{batch_id}",
            headers=headers, timeout=30)
        try:
            pdata = poll.json()
            if not isinstance(pdata, dict):
                pdata = {}
        except Exception:
            pdata = {}  # 404 纯文本等非 JSON 响应：下面按状态码分流，绝不让解析异常外泄
        if poll.status_code == 429 or 500 <= poll.status_code < 600:
            # 限流/服务端瞬时异常（问题36）：deadline 内退避后继续轮询，绝不误判
            # 任务失败——max 模式在途任务多、轮询请求密，撞到限流要体面退让；
            # "gone" 只留给服务器明确不认识该 batch 的响应。
            _sleep(min(_retry_after_of(poll) or _POLL_INTERVAL * 2, 60.0))
            continue
        if poll.status_code != 200 or pdata.get("code") not in (0, 200):
            return None, "gone"
        items = (pdata.get("data") or {}).get("extract_result") or []
        state = items[0].get("state") if items else "pending"
        if state == "done":
            zip_url = items[0].get("full_zip_url")
            break
        if state in ("failed", "error"):
            return None, "failed"
        time.sleep(_POLL_INTERVAL)
    if not zip_url:
        return None, "timeout"
    zreq = requests_mod.get(zip_url, timeout=120)
    if zreq.status_code != 200:
        return None, "download"
    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(zreq.content))
    mds = [zi for zi in zf.infolist() if zi.filename.lower().endswith(".md")]
    if not mds:
        return None, "no-md"
    best = max(mds, key=lambda zi: zi.file_size)  # 最大者=正文主文档
    md = zf.read(best).decode("utf-8", "replace")
    return (md, "done") if md.strip() else (None, "empty")


_sleep = time.sleep  # 测试注入点（重试退避不真睡）


def _mineru_submit(path, is_ocr, headers):
    """提交批任务（问题35：滑动窗口限速 + 按错误类别重试）。

    临时性失败（网络异常 / 429 / 服务异常类错误码）按指数退避 + 抖动重试，
    429 尊重 Retry-After；永久性失败（超限/格式/空文件）与 Token 失效立即上抛
    （Token 失效同时置全局标志，调度方据此停掉同批剩余任务）。
    返回提交响应的 data dict（含 batch_id / file_urls）。
    """
    import requests
    fname = Path(path).name
    body = {"enable_formula": True, "enable_table": True,
            "model_version": get_model_version(),
            "files": [{"name": fname, "is_ocr": is_ocr, "data_id": "doc"}]}
    last = None
    for attempt in range(1, _SUBMIT_MAX_ATTEMPTS + 1):
        resp = None
        try:
            _submit_gate()
            resp = requests.post(
                f"{_MINERU_BASE}/file-urls/batch",
                headers={**headers, "Content-Type": "application/json"},
                json=body, timeout=30)
            data = resp.json()
            if resp.status_code != 200 or data.get("code") not in (0, 200):
                code = data.get("code", resp.status_code)
                raise _MineruSubmitError(
                    _classify_mineru_code(code), code,
                    f"任务提交异常 HTTP {resp.status_code} code={code}")
            return data
        except _MineruSubmitError as e:
            if e.kind != "transient" or attempt == _SUBMIT_MAX_ATTEMPTS:
                if e.kind == "token":
                    # A0202/A0211：置全局失效标志——同批后续请求大概率全部失败，
                    # worker 入口据此快速失败、index 调度方据此取消剩余任务。
                    _token_invalid.set()
                raise
            _sleep(_backoff_delay(attempt, _retry_after_of(resp)))
        except requests.RequestException as e:   # 网络层异常：可重试
            last = e
            if attempt == _SUBMIT_MAX_ATTEMPTS:
                break
            _sleep(_backoff_delay(attempt))
    if last is not None:
        raise last
    raise RuntimeError("任务提交失败")

def _mineru_cloud_extract(path, is_ocr, key=None):
    """MinerU 云端 API：申请批任务 → 预签名 PUT 上传 → 轮询 → 下载 zip 取正文 .md。

    key：调用方已持有的文件字节 md5（并行 worker 传入，免重复读盘）；None 现算。

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
    问题35（2026-09-03）并行化改造：提交段抽到 _mineru_submit（滑动窗口限速 +
    错误分类重试）；轮询/下载抽到 _mineru_poll_result（与断点续接共用）；上传成功
    即落 _pending_add 断点簿记（进程中断后下一轮按 batch_id 续接服务器端结果，
    不重复提交）；同文件同字节内容已有在途任务时优先续接（_pending_match →
    _mineru_resume）而非重新提交。
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
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if not (isinstance(key, str) and key and key != "unreadable"):
            key = _file_md5(path)
        # 断点恢复优先（问题35）：同一文件同一字节内容已有在途任务 → 续接轮询
        # 拿结果，不重复提交（重复提交纯烧配额；服务器端任务独立于本进程存活）。
        job = _pending_match(str(path), key)
        if job is not None:
            return _mineru_resume(job, headers, budget)
        if _token_invalid.is_set():
            # 同批已有任务发现 Token 失效：本文件不发请求直接失败（index 调度方
            # 会取消尚未启动的其余任务）；没有 pending 条目可留。
            return None, "extract-failed"

        data = _mineru_submit(path, is_ocr, headers)
        batch_id = (data.get("data") or {}).get("batch_id")
        put_url = ((data.get("data") or {}).get("file_urls") or [None])[0]
        if not batch_id or not put_url:
            raise RuntimeError("提交响应缺少 batch_id/file_urls")
        put = requests.put(put_url, data=Path(path).read_bytes(), timeout=120)
        if put.status_code not in (200, 201):
            raise RuntimeError(f"文件上传失败 HTTP {put.status_code}")
        # 上传成功即落断点簿记（问题35）：从此刻起无论本进程死活，下一轮都能按
        # batch_id 续接服务器端结果；成功取回正文后移除。
        route = "ocr:mineru-cloud" if is_ocr else "mineru-text"
        _pending_add(batch_id, str(path), route, key)
        md, why = _mineru_poll_result(batch_id, headers,
                                      time.monotonic() + max(30.0, budget), requests)
        if md is not None:
            _cache_put(key, md, route)
            _pending_remove(batch_id)
            return md, ""
        if why in ("timeout", "download"):
            # 保留簿记：服务器端任务可能仍在跑 / 结果包仍可取，下一轮续接，
            # 不浪费已花掉的提交配额
            return None, "extract-failed"
        _pending_remove(batch_id)   # failed / gone / no-md：下一轮按正常流程重提
        return None, "empty" if why == "empty" else "extract-failed"
    except Exception as e:
        # 只记类型与摘要：api_key 与响应体全文绝不进日志。
        # 此处已有 pending 条目的失败（轮询/下载中的网络异常等）保留条目——
        # 下一轮断点续接，不重复提交。
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
