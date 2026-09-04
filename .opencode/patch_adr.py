"""patch_adr.py — 决策记录追加 ADR-14~17。"""
from pathlib import Path

p = Path(r"D:\_STOREROOM\lol\Obsidian Vault\20-Projects\Obsidian RAG\Obsidian RAG 决策记录.md")
t = p.read_text(encoding="utf-8")

adr = """

## ADR-14 多格式支持与人机分权门禁（2026-08-24）

- **状态**：已接受。META_VERSION 8→9 全量重建（Obsidian Vault 1594 块）；7 个 DOCX 已入库。
- **背景**：vault 里积累的 PDF/DOCX 完全在检索之外；且 AI Agent 会经 MCP 触发索引/自动同步——
  用户要求：多格式默认开启、GUI 可自选格式并持久化，但**未经用户批准的文档类型，
  Agent 不得重建/增量纳入**（含检索触发的自动同步）。
- **决策**：
  1. `extensions` 默认 `md,pdf,docx`（库未显式设置即继承）；GUI 库配置改勾选块。
  2. 注册表新增 `agent_formats`：Agent 可自动处理的二进制格式授权清单；
     生效值 = extensions ∩ agent_formats（收窄 extensions 授权自动失效）。
  3. 门禁语义 = **冻结而非拒绝**：Agent 轮次对未授权文件零 I/O、不转换、条目与块原样保留
     （绝不裁剪清理，否则等于变相删库）；人类路径（GUI/CLI）不受限。
  4. 批准粒度：一次批准长期有效（写注册表持久化），GUI 取消勾选即收回。
- **后果**：Agent 永远可维护文本层；二进制内容的进出完全由用户掌控。
  红线：冻结分支必须在 stat 之前（零 I/O），且 kb_stale/_index_core 同步实现。

## ADR-15 扫描件 OCR：MinerU 云端 HTTP API 直连（2026-08-24）

- **状态**：已接受（R3a）。R3b 本地部署搁置。⏳ 真实 Key 云端冒烟待做。
- **背景**：扫描件 PDF 无文字层，本地直提无能为力。原计划经 MinerU-Open-CLI 子进程调用，
  但 CLI 需额外安装且有子进程卫生负担；用户明确要求 API Key 可在 config 与 GUI 双端设置。
- **决策**：直连 mineru.net REST API（file-protocol/batch：申请批任务→预签名 PUT 上传→
  轮询→下载 zip 取正文）。三键配置：pdf_scan_backend（none/mineru-cloud）、mineru_api_key、
  mineru_timeout_seconds——config.json 与 GUI 设置页同一份存储，后保存者生效。
- **后果**：零安装依赖、超时可控、错误折叠进统一终态；Key 不落日志。
  缓存键升级 `<md5>.<route>.v2`（route=local/ocr:mineru-cloud），后端切换不丢历史成果。
  遗留：外部契约仅 mock 验证，真实 Key 冒烟待用户配合。

## ADR-16 统一终态机制与一致性自愈推广（2026-08-24）

- **状态**：已接受。META v9 核心机制。
- **背景**：多格式引入后「不产块的文件」种类暴增（扫描件/坏文件/空文件/TBD 重）。
  若不入 meta：每轮重计 added → 每轮误判 stale → 死循环；若转换失败无记录：
  无法 O(1) 跳过。另实测两起分叉：进程被杀致 WAL 段丢失（HEBAT3 104 块蒸发）、
  并发写入致 2370 vs 1594 分叉——旧逻辑每轮报 stale 却永远修不回。
- **决策**：
  1. 一切不产块文件落持久化终态 `_terminal_entry(xfail, reason, chunks=0)`：
     unreadable（OSError 哨兵指纹两轮判稳）/ extract-failed / empty / tbd / scanned。
  2. 指纹统一为**原始字节 MD5**（与旧内容哈希在合法 UTF-8 上等值，免迁移）。
  3. 终态条目带 `xsrc` 能力签名；签名变化穿透 size+mtime 快速路径 → 自动重试转正。
  4. 一致性自愈推广：meta 期望块数 ≠ Chroma 实际 → 自动转全量重建（部分丢块可修；
     全终态库 0==0 不误伤）；kb_stale 判空以真实条目为准（全删收敛态不再无限误报）。
- **后果**：实测自愈 1490→1594（38.9s）、2370→1594 并发分叉各一起。
  红线：终态条目必须 current_rels.add 持久化；None/unreadable 判定严格先于 TBD 判定。

## ADR-17 提取试验台：所见即所得的转译预览（2026-08-24）

- **状态**：已接受（GUI 库管理工具栏入口）。
- **背景**：OCR/提取质量需要"选个文件试试看"的低成本验证途径；用户类比 Google Translate
  左右对照，但自己指出双栏空间利用率低。
- **决策**：单文件走与索引**完全相同**的管线（extract_preview → _extract_full，含缓存与
  当前后端），布局 = 顶部控制行（选文件/开始/后端单次覆盖下拉）+ 信息徽章（路由/耗时/
  字符数/缓存命中/失败指引）+ 整幅结果区（渲染 Markdown 与源码两页签自绘切换）。
  预览不写索引终态/meta；提取跑 daemon 线程 + 进度条秒表心跳；中断无需回滚（缓存原子写）。
  后端下拉为单次覆盖（extract_preview(backend=…) 参数穿透），不污染全局配置。
- **后果**：调 OCR 质量从"改配置→全量索引→检索检验"三步变一步。适配 flet 0.86 新控件模型
  （FilePicker 异步 pick_files、弃用新签名 Tabs 改自绘页签按钮）。
"""

t = t.rstrip() + "\n" + adr + "\n"
p.write_text(t, encoding="utf-8", newline="")
print("决策记录 OK,", len(t.splitlines()), "行")
