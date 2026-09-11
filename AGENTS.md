# AGENTS.md — obsidian-rag 项目维护指引（AI 长期指令）

> 本文件给**在本仓库工作的 AI agent**：接手维护/开发前必读。
> 历史使命（验证 fix/audit-2026-08-14 分支）已完成并合入 main，旧版内容见 git 历史。

## 项目一句话

个人 Obsidian 知识库的本地语义检索系统：多格式文档（md/txt/pdf/docx）→ 切块 →
BGE-M3 嵌入 → Chroma；混合检索 + 重排；MCP server 接 opencode；Flet 桌面 GUI。

## 当前状态速览（2026-09-06）

- **GUI 双实现并存**：原 Flet GUI（`python gui/app.py`）保留可用；**guiweb**（问题42，
  `python guiweb/app.py`）= pywebview 桌面壳 + HTML/JS 前端（深黑玻璃 + 全库图谱），
  后端复用 store/worker/config_editor/library 零逻辑重写，GUI 仍是零侵入观察者。
  契约见 `guiweb/contracts.md`，功能对齐清单 `guiweb/FEATURE_PARITY.md`，接线检查
  `guiweb/wiring_check.py`（纳入 tests/test_guiweb.py 回归）。改 store/config_editor
  等共享层时两套 GUI 都要过一遍
- **路径级勾选建模（问题44）**：`libraries.json` 每库 `selection_in`/`selection_out`
  （显式勾选/排除，相对路径）+ 中性默认 `selection_new_files`。判定优先级
  （问题47 改为谁具体听谁的）：**最近显式命中 vs 目录排除按深度，更具体的赢**
  （文件点名可穿透继承的目录排除；反之更具体的排除也赢）；**同位置打架**
  （纳入目标本身就在 exclude_dirs 里）排除站住，且 `set_selection`/MCP 提案
  拒绝新建此类状态（GUI 点击即弹窗：仅本库移除排除并纳入 / 放弃）；文件名/
  格式类规则一律最弱，显式静默穿透。裁决唯一实现 `library.decide_included`，
  显示与漏斗共用，勿各写一份。
  过滤在 `collect_md_files` 唯一漏斗（新增 selection 参数）——**新增文件枚举路径
  必须穿它**，否则被排除文件会漏进管线。格式开关变更经 bulk_for_extensions 批量
  勾/取消（文件级条目跟着迁移，文件夹级不动）。Agent 改勾选只走 MCP 两段式硬门禁
  （selection_gate.py：提案号+确认码+TTL+一次性，无配置绕过），GUI 直改免码
 - META_VERSION **11**：索引层提取噪声清洗（v10 死图链/页码/样板行启发式；v11 起 MinerU
   云端新提取文件带官方块标注 sidecar `{md5}.v{EXTRACT_VERSION}.mineru.json`，双轨：
   有 sidecar 精确删 header/footer/page_number、无则启发式兜底）；EXTRACT_VERSION **4**
   ——sidecar 与 md 同 md5 前缀同版本联动，EXT 不动 = 老缓存不重提、老文件无 sidecar
   走启发式；sidecar 只在 MinerU 云端解包处落（`_mineru_poll_result`），local 直提不产
- **全局回收（问题49）**：每轮整库索引**全部成功后**自动跑
  `index.prune_unreferenced_data`——删提取缓存孤儿（md/sidecar/tmp）、已删库的
  指纹与残留 Chroma collection；多库 meta 指纹并集 + 在途 MinerU 断点簿记 = 活着集，
  幂等、失败降级只记日志。任何一库失败（had_error）跳过回收。测试
  `tests/test_prune.py`。改"哪些算活着/何时触发"的语义时两处触发点
  （CLI `__main__`、`server._run_index`）与存活集构造要一起过一遍
- 多格式默认开启（`extensions=md,pdf,docx`）；扫描件 OCR 三档（`pdf_scan_backend`，
  默认 none）：none 跳过 / mineru-cloud 云端 API / **mineru-local 本机 pipeline
  服务（问题51 R3b：uv tool 的 py3.12 跑 `mineru_server.py` :9102，串行+懒加载+
  空闲卸载，仲裁双向抢占；服务瞬态不可用记 deferred 本轮跳过不落终态；单文件
  页上限 `mineru_local_max_pages` 默认 200）**；EXTRACT_VERSION **5**
- **混合型 PDF（任一页无文字层，含"PPT 文字页+扫描图"混装课件）整本按扫描件路由**（问题34）：
  云端开 → 整本 is_ocr=True 送 MinerU vlm 认字产出一份完整 MD；未开 → 整本 scanned 终态待
  xsrc 自愈——宁可诚实空缺，绝不产出"文字页直提+图片页丢失"的半份拼接内容
