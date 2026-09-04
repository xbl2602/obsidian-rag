# 红队评审报告 — 多格式文档支持落地（92af6e6 → d3fbf5c）

## B1 🔴 blocker — `extractors.py:329-381` `_extract_pdf` 缺 except，PDF 页面级异常未被吸收，触发"整批清零"式死循环
（与 reliability.md B1 同一根因，独立发现）350-380 行 try 只有 finally 没有 except，351/359-360 行的 `doc.page_count`/逐页 `get_text` 暴露在外。pymupdf 对"能打开但页面损坏/需要密码未认证"的 PDF，在逐页访问时才抛异常。异常冒泡穿透 `_extract_full`（调用处同样无 try/except）→ `extract_to_markdown` → `index.py:1335`（无局部 try/except）→ 整个 `_index_core` for 循环中断，`save_meta()` 在循环全部跑完后才调用，本轮循环中已处理的其它文件全部白做。下一轮索引在同一坏 PDF 上重复崩溃——比"单文件卡死"更严重的"整库当轮进度清零"式死循环。

对照：DOCX 路径（`_extract_docx`）用覆盖全部子元素遍历的 try/except 兜底，行为达标；PDF 路径不对称、不达标。

## B2 🔴 blocker — GUI 提取试验台的后端覆盖会永久污染生产环境共享缓存，绕过全局 pdf_scan_backend 门禁
试验台下拉框可选 mineru-cloud，与全局 `pdf_scan_backend` 完全独立；点击提取后 `_cache_put` 写入的是生产环境同一个 `DEFAULT_CACHE_DIR`（试验台没有调用 `set_cache_dir` 做隔离）。`_extract_full` 的缓存路由查找列表 `routes = ["ocr:mineru-cloud","ocr:mineru-local","local"]` 恒定构造、与传入 backend/全局配置无关（"后端切换不丢历史成果"的设计）。用户之后即使从未把 `pdf_scan_backend` 设为 mineru-cloud，正常索引也会命中试验台写入的云端 OCR 缓存，完全跳过 backend=none 分支——该文件正文永久来自云端 OCR，meta 不记录 route/xsrc，用户无法事后判断、也无清除路径（`--full` 不清 extract_cache）。与 R3a"pdf_scan_backend 是唯一开关"的设计初衷直接矛盾。

`test_preview_backend_override` 只测了配置不被污染+用了 set_cache_dir 隔离，恰恰没测"预览覆盖写入的缓存后续被真实 backend=none 索引捡到"这条链路。

## 🟡 note — `mineru_timeout_seconds` 手改为 0 时被 `0 or 600` 静默吞掉改用默认值而非报错/地板值（需手改 JSON，触发条件苛刻）。

## 🟡 note — 加密/需要密码的 PDF 未检查 `doc.needs_pass`，会被误判为"扫描件"（get_text 返回空串），可能触发无意义的云端上传。

---

## 总体结论
文本类/DOCX 路径确实做到"绝不抛异常"，测试套件对已知 xfail 序列覆盖扎实。但 PDF 路径存在未加 except 的真实漏洞，遇到页面级损坏的 PDF（下载不完整/云盘同步半截文件/加密文档，真实 vault 里完全可能出现）会击穿终态防线，造成整库当轮进度清零式死循环。GUI 试验台与生产索引共享同一份提取缓存且路由查找不看当前配置，使一次性"预览测试"能在用户不知情且事后无法追溯的情况下让文件内容永久来自云端 OCR，绕开本该由 pdf_scan_backend 严格把关的隐私边界。建议 R3a 收口前必须先补 `_extract_pdf` 异常兜底，并给试验台的后端覆盖加缓存隔离或二次确认提示。
