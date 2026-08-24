# AGENTS.md — obsidian-rag 项目维护指引（AI 长期指令）

> 本文件给**在本仓库工作的 AI agent**：接手维护/开发前必读。
> 历史使命（验证 fix/audit-2026-08-14 分支）已完成并合入 main，旧版内容见 git 历史。

## 项目一句话

个人 Obsidian 知识库的本地语义检索系统：多格式文档（md/txt/pdf/docx）→ 切块 →
BGE-M3 嵌入 → Chroma；混合检索 + 重排；MCP server 接 opencode；Flet 桌面 GUI。

## 当前状态速览（2026-08-24）

- META_VERSION **9**：多格式提取 + 统一终态 + 原始字节指纹
- 多格式默认开启（`extensions=md,pdf,docx`）；扫描件 OCR 经 MinerU 云端 API（`pdf_scan_backend`，默认 none）
- **人机分权门禁**：Agent 触发的索引默认只处理文本类 + `agent_formats` 已批准格式，
  未授权二进制文件被冻结（保留条目与块）；批准一次长期有效、可撤销
- 详细机制：`AI_GUIDE.md`（部署/使用）、Vault 内 `20-Projects/Obsidian RAG/` 文档组、
  开发史 `TASK_LOG.md`（问题 1–27）、路线 `TODO.md`

## 环境与命令（Windows / PowerShell）

```powershell
# Python 3.14 + .venv；跑任何 python 前建议：
$env:PYTHONIOENCODING = "utf-8"

# 回归测试六件套（改动后必须全绿才算完成）
.venv\Scripts\python tests\audit_regression_test.py      # 19 用例
.venv\Scripts\python tests\library_registry_test.py      # 15 用例
.venv\Scripts\python tests\server_singleton_test.py      # 5 用例
.venv\Scripts\python tests\test_config_editor.py         # 6 用例
.venv\Scripts\python tests\test_gui_store.py             # 30 用例
.venv\Scripts\python tests\test_extractors.py            # 25 用例（含 mock HTTP）
.venv\Scripts\python tests\verify_export_import.py       # 39 检查项（会动真库，最后跑）
```

- 管道环境跑测试必须带 `$env:PYTHONIOENCODING='utf-8'`（交互控制台可省）
- `verify_export_import` 会做真库导出/导入副本演练与一次真检索（加载模型数十秒）

## 架构红线（改代码前必读，违反 = 生产事故）

1. **extractors 契约**：`extract_to_markdown` 绝不抛异常、绝不写源目录；
   失败一律折叠 `(None, reason)`。新增格式先进 `SUPPORTED_EXTS` 单一事实来源。
2. **统一终态**：一切"不产块的文件"必须落持久化终态（`_terminal_entry`，
   reason ∈ unreadable/extract-failed/empty/tbd/scanned），否则每轮误判 stale 死循环。
   二进制失败终态必须带 `xsrc=current_backend_sig()`。
3. **None/unreadable 判定严格先于 is_tbd_heavy**；成功路径统一在 stat 后
   `current_rels.add(rel)`——漏加 = 条目被裁剪、块被当幽灵清理。
4. **一致性自愈**：meta 期望块数 ≠ Chroma 实际 → 自动转全量重建。
   全终态库（0==0）不触发。别动这个分支的判空基准（真实条目数，非原始 dict）。
5. **改切块/清洗逻辑必动 META_VERSION**；改提取逻辑必动 EXTRACT_VERSION。
6. **Agent 门禁语义不可回退**：`agent_allowed` 冻结分支在最早期（stat 前），
   保证未授权文件零 I/O 且条目永不被裁剪；kb_stale 与 _index_core 必须同步修改。
7. **GUI 是零侵入观察者**：不加载模型索引、不直写 Chroma；进程治理只用
   `gui/stop.py`（禁止单杀 PID，flet.exe 会成孤儿）。
8. **API Key 不进日志**：MinerU 客户端的错误消息只含类型与摘要。

## 测试纪律

- 新功能必须带用例进对应测试文件（风格对齐 audit_regression_test.py：标准库、
  逐用例 PASS/FAIL、`_run_all()` 运行器）；修 bug 先写复现用例再修
- 索引集成测试用 `_IsoEnv`（重定向落盘路径 + 假编码器 numpy 零向量），绝不碰真实模型/Chroma
- 外部 HTTP（MinerU）用注入 fake `requests` 模块测；真实 Key 冒烟单独人工执行

## 提交与文档

- 提交信息中文、`feat:/fix:/docs:/test:` 前缀；按里程碑提交，未经用户要求不 push
- 用户可见的行为变更三处同步：`TASK_LOG.md`（问题编号叙事）、`TODO.md`（状态勾选）、
  Vault 的 `20-Projects/Obsidian RAG/` 文档组（用户手册视角）
- 拿不准的设计决策：停下问用户，不要自行扩大范围
