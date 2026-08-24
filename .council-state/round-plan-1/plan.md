# Council 方案门 round-plan-1：多格式文档支持实现方案

任务：为 obsidian-rag 增加 PDF + DOCX + 扫描件 OCR 支持。
模式：Blocker。基线 HEAD=92af6e6ca3e8adf76dea13b7b0be7a2ee7c342d9。

## 目标
索引时把非 MD 文件统一转成 Markdown 复用既有切块管线，源文件零写入、检索引用仍指向原始相对路径，提取失败用 xfail 元数据条目防止死循环。

## 改动清单

### 2.1 新文件 extractors.py（约 200 行）
对外契约唯一入口：`extract_to_markdown(path) -> str | None`，绝不抛异常、绝不写源目录。
按 suffix 分发：.docx→DOCX 路；.pdf→PDF 路；其他后缀→warn_once+None（.md/.txt 由 index._load_text 直读）。顶层 try/except 兜底降级 None+日志。

- `_cached(path, fn)`：缓存层。key=原始文件字节 md5；路径 data/ocr_cache/<md5>.md。命中直接返回；None 结果不写缓存；成功结果以 `<md5>.<pid>.tmp` 写临时文件后 os.replace 原子落盘（tmp 带 pid：提取在写锁外并发区，防跨进程互踩）。
- `_docx_to_md`：懒加载 import docx。遍历 document.element.body 保序：w:p→Paragraph，w:tbl→Table。样式 Heading N/标题 N（兜底 base_style）→ #×N（N>3 钳 ###）；表格转管道表（首行表头+分隔行，|转义，换行→空格）。
- `_pdf_to_md`：懒加载 pymupdf（兼容 fitz）。逐页文字覆盖率=实质文字页(len>=20)/总页数。≥0.5 → pymupdf4llm.to_markdown()（list 返回值 join 归一）；否则 _ocr_via_backend。
- `_ocr_via_backend`：CFG["pdf_scan_backend"]：none→warn_once+None；mineru-local→mineru_local_cmd 或 shutil.which("mineru"/"magic-pdf") 探测；mineru-cloud→同构。子进程卫生：列表参数禁 shell=True、utf-8 显式解码 errors=replace、timeout=600（超时=失败）、Windows CREATE_NO_WINDOW。工作目录与输出目录=data/ocr_cache/tmp-<pid>-<ts>/，rglob("*.md") 取最大者读取后整体删临时目录。
- `_warn_once(key,msg)`：进程级去重 stderr 警告。

### 2.2 index.py（4 处 + 1 可选）
(a) L42 META_VERSION 8→9，注释 v9 多格式提取+原始字节指纹。
(b) 新增 TEXT_SOURCE_EXTS={".md",".txt"} 与 `_load_text(fpath, extract=True)` 统一入口：返回 (markdown|None, 原始字节MD5)。md/txt 读字节 utf-8 解码 replace；其他扩展 extract=True 走 extractors（带缓存），extract=False 返回 None（kb_stale 指纹专用，不跑转换）。read_bytes OSError 向上传播；extractors 函数内懒加载。
(c) kb_stale L917-933 重写指纹段：L920 read_text→_load_text(extract=False)；is_tbd_heavy 移入 content 非 None 分支；删除 content.encode md5 改用原始字节哈希；L937-943 两处 not v.get("tbd") → not v.get("tbd") and not v.get("xfail")。
(d) _index_core L1149 替换 content,fhash=_load_text(fpath)；新增失败分支（tbd 分支后、current_rels.add 前）：content None → log + current_rels.add(rel) + meta[rel]={hash,chunks:0,size,mtime,tbd:False,xfail:True} + changed+=1 + continue。chunks=0 ⇒ valid id 零贡献、期望块数零贡献、指纹稳定 ⇒ 不再触发重建；文件变化时快速路径失效重新提取。
L1161 frontmatter 门控：suffix in TEXT_SOURCE_EXTS 才 extract_frontmatter 否则 ({}, content)。
增量快速路径 L1143-1148 无需改（xfail 条目 tbd:False 自动进 current_rels 存活）。
(可选采纳) 提取前 update_progress(phase="converting",...) 且 progress_text 加 converting 分支，防长 OCR 被停滞看门狗误报。

