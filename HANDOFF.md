# HANDOFF — obsidian-rag 当前状态（2026-09-03）

> 本文件给**未来接手的 AI agent**：一页看懂项目现状、最近三笔改动在做什么、用户侧待完成事项。

---

## 项目定位

个人 Obsidian 知识库的本地语义检索系统：多格式文档（md/txt/pdf/docx）→ 切块 →
BGE-M3 嵌入 → Chroma；混合检索 + 重排；MCP server 接 opencode；Flet 桌面 GUI。

---

## 当前代码状态

- **META_VERSION**: 9（多格式 + 统一终态 + 原始字节指纹）
- **EXTRACT_VERSION**: 4（PDF 分拣规则 + MinerU model_version 参数）
- **WEMM_VERSION**: 1（页级视觉导航独立版本号，独立自愈，互不影响文字索引）
- **六件套回归**: 全绿（extractors 71/71, audit 38/38, registry 15/15, singleton 5/5, config_editor 0, gui_store 0, verify_export_import 39/39）
- **新增三套**: 全绿（test_wemm_indexer 26/26, test_wemm_retriever 13/13, test_dedup 19/19）

---

## 最近三笔提交（2026-09-03）

| 提交 | 内容 |
|---|---|
| `cb1be8e` | **问题35**：MinerU 云端批量并行加速——限流闸门、错误三分类重试、断点簿记与续接、Token 失效自动停整批。 |
| `bd24227` | **问题36**：max 模式（`mineru_concurrency=0`）——不设固定并发，提交节奏交给滑动窗口限速闸门自动节流；轮询遇 429/5xx 在 deadline 内退避续询不误判失败。 |
| **本次提交** | **问题37**：WEMM 页级视觉导航 + read_document + 近似去重（见下"最近这笔改动"） |

---

## 最近这笔改动（问题37，本次提交）

**WEMM 页级视觉导航**：把 PDF 每页渲染成图 → WeMM-Embedding-2B（腾讯微信视觉团队，512 维，
全局 Python 装 torch/transformers，项目 .venv 零依赖）→ 每页一个向量 → 独立 `wemm_<collection>`
Chroma 页库 + 独立 `data/wemm_meta_<库>.json`。检索时 `navigate_knowledge` 告诉 AI"内容在
哪个 PDF 的第几页"（扫描件也能定位）。**默认关闭**（`wemm_backend=off`，隐私/显存优先）。

- 新增文件：`wemm_server.py`（本地看图 HTTP 服务，端口 9101，可 `--unload-after` 空闲释放显存）、
  `wemm_indexer.py`（逐页渲染→页向量，带门禁/终态/一致性自愈/精确清理）、`wemm_retriever.py`（页级导航检索）
- MCP 新增：`navigate_knowledge`、`read_document`（取完整正文+绝对路径，零触发只读缓存）、`find_duplicates`
- `dedup.py`：文本级 MinHash+LSH 近似去重（只读建议，绝不删改文件，不触发 OCR/云端）
- config 三处同步：`wemm_backend`/`wemm_url`/`wemm_model`/`wemm_dim`（off / 127.0.0.1:9101 / tencent/WeMM-Embedding-2B / 512）
- 冒烟：ManometerEquation.pdf（9 页）入库 9 向量，导航查询命中第 2/1/5/8 页（cos 0.51–0.58）；
  各 MCP 工具在 Obsidian Vault 实测可用；冒烟临时库/服务已清理（释放 ~5GB 显存）

---

## 问题脉络（从调研纪要到我接手时已完成的范围）

1. **问题33**：`model_version` 从未显式传，一直用较弱的默认 pipeline（已修）
2. **问题34**：PDF 分拣规则整本二分 → 存在图片页即整本按扫描件（06f397a）
3. **问题35**：串行送云端 → 并行批量（cb1be8e）
4. **问题36**：固定并发上限 → max 模式（bd24227）
5. **问题37（本次）**：WEMM 页级视觉导航 + read_document + 近似去重——三条一起落地

---

## 用户侧待完成事项

- 开启 mineru-cloud：GUI 设置页「常用 → PDF 与云端 OCR」→ 扫描件 OCR 后端选 `MinerU 云端 OCR`（Key 已配）
- 导入课件：PDF 放进 vault，下一轮自动同步整本云端认字入库
- 真实课件验证（可选）：用户手上两份实测课件（ManometerEquation、Note9），若能给路径可做完整性验证
- 视觉导航（可选，默认关闭）：需要时按《操作手册》「视觉导航（WEMM）」三步开启——设置页开
  `wemm_backend` → `python wemm_server.py --port 9101` → `wemm_indexer.py --library <库名> --backend on`

---

## 关键配置键（data/config.json）

| 键 | 当前值 | 含义 |
|---|---|---|
| `pdf_scan_backend` | `none` | 扫描件/混合型 PDF 的处理方式（none 跳过 / mineru-cloud 云端 OCR） |
| `mineru_api_key` | 已配置 | mineru.net API Token，敏感信息不进日志 |
| `mineru_concurrency` | `3` | 0=最大吞吐、1=串行、≥2=固定并发 |
| `mineru_rate_per_minute` | `45` | 每分钟提交上限（官方 50/分钟，留余量） |
| `mineru_timeout_seconds` | `600` | 单文件提交+轮询+下载总超时 |
| `mineru_model_version` | `vlm` | 云端解析模型（pipeline 更省配额，vlm 精度更高） |
| `extensions` | `md,pdf,docx` | 多格式默认开启 |
| `wemm_backend` | `off` | 页级视觉导航开关（off / on-local）；默认关，隐私/显存优先 |
| `wemm_url` | `http://127.0.0.1:9101` | 本地 WEMM 看图服务地址 |
| `wemm_dim` | `512` | 页向量维度（WeMM-Embedding-2B matryoshka） |

---

## 架构红线（改代码前必读，违反 = 生产事故）

1. extractors 契约：绝不抛异常、绝不写源目录，失败一律折叠 `(None, reason)`
2. 统一终态：一切不产块的文件落持久化终态，扫描件类终态带 xsrc 能力签名
3. API Key 不进日志
4. GUI 是零侵入观察者：不直写 Chroma，只读进度/meta 文件
5. 测试先于修改：六件套全绿才能提交

---

## 测试命令

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python tests\audit_regression_test.py      # 38
.venv\Scripts\python tests\library_registry_test.py      # 15
.venv\Scripts\python tests\server_singleton_test.py      # 5
.venv\Scripts\python tests\test_config_editor.py         # 0
.venv\Scripts\python tests\test_gui_store.py             # 0
.venv\Scripts\python tests\test_extractors.py            # 71
.venv\Scripts\python tests\test_wemm_indexer.py          # 26
.venv\Scripts\python tests\test_wemm_retriever.py        # 13
.venv\Scripts\python tests\test_dedup.py                 # 19
.venv\Scripts\python tests\verify_export_import.py       # 39
```

---

## 文档位置

- **AGENTS.md**（本项目 AI 指令）：当前状态速览 + 架构红线
- **AI_GUIDE.md**：部署/使用手册
- **TASK_LOG.md**：问题 1–37 完整开发史
- **TODO.md**：路线图与 Backlog
- **Vault 内** `20-Projects/Obsidian RAG/`：用户视角文档组

---

> 下次接手时：先读本 HANDOFF + AGENTS.md，核对六件套是否仍全绿，再决定从 Backlog 哪个条目继续。