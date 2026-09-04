"""patch_r3a.py — 一次性补丁：extractors 三元组化 + _extract_pdf 重写 + 云端客户端。"""
from pathlib import Path

p = Path("extractors.py")
t = p.read_text(encoding="utf-8")

# 1) _extract_docx 的返回值三元组化（带唯一上下文逐个替换）
pairs = [
    ('_warn_once("docx-dep", "python-docx 未安装，DOCX 提取不可用"\n'
     '                               "（pip install python-docx）")\n'
     '        return None, "extract-failed"',
     '_warn_once("docx-dep", "python-docx 未安装，DOCX 提取不可用"\n'
     '                               "（pip install python-docx）")\n'
     '        return None, "extract-failed", "local"'),
    ('f"DOCX 打开失败（损坏或加密？按提取失败处理）：{e}")\n'
     '        return None, "extract-failed"',
     'f"DOCX 打开失败（损坏或加密？按提取失败处理）：{e}")\n'
     '        return None, "extract-failed", "local"'),
    ('f"DOCX 正文遍历异常（按提取失败处理）：{e}")\n'
     '        return None, "extract-failed"',
     'f"DOCX 正文遍历异常（按提取失败处理）：{e}")\n'
     '        return None, "extract-failed", "local"'),
    ('    md = "\\n".join(lines).strip()\n'
     '    if not md:\n'
     '        return None, "empty"\n'
     '    return md, ""',
     '    md = "\\n".join(lines).strip()\n'
     '    if not md:\n'
     '        return None, "empty", "local"\n'
     '    return md, "", "local"'),
]
for old, new in pairs:
    assert t.count(old) == 1, f"锚点非唯一或未找到：{old[:60]!r} (count={t.count(old)})"
    t = t.replace(old, new)

# 2) 整体替换 _extract_pdf 到文件尾：路由分发 + MinerU 云端客户端
marker = "def _extract_pdf(path):"
idx = t.index(marker)
t = t[:idx] + '''def _extract_pdf(path):
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
        md = "\\n".join(
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
'''

ast_check = compile(t, "extractors.py", "exec")
print("语法 OK，新文件行数:", len(t.splitlines()))
p.write_text(t, encoding="utf-8", newline="")
