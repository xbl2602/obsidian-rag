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
- **六件套回归**: 全绿（extractors 71/71, audit 38/38, registry 15/15, singleton 5/5, config_editor 0, gui_store 0, verify_export_import 39/39）

---

## 最近三笔提交（2026-09-03）

| 提交 | 内容 |
|---|---|
| `06f397a` | **问题34**：PDF 分拣规则改为「存在图片页即整本按扫描件路由」——混合型课件（PPT 文字页 + 教材扫描图）不再静默丢失图片页内容。未开云端时整本跳过并标 scanned 终态；开启后整本送 MinerU vlm 认字产出一份连贯完整的 MD。 |
| `cb1be8e` | **问题35**：MinerU 云端批量并行加速——限流闸门、错误三分类重试、断点簿记与续接、Token 失效自动停整批。测试 +11 例，六件套全绿。 |
| **即将提交** | **问题36**：max 模式（`mineru_concurrency=0`）——不设固定并发，提交节奏交给滑动窗口限速闸门自动节流（窗口没满立刻送、接近频控停下、窗口滑动续送），任务完成腾出线程立刻补位直到全部完工；轮询遇 429/5xx 在 deadline 内退避续询不误判失败。测试 +4 例，六件套全绿。 |

---

## 问题脉络（从调研纪要到我接手时已完成的范围）

1. **问题33**（已在，问题33 修复）：`model_version` 从未显式传，一直用较弱的默认 pipeline
2. **问题34**（已完成并提交 06f397a）：PDF 分拣规则整本二分 → 存在图片页即整本按扫描件
3. **问题35**（已完成并提交 cb1be8e）：串行送云端 → 并行批量（ThreadPoolExecutor + 限速 + 错误分类 + 断点续接）
4. **问题36**（即将提交）：固定并发上限 → max 模式（concurrency=0），限速闸门唯一节流
5. **read_document MCP 工具**：用户拍板暂缓（主力模型已有视觉能力，纯语言模型路径无真实使用者）
6. **WeMM 页级视觉导航**：纪要自己排在问题 1-4 之后评估，1-4 完成后若用户真需要再议

---

## 用户侧待完成事项

- 开启 mineru-cloud：GUI 设置页「常用 → PDF 与云端 OCR」→ 扫描件 OCR 后端选 `MinerU 云端 OCR`（Key 已配）
- 导入课件：PDF 放进 vault，下一轮自动同步整本云端认字入库
- 真实课件验证（可选）：用户手上两份实测课件（ManometerEquation、Note9），若能给路径可做完整性验证

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
.venv\Scripts\python tests\verify_export_import.py       # 39
```

---

## 文档位置

- **AGENTS.md**（本项目 AI 指令）：当前状态速览 + 架构红线
- **AI_GUIDE.md**：部署/使用手册
- **TASK_LOG.md**：问题 1–36 完整开发史
- **TODO.md**：路线图与 Backlog
- **Vault 内** `20-Projects/Obsidian RAG/`：用户视角文档组

---

> 下次接手时：先读本 HANDOFF + AGENTS.md，核对六件套是否仍全绿，再决定从 Backlog 哪个条目继续。