⚠️ 关键调研发现：现有代码 tbd=True 条目实际从不落盘（L1274 裁剪不在 current_rels 的条目），kb_stale L914-916 的 tbd 快速路径是防御性死代码。故 xfail 必须显式加入 current_rels 使条目持久化，不能照抄 tbd 裁剪行为，否则防死循环落空。

### 2.3 library.py
set_config 在 collection 校验分支旁新增 extensions 白名单分支：非法值（非 {md,txt,pdf,docx}）抛 ValueError；合法值小写归一+去重保序。

### 2.4 config.py
DEFAULTS 与 CONFIG_TEMPLATE 同步新增三键（值逐字节一致，template_consistency_errors 保持 []）：
"pdf_scan_backend": "mineru-local"；"mineru_local_cmd": ""；"mineru_cloud_cmd": ""

### 2.5 requirements.txt
追加 pymupdf / pymupdf4llm / python-docx，版本实施第 1 步安装验证后回填锁定值（lxml cp314 轮子是唯一硬风险，失败即停回报；退路 docx 优雅降级）。

### 2.6 新文件 tests/test_extractors.py（风格对齐 audit_regression_test.py，非 pytest）
10 组用例：docx 回环（标题层级/管道表/正文）；pdf 文字层生成提取；伪造二进制返回 None；空 docx=None（走 xfail）；缓存命中计数不增+None 不写缓存；xfail 防死循环（坏 pdf patch 失败跑 _index_core→meta 有 chunks:0/xfail→第二遍零重建→patch 成功+mtime 变化→第三遍出块）；kb_stale 对 xfail meta 判 not stale、字节变化判 stale；源目录零写入快照；MinerU 后端未装自动 skip；静态断言 kb_stale/_index_core 含 _load_text 不含裸 fpath.read_text。

### 2.7 tests/verify_export_import.py L149-151
旧 rglob("*.md") 计数比对 → manifest vault_files[].rel 权威清单集合级比对（扩展名无关、更严格；不改 export.py schema）。
[编排者已裁决：接受]

### 2.8 明确不改
retriever.py 零改动（块元数据链路基于 meta.file 相对路径，天然指向原文件）；export/server/gui-store/check-notes 已透传 cfg["extensions"] 自动受益；index_vault legacy 入口保持 ["md"]。

## 实现顺序
1. requirements 安装验证（Py3.14 轮子）→ 回填版本号，失败即停
2. config.py 三键双写 → template_consistency_errors 自检
3. extractors.py 全新模块（只依赖 config.CFG）
4. index.py：META_VERSION → _load_text → kb_stale → _index_core（含 converting 相位）
5. library.py 白名单
6. tests/test_extractors.py 全量
7. verify_export_import.py 修复
8. 六个既有测试回归

## 验收标准 A1-A12
A1 template_consistency_errors()==[]
A2 三键生效+补写一致
A3 docx/pdf 提取 PASS
A4 优雅降级 None 无堆栈
A5 缓存命中不重跑
A6 xfail 防死循环
A7 源目录零写入
A8 extensions 白名单拒 exe 收 md,pdf
A9 原始字节指纹静态断言+md 库不误判
A10 META_VERSION=9 触发 version_upgrade=True
A11 verify_export_import 总失败 0
A12 audit_regression 19/19 等六件套全绿

## 风险
1 Py3.14 轮子（lxml）→ 第 1 步先行验证，退路 docx 降级
2 MinerU 未装无法实测 CLI → 单函数封装+skip 测试+联调待用户装机
3 长 OCR 触发停滞误报 → converting 相位
4 并发缓存竞态 → pid tmp + os.replace，最坏重复计算不写坏
5 pymupdf4llm API 漂移 → isinstance list 归一+锁版本
6 既有隐患备案：(a) 空正文 md 守卫不落 meta 反复判 added；(b) kb_stale tbd 注释与实际裁剪不符（死代码）——另立任务
7 测试改动边界：仅授权 verify_export_import 一处，其余全新增

[编排者裁决] 待确认三项全部接受：manifest 集合比对；converting 相位要做；版本回填+lxml 退路可行。