- MinerU 云端并行批量（问题35/36）：扫描段 `classify_extraction` 分流攒批 → 线程池只并行
  网络 I/O（`mineru_concurrency`：默认 3；0=最大吞吐——限速闸门自动节流、完成一个补一个
  直到完工；1=串行回退；worker 不触碰 pymupdf）→ 结果回主线程单线程收口；滑动窗口限速
  （`mineru_rate_per_minute` 默认 45）+ 提交错误三分类重试 + 断点簿记
  （`data/extract_cache/mineru_pending.json`，中断任务下轮续接不重复提交）
- 有文字层 PDF 可选 `pdf_text_backend` 送 MinerU 换更准的版面/表格结构识别（`is_ocr=False`，默认 local）
- **人机分权门禁**：Agent 触发的索引默认只处理文本类 + `agent_formats` 已批准格式，
  未授权二进制文件被冻结（保留条目与块）；批准一次长期有效、可撤销
- 笔记双链关系查询（出链/入链）：GUI 语义检索卡结果可内联展开"关联笔记"，
  另有 MCP 工具 `note_relations`
- **检索结果自适应建议（问题55，`advice.py`）**：检索返回里随结果给 1~2 条"下一步怎么做"
  （≥3 条强命中 → 提醒调大 top_k；≥2 条同标题不同路径 → 那是两篇笔记；命中落 agents/skills
  非笔记库 → 只要笔记请传 libraries；整批偏低 → 换笔记里的原始术语或先 `include_body=false`
  摸底；含 pdf/docx → `read_document`/`navigate_knowledge`；已折叠回填 → 要原文用
  `read_document`…共 12 条规则，表见模块 docstring）。纯规则零 I/O、同一输入必然同一输出，
  `tests/audit_regression_test.test_result_advice_rules` 逐条钉住；阈值与
  `retriever.CONF_TIER_STRONG` / `confidence_warn_threshold` 同尺度，**换打分模型要重测**。
  纪律：建议行一律不以 `[来源]` 开头 → 两套 GUI 都按提示横幅渲染，改这个协议要同时改
  `guiweb.parse_search_text` 与 `gui/widgets._render_results`（后者曾把提示行渲染成畸形结果卡）
- **WEMM 页级视觉导航（问题37/38，默认开启 `wemm_backend=on`）**：`wemm_server.py`（全局
  Python 跑，懒加载 + 空闲卸载显存 + 空闲自退出；由 gpu_arbiter.ensure_server 懒拉起——且建页库已接入索引管线：index_library 文字索引完成后自动同步页库（问题41 附记），异常只记日志绝不波及文字索引）+ `wemm_indexer.py`（页向量独立 collection
  `<collection>.wemm` / 独立 meta / 独立 WEMM_VERSION；终态与成功条目带
  `xsrc=wemm:<模型>:<维度>:<DPI>` 签名，失败每轮真重试、改档自动重渲染；upsert 分批、
  写库成功才落成功 meta）+ `wemm_retriever.py`（查询零写副作用）。MCP：`navigate_knowledge`、
  `read_document`（零触发只读缓存，交付全文）、`find_duplicates`（dedup.py MinHash+LSH 只读）、
  `index_failures`、`wemm_status`。GUI「文件生效明细」面板（问题40）：逐文件展示
  索引/页库状态与失败原因、点行打开源文件，数据全走 gui/store 零侵入只读函数。长驻 MCP
  进程配置一律经 `config.reload_config()` 现读（问题39），不要再读 import 快照
- **GPU 显存仲裁（问题41，`gpu_arbiter.py`）**：同一时刻只让一个模型驻留显存——WEMM 加载
  前等空闲显存 ≥5.5GB；bge-m3 加载前显存不足则 evict WEMM（检索优先，被抢占批次由页索引
  终态下轮重试）；server 空闲 600s 自动卸载 bge-m3/reranker；WEMM 空闲 5min 卸显存 +
  30min 自退出、按需自动拉起。**fail-open 铁律：显存探测失败绝不阻塞任何路径**
- 详细机制：`AI_GUIDE.md`（部署/使用）、Vault 内 `20-Projects/Obsidian RAG/` 文档组、
  开发史 `TASK_LOG.md`（问题 1–44）、路线 `TODO.md`

## 环境与命令（Windows / PowerShell）

```powershell
# Python 3.14 + .venv；跑任何 python 前建议：
$env:PYTHONIOENCODING = "utf-8"

# 回归测试（改动后必须全绿才算完成；单进程 A→B→C，实测约 45 秒）
.venv\Scripts\python tests\run.py                  # 统一入口：14 套全跑 + 套级计时 Top10
.venv\Scripts\python tests\run.py --suite test_dedup   # 只跑某套（调试用）
.venv\Scripts\python tests\run.py --list           # 只列分组与顺序
# 各文件仍可单独跑（用法不变，断言一个没删）：
# audit / library_registry / server_singleton / test_config_editor /
# test_gui_store / test_extractors / verify_export_import / test_wemm_indexer /
# test_wemm_retriever / test_dedup / test_gpu_arbiter / test_selection /
# test_guiweb（随 guiweb 回归）+ tests/smoke_gui.py（手动冒烟，不进回归）
```

- 管道环境跑测试必须带 `$env:PYTHONIOENCODING='utf-8'`（交互控制台可省）
- `tests/hidden-vault/` 是固定隐藏测试库（14 种文档全覆盖，平时不可见、
  永不进 `libraries.json`）：`verify_export_import` 默认连它，真 `data/` 与真
  Vault 零触碰；缺失时自动由 `tests/make_hidden_vault.py` 重造。
  `--vault <路径>` 可显式指定真实库做里程碑前手动演练
- `verify_export_import` 真模型只加载 1 次（隐藏库索引 embedding + 1 次真检索，
  进程内复用）；两库对比走向量余弦免模型；导出/导入/损坏/留3个全部进程内
  直接调函数，零子进程

## 架构红线（改代码前必读，违反 = 生产事故）

1. **extractors 契约**：`extract_to_markdown` 绝不抛异常、绝不写源目录；
   失败一律折叠 `(None, reason)`。新增格式先进 `SUPPORTED_EXTS` 单一事实来源。
   `finally` 块里的清理调用（如 `doc.close()`）也要包一层 `try/except: pass`——
   `finally` 内代码自己抛异常会覆盖 `except` 分支已产生的 return 值并继续外泄，
   等于让"绝不抛异常"这句承诺在收尾这一步失效（2026-08-25 council 审计实测）。
   同理：except 回滚分支引用的变量必须在进入 try 前绑定，用 `x = None` 替代
   `del x` 且置于作用域内最后一个可抛调用之后——`del` 后引用即 UnboundLocalError
   掩盖原始异常（2026-08-26 council 实测，index.py `_try_switch_back_cuda`）。
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
   `gui/stop.py`（禁止单杀 PID，flet.exe 会成孤儿）。任何"仅供预览/测试"的旁路
   工具（如提取试验台）若复用与正式索引相同的共享状态（缓存目录等），必须显式
   隔离（`tempfile` + `set_cache_dir` 之类），否则会静默绕开用户配置的隐私/后端
   开关——一次性的手动测试会在用户不知情下变成正式生效的内容（2026-08-25 council
   审计发现：试验台测云端 OCR 曾写进生产缓存，之后 backend=none 的索引照样命中）。
8. **API Key 不进日志**：MinerU 客户端的错误消息只含类型与摘要。

## 测试纪律

- 新功能必须带用例进对应测试文件（风格对齐 audit_regression_test.py：标准库、
  逐用例 PASS/FAIL、`_run_all()` 运行器）；修 bug 先写复现用例再修
- 索引集成测试用 `_IsoEnv`（重定向落盘路径 + 假编码器 numpy 零向量），绝不碰真实模型/Chroma
- 外部 HTTP（MinerU）用注入 fake `requests` 模块测；真实 Key 冒烟单独人工执行
- 跨模块落盘补丁必须全覆盖 import 期绑定值：`retriever.CHROMA_DIR` 是
  `from index import` 绑定的旧值，打 `index.CHROMA_DIR` 补丁够不着——漏改会让
  检索单例连真实库并建空 collection 污染真库（2026-09-10 verify 隔离化实测：
  真库多了个空 `kb_hidden-test`，已清理）。改完用真库 collection 清单核对
  （应为 9 个，无新增）。同理 `export/import.py` 的 `CHROMA_DIR/DATA_DIR` 与
  `EXPORT_DIR/IMPORT_WORK_DIR/VAULT_EXPORT_DIR/ARCHIVE_DIR` 常量也要同步改，
  且绝不 `reload(import)`（会把补丁重置回真实路径）
- 单进程跑全量时各套件前后快照/还原共享全局（见 `tests/run.py`），新测试文件
  若引入新的模块级落盘路径，记得加进快照表

## 提交与文档

- 提交信息中文、`feat:/fix:/docs:/test:` 前缀；按里程碑提交，未经用户要求不 push
- 用户可见的行为变更三处同步：`TASK_LOG.md`（问题编号叙事）、`TODO.md`（状态勾选）、
  Vault 的 `20-Projects/Obsidian RAG/` 文档组（用户手册视角）
- 拿不准的设计决策：停下问用户，不要自行扩大范围
