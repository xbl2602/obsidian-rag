# 个人 AI 工程助手 — 任务记录

> 开发记录：Obsidian Vault + RAG + MCP + AI Agent 全链路搭建与调优
> 更新：2026-08-11

---

## 1. 任务背景

用户（USM 航空航天工程学生）用 Obsidian 维护个人知识库，目标是搭建"个人 AI 工程助手"：

```
工程师
  + AI Agent（opencode + DeepSeek v4 flash API）
  + 知识系统（Obsidian Vault 语义检索）
  + 自动化（MCP 工具）
```

**核心问题**：Vault 文件量小（117 个知识文件）但内容零散，文件粒度的检索失效 ——
AI 若想回答"免费证书有哪些"，必须通读全文才能定位到分散的笔记。

**关键决策**：
- 检索层全本地（数据不出本机），仅问答内容发给 DeepSeek API
- Embedding 用 BGE-M3（多语言、0.6B、8192 上下文），向量库用 Chroma
- 需要 RAG 的原因不是文件多，而是"内容分散 + 抽象查询无法定位"
- 不自建 MCP 服务器（用 opencode 原生工具），本项目只提供检索能力

**交付物**：
- 架构文档：`个人AI工程助手-系统架构方案.md`（桌面）
- 检索系统：本项目 `obsidian-rag/`（索引 + 混合检索 + MCP 服务器）

---

## 2. 解决的问题（按时间线）

### 问题 1：精确名词能命中，泛化查询失败

- **现象**：搜 "IBM SkillsBuild" 命中；搜 "免费证书" 返回 `目录.md` 等无关文件，
  完全错过 `OTHER CERT/论点/免费入门证书清单.md`。
- **根因**：
  1. 纯 dense 检索对抽象查询语义泛化不足
  2. 结构类文件（`目录.md`、`AGENTS.md`、`LOG.md`）污染结果
  3. opencode 会话记录（`session-ses_*.md`）被当知识索引
- **解决**：见下文 §4。

### 问题 2：全量重建会把索引清空（数据丢失）

- **现象**：`--full` 先 `delete_collection` 清库，随后进程被中断 → Chroma 变成 0 块。
- **根因**：清库是立即的，重建是异步慢速的，中断窗口导致数据真空。
- **解决**：这是操作层面问题，不是代码 bug；恢复方法 = 重跑 `--full`。
  已在本文件 §5 记录"不要在重建中途强杀进程"。

### 问题 3：CUDA 版 PyTorch 装不上（Smart App Control 拦截）

- **现象**：`torch 2.13.0+cu130` 加载报 `OSError: [WinError 4551]`
  "An Application Control policy has blocked this file"。
- **根因**：Windows 11 的 Smart App Control（SAC，enforce 状态）基于**云端信誉**
  拦截低信誉 DLL。cu130 是最新构建，`torch_global_deps.dll` 信誉不足被拦；
  `torch_cpu.dll` 同样无签名却能加载，证明拦截是信誉驱动而非签名驱动。
- **解决**：降级到 `torch 2.11.0+cu128`（已发布约半年，信誉充分，
  且 RTX 5060 Blackwell sm_120 要求 CUDA ≥12.8，cu128 恰好满足）→ 一次通过。

### 问题 4：Python 3.14 兼容性顾虑（未发生，属预判）

- 起初怀疑 torch CUDA 版不支持 Python 3.14 需降级。
- 实测官方支持 Python 3.10-3.14，`cp314-win_amd64` wheel 存在，无需降级。

### 问题 5：库悄悄过期（指纹检查缺失）

- **现象**：每次搜索前 AI 必须**记得**调 `reindex_knowledge`，否则库陈旧。
- **方案 B（已实现）**：新增 `kb_stale()`（`index.py`）指纹检查——
  只扫 Vault 文件 MD5 对比 `index_meta.json`，不加载模型、毫秒级。
  `search_knowledge` 调用前 `ensure_fresh()`（`server.py`）自动增量同步，
  AI 无感知、库永远新鲜。变更时返回结果开头提示。
- **附带**：文件发现逻辑抽为 `collect_md_files()`，索引与指纹检查共用同一套
  过滤规则，避免误报。

### 问题 6：双语检索弱（中文查不到英文笔记）

- **现象**：中文查询"仿真配置/设计参数"搜不到 `HEBAT3_ORK_Design_Parameters.md`
  （全英文内容）。dense 相似度低（0.555），BM25 中文 2-gram 与英文 token 零重叠（score=0）。
- **方案（已实现）**：**索引期中文锚点增强**——切块时若文件 title/summary 含中文
  且正文以英文为主，把中文元数据拼到块头（`index.py` `make_anchor`）。
  - dense 相似度 0.555 → 0.641（提升 15.5%）
  - BM25 中文 2-gram 能通过锚点命中英文块
  - 锚点**只加首块**（chunk=0），避免逐块冗余；存 metadata `anchor` 字段
  - 检索显示时按 metadata 精确前缀剥除（`retriever.py` 用 `meta.get("anchor")`，
    不用正则），LLM 只看到原文
- **验证**：中文查询"HEBAT3 仿真配置 设计参数 飞行数据"直接命中
  "3. Simulation Configuration" 块。

### 问题 7：代码审查发现的三个真实 Bug（2026-08-02 修复）

用户对 `index.py` / `retriever.py` 做了逐行 code review，发现 3 个真实 Bug
（另附若干性能问题与死代码，见下）：

- **Bug 1（删除文件不清理 → 永久 stale）**：
  索引只"写新"不"删旧"，已删文件块永远留在 Chroma 污染结果；
  且指纹检查把已删文件计入 `removed` → 每次搜索都 stale → 反复全量重建。
  - 修复：清理逻辑改为按 `meta` 记录的块数生成精确 `valid` id 集合，
    **`save_meta` 前先修剪 meta 中磁盘上已不存在的文件键**（关键一步），
    无效 id 一次性 `collection.delete`。
  - 验证：建 `TMPBUGTEST.md` → 索引 → 删除 → 再索引 → "清理 1 个失效块"，
    Chroma 与 meta 均无残留，`kb_stale()` 不再误报。
- **Bug 2（块数变少留幽灵块）**：文件从 3 块缩到 1 块后，旧 id
  （`::1`/`::2`）残留。同 Bug 1 修复逻辑覆盖（valid 集按新块数生成，
  超出的旧 id 自动判失效）。
  - 验证：`GHOSTTEST.md` 3 块 → 缩为 1 块 → "清理 2 个失效块"。
- **Bug 3（--full 真空窗口）**：旧逻辑先 `delete_collection` 清库再慢速重建，
  中断即 0 块（即问题 2）。修复：`--full` 先嵌入全部新向量，**进入写锁后**
  才 `delete_collection` + upsert，真空窗口缩到毫秒级；`msvcrt` 文件锁
  `data/index.lock` 防并发写。
- **性能修复**：`kb_stale()` 快速路径（mtime+size 命中则免 MD5）；
  检索结果格式化合并 N+1 次 `get` 为单次批量 `get`；BM25 索引构建
  合并两次 `get` 为一次。
- **死代码清理**：删 `rrf_fuse()`（权重融合已够用）、`build_bm25_index()`
  （建索引是 `get_bm25` 内部步骤）、`alpha` 参数（未使用）。
- **锚点设计修正**：锚点只加首块（原方案加到每块，浪费 + 冗余）；
  剥除从 `ANCHOR_RE` 正则改为 metadata `anchor` 字段精确前缀匹配（无假阳性）。
- 验证通过后全量重建 787 块 / 117 文件（28.7s），双语 + 泛化查询回归全绿。

### 问题 8：切块切在句子中间，语义被截断（2026-08-02 修复）

- **现象**：暴力切块（1500 字符硬截断）可能在句子中间剪断，
  "在阅读本文件之前，不可以执行这个程序"被切成"执行这个程序"，
  否定/条件语义被曲解。
- **方案**：先扫描 Vault 结构（117 文件：429 块 ≤200、36 块 >1500、
  14 个文件有超长段落）——结论是不需要 embedding 语义切块，
  标题切块已覆盖 80% 文件，只对超长块做**两级边界降级**（见 §4.3）。
- **验证**：787 → 1058 块；>1500 从 36 → 5（全为纯表格，无句号可切）；
  双语/泛化/网格无关性查询回归全绿；锚点仍只在首块。

### 问题 9：第二轮 code review 的三个真 Bug + 边界项（2026-08-02 修复）

- **Bug A（folder 过滤只挡 dense）**：`where` 只作用于 dense 查询，
  BM25 全库打分 → folder 外的高分命中照样进 top_k（如 folder="ROCKETRY"
  仍能返回 OTHER CERT 内容）。**且实测发现当前 Chroma 版本已不支持
  `$startswith`**（`ValueError: Expected where operator...got $startswith`），
  即 folder 过滤的 dense 路此前一直是坏的。
  修复：弃用 where，dense 查全库候选后在内存按块 id 前缀过滤，
  BM25 侧同理按 `file` 元数据过滤——两路对称、零版本依赖。
  验证：folder="ROCKETRY" 查询 5 条结果零越界。
- **Bug B（--full 真空窗口实际 ~28s 非毫秒）**：`model.encode` 实际在
  `write_lock()` 内（注释写"锁外"是错的），全量重建时锁被持有整个编码
  时长，并发进程 LK_LOCK 重试 10s 后抛 OSError。
  修复：编码移出锁外（先编码后持锁），锁内只剩 delete/upsert/清理/
  save_meta（毫秒级）；注释同步修正。
- **Bug C（ensure_fresh 无异常保护）**：index_vault 抛错 → search 整个
  失败，LLM 连陈旧结果都拿不到。修复：try/except 降级为
  "用旧索引 + 明确提示"，检索永不整体失败。
- **边界 4（段落级块不封顶）**：多段落中单段超长不切（只对唯一段落降级）。
  修复：每个段落独立判断 >1500 再降级句子切。
- **边界 5（缩写误切）**：`Mr.`/`e.g.`/`3.14` 被当句子边界。修复：
  常见缩写保护表（大小写变体）+ 英文边界要求"点后空格+大写/数字"+
  拼接补空格防粘连。
- **潜伏项顺手处理**：index_vault 增量复用 mtime+size 快速路径
  （免读全文+MD5，与 kb_stale 同策略）；collect_md_files 只跑一遍
  （current_rels 复用）；index_meta.json 原子写（tmp + os.replace）。
- **保持现状（单用户本地，风险极低）**：`--full` 期间并发读、
  `$startswith` 版本依赖（已弃用 where，不再依赖）。

---

## 3. 困难点

| # | 困难 | 说明 | 应对 |
|---|------|------|------|
| 1 | MCP 2.0 API 变化 | 2.0.0 移除 FastMCP 高层 API，文档滞后 | 改用 `MCPServer` + `@server.tool()` + `run("stdio")`（实测可用） |
| 2 | stdio 协议污染 | `print` 到 stdout 会破坏 MCP 帧 | 所有日志走 `log()` → stderr（`index.py:26`） |
| 3 | Windows 执行策略 | `opencode.ps1` 被拦截 | 用 `opencode.cmd` 调用 |
| 4 | 中文编码 | 默认 cp1252 下中文输出崩溃 | 设 `PYTHONIOENCODING=utf-8` |
| 5 | LSP 误报 | IDE 报 `chromadb`/`sentence_transformers` 无法解析 | LSP 用系统 Python 而非 venv，属误报，忽略 |
| 6 | 陌生高 CPU 进程 | 疑为本项目残留 | 排查后确认是 ANSYS Fluent 的 python 进程，非本项目 |
| 7 | SAC 拦截 CUDA DLL | 见问题 3 | 降级 cu130 → cu128 |
| 8 | 残留进程 | 多次测试遗留 python 进程占内存 | `Get-CimInstance` 定位并 `Stop-Process` |
| 9 | stdin 中文乱码 | PowerShell 管道传中文给 python 变成 `?`，污染测试查询词 | 测试脚本写 `.py` 文件（UTF-8）执行，不用 heredoc |
| 10 | 英文笔记双语检索弱 | 中文查询对全英文内容 dense/BM25 双路失效 | 索引期中文锚点增强（见问题 6） |

---

## 4. 如何解决（设计思路）

### 4.1 检索架构：Hybrid = Dense + BM25，加权融合

纯 dense 的语义泛化不够（抽象查询失败），纯 BM25 的词匹配又不够语义。
二者互补 → 混合检索。

- **Dense**：BGE-M3 嵌入查询，与全部块做余弦相似度
  （Chroma HNSW 空间为 cosine，`index.py:98`）
- **BM25**：关键词召回（`retriever.py`）
  - 分词：英文按词 + 中文连续片段按 **2-gram** 切（兼顾中英混合）
  - 参数 k1=1.5, b=0.75
- **融合**：加权得分
  ```
  score = dense_weight * sim + bm25_weight * (bscore / (1 + bscore))
  ```
  默认 dense 0.6 / bm25 0.4（`retriever.py:121`）

### 4.2 索引清洗（消除污染）

- **结构文件排除**：`STRUCTURE_FILES = {目录.md, AGENTS.md, LOG.md, README.md}`
  这些是链接清单/指令文件，非知识本体。
- **目录排除**：`.obsidian`、`.smart-env`、`.trash`、`TEMP`、`templates`
- **会话文件排除**：`EXCLUDE_PATTERNS = ("session-", "会话", ".tmp")`
  拦截 opencode 会话日志（如 `session-ses_0439.md`）
- **效果**：全量重建后 994 → 776 块，污染项 0。

### 4.3 切块策略

- 按 Markdown H1/H2/H3 标题切块（`split_by_headings`），
  每块带 `heading` 元数据，块 id 为 `{相对路径}::{块序号}`。
- 短文件（正文 ≤200 字符）整文件作为单块，用 frontmatter `title` 做标题。
- **两级降级切块（2026-08-02，问题 8）**：标题块 >1500 字符时降级为
  段落切（`split_paragraphs`，按空行）；段落本身 >1500 再降级为
  句子切（`split_sentences`，按 `[。！？!?]` 边界）。**永不从句子中间剪断**
  （单句超长宁长勿断），彻底消除"否定/条件语义被截断"类问题。
  纯表格（无句号/空行）无法再切，属正确边界。
- frontmatter 元数据（title/tags）提取后随块存储。
- **效果**：787 → 1058 块（+271）；>1500 块从 36 → 5（全部为表格）；
  平均块长 301 字符；`原始数据.md`（22392 单块）拆为 107 个主题块。

### 4.4 增量索引

- 文件指纹存 `data/index_meta.json`（hash + size + mtime_ns）；
  未变的文件跳过，变了的重新切块嵌入（`index.py`）。
- `upsert` 按 id 幂等写入，永不重复。
- 失效块清理（Bug 1/2）：以 `meta` 中每个文件记录的块数生成精确 `valid` id 集合，
  先修剪已删除文件的 meta 条目，再 `collection.delete` 所有不在 valid 的 id
  （`index.py` 写锁内）。删除文件与块数变少两类失效一次覆盖。

### 4.5 CUDA 迁移

- `get_model()` 自动探测：`cuda` 优先，回退 `cpu`（`index.py:31-38`）。
- `SentenceTransformer(MODEL_NAME, device=device)` 一次指定设备。
- 结果：全量索引 4分16秒 → **32 秒**（约 8 倍），查询嵌入毫秒级。
- 约束：RTX 5060 是 Blackwell（sm_120），要求 **CUDA ≥12.8**；
  torch cu128 / cu130 均可，cu128 更稳（SAC 信誉通过）。

### 4.6 系统调用链

```
opencode（LLM 判断何时检索）
   → MCP stdio 调用 search_knowledge / reindex_knowledge（server.py）
      → retriever.hybrid_search()
         → get_model() [BGE-M3 on CUDA] + Chroma query + BM25
   → LLM 结合检索结果作答，附 [来源] 链接
```

注册于 `~/.config/opencode/opencode.json`（MCP `obsidian-rag`）。

---

## 5. 操作备忘（踩坑记录）

```powershell
# 运行脚本前设编码（否则中文崩溃）
$env:PYTHONIOENCODING = "utf-8"

# 全量重建（清库重嵌；中途勿强杀进程，否则索引变空）
$env:HF_HUB_OFFLINE = "1"; .venv\Scripts\python.exe index.py --full

# 增量重建
.venv\Scripts\python.exe index.py

# 调用 opencode（ps1 被执行策略拦截）
& "$env:APPDATA\npm\opencode.cmd" run "..."
```

- torch CUDA 版来源：`--index-url https://download.pytorch.org/whl/cu128`
- 若遇到 SAC 拦截 DLL（WinError 4551）：降级到一个发布更久的 cu 版本，
  或从 Windows 安全中心关闭 Smart App Control（enforce 状态不可逆，慎用）。
- LSP 对 venv 依赖的解析报错是误报，忽略。

---

### 问题 10：检索结果"截断当完整" + 六项检索体验缺口（2026-08-02 修复）

- **现象**：用户实测检索"免费 CERT"时，19 块的清单文件只返回 5 块截断切片，
  AI 却把它当完整答案汇报——skill 缺完整性守卫。审查（general 子代理逐行
  review）后确认方案并修复 6 项 + 2 个顺手项。
- **方案**：
  1. **完整性标记（retriever.py `_format_result`）**：来源行带 `[块 k/N]`
     （k 取 meta.chunk，N 从 BM25 缓存 `_bm25_files` Counter 派生——与检索
     内容同源同生命周期，reindex 后同步重建，无独立缓存不撒谎）；超
     CHUNK_LIMIT 的块尾部附 `… [本块已截断，完整内容见源文件]`。
  2. **同文件封顶**：正文模式每文件最多 3 块（`MAX_CHUNKS_PER_FILE`），
     按分数保留最高、边迭代边计数填满 top_k；list 模式不封顶（标记即
     完整性提示）。命中封顶输出汇总行"同一文件最多展示 3 块…"。
  3. **folder 边界**：`_in_folder()` 前缀+边界校验（`rel == folder` 或
     `startswith(folder + "/")`），杜绝 folder="AI" 误匹配 "AIML/"、
     "AI.md"、"AI Dev/"；`_norm_folder()` 统一 `\\`→`/`、去首尾空白斜杠；
     folder 支持父目录与单文件。
  4. **dense 候选池**：`dense_k = max(top_k*8, 200)` 无条件放大（原 50），
     修 folder 检索后过滤的小目录召回天花板（BM25 侧本就全库打分）。
  5. **性能**：融合阶段 `in`/`.index()` 的 O(M·N) 列表扫描 → dict 查找
     （`dense_map`/`bm25_map`），二次搜索实测 0.02s。
  6. **文案对齐（server.py）**：reindex_knowledge docstring 改为"一般无需
     手动调用，仅在自动同步失败或用户明确要求时"；search_knowledge
     docstring 注明首次调用/变更后首次调用耗时数十秒属正常 + folder 精确
     语义 + `[块 k/N]` 标记说明。
  7. **skill（obsidian-knowledge-search）**：Step 6 重复 → 7（修编号）；
     新增 **Completeness guard 硬规则**（k<N 且涉清单/数量 → 必读源文件；
     截断标记 → 必读源文件；同文件 ≥2 块 → 整读优先；快问快答可只引块但
     不得声称完整）；folder 语义修正；首次延迟提示；list 模式口径
     （不封顶 + 标记说明）；frontmatter 描述与正文一致化。
- **验证**：验收脚本 27/27 全绿（见 §7 验收标准）。附带清理：C 盘 0 字节
  告急（pip cache 4.8GB + 旧 bge-m3 快照 + Chroma ONNX 垃圾 79MB），清出
  7.1GB——ONNX MiniLM 是 Chroma 默认嵌入函数触发的下载，本项目用 BGE-M3
  根本不需要，已删。

---

## 7. 验收标准（问题 10 修复）

| # | 验收项 | 结果 |
|---|--------|------|
| 1 | 来源行带 `[块 k/N]`，N 与 index_meta.json 块数一致（19 块文件回 19） | ✔ |
| 2 | 正文模式同文件 ≤3 块 + 封顶汇总行；list 模式不封顶、无汇总行 | ✔ |
| 3 | 截断标记（临时 collection 构造 7000 字符块单测） | ✔ |
| 4 | folder 边界 9 组单测 + `_norm_folder` 2 组全过；"OTHER CERT/论点"/"ROCKETRY" 零越界 | ✔ |
| 5 | 泛化查询（GitHub Foundations 多少钱）、双语查询（HEBAT3 仿真配置）回归命中 | ✔ |
| 6 | 二次搜索 0.02s（<5s 门槛） | ✔ |
| 7 | skill 编号无重复 Step、含 Completeness guard 节、与代码语义一致 | ✔ |

---

## 6. 现状与下一步

**已验证**：
- MCP 工具被 opencode 实际调用并正确作答（带出处）
- 泛化查询（"免费入门证书有哪些" / "GitHub Foundations 认证多少钱"）命中目标笔记
- CUDA 加速生效，索引 1058 块 / 117 文件，无污染
- 指纹自动同步：改文件后首次搜索自动增量更新并提示；二次搜索幂等无提示
- 双语检索：中文查询命中全英文笔记（中文锚点增强，dense 0.555→0.641）
- 三个 Bug 修复 + 性能/死代码清理后回归：删文件清理、幽灵块清理、
  锚点只在首块、双语/泛化查询全绿（28.7s 全量重建 787 块）
- 两级边界切块（问题 8）：永不剪断句子，超长块降级段落/句子切，
  787 → 1058 块，>1500 块 36 → 5（纯表格）

**待办**：
- [x] 端到端再验一轮（opencode 实际问答）→ 已由问题 10 验收脚本覆盖
- [ ] 评估防重复 server 进程方案（避免多次测试叠加内存占用）
- [ ] （可选）若泛化查询仍偶发不中，调 BM25 加权 / RRF 融合 / folder 限定
- [ ] 给 `HEBAT3_ORK_Design_Parameters.md` 等其他全英文笔记补中文 `summary`，
      让中文锚点更贴合内容（增强效果已验证，但锚点来自元数据，元数据越准命中越准）

---

## 问题 11：现存不足崩溃 + 无法自动修复 + 无法自动切 CPU（2026-08-05 修复）

- **现象**：显存/内存不足时：`torch.cuda.OutOfMemoryError` 直接崩、MCP server 启动即死
  且无限重启循环、检索永久失败；`import torch` 偶发 `WinError 1455`（页面文件不足）。
- **根因（7 个）**：
  1. **P5（致命）** `index.py` 顶层 `from sentence_transformers import ...` →
     **任何 import index 的进程（含 MCP server 启动）都会加载 CUDA DLL** →
     内存不足时启动即死，进程层"无法自动修复"。
  2. **P1** `get_model()` 设备一次性探测（`cuda if available else cpu`），
     `_model` 缓存后永不更换 → 无任何切 CPU 机制。
  3. **P2** 索引/检索编码裸奔（`model.encode` 无 try/except、无 batch 控制），
     一次编码全部新块；且 CUDA OOM 后驱动上下文损坏，同模型对象继续每次崩溃。
  4. **P3** `kb_stale` 只校验 Vault 指纹，不校验 Chroma↔meta 一致性 →
     `--full` 中途被杀（清库窗口）后 0 块 + meta 完好 + 文件未变 → **永不自动重建**。
  5. **P7** 即使发现 0 块，增量路径指纹全命中 `new_ids` 为空 → 0 块保持 0 块。
  6. **P4** server 工具层无兜底，异常直接上抛。
  7. **P6** CPU 降级内存也不足：bge-m3 fp32 ≈2.3GB，需 fp16 减半。
- **方案（已实现）**：
  1. **延迟导入**：`sentence_transformers` 移入 `_build_model()` 内，import index/server 不碰 torch。
  2. **设备状态机**（`index.py`）：CUDA 初始化/编码失败 → 进入 **5 分钟冷却**（`_cuda_cooldown_until`，
     进程内，非硬性窗口）→ 冷却到期后每次调用先做**毫秒级显存探测**（`_cuda_probe`，分配 64MB
     tensor），通过即用 CUDA、失败再冷却；`fallback_to_cpu()` 同进程内卸载 CUDA 模型 + `empty_cache()`；
     `device_state.json` 仅作诊断落盘（失败原因/时间），不再做门禁。
     **显存恢复后自动切回**：CPU 模型缓存期间，每次请求探测通过即 `_try_switch_back_cuda()`
     （先释放 CPU 模型避免双模型内存峰值，失败回滚 CPU 并冷却）——无 24h 硬等待。
  3. **`encode_safe()` 统一入口**：捕获 OOM/页面文件/内存类错误（`_is_memory_error` 匹配
     "out of memory"/"paging file"/1455 等）→ 自动降级 CPU 重试一次；非内存错误不降级；
     CPU 也失败才抛（不可恢复）。索引（`index_vault`）与检索（`hybrid_search`）共用；
     encode 显式 `batch_size=32`（检索 1）。
  4. **CPU fp16**：`_build_model("cpu", fp16=True)` 减半内存（失败回退 fp32）。
  5. **一致性自愈**：`kb_stale` 增加 `_chroma_count()` 校验（meta 块数 vs Chroma 实际 count，
     不加载模型、毫秒级），不符 → stale → 自动重建；`index_vault` 检测
     "meta 有数据但 Chroma 空" → 清空 meta 强制全量重嵌（修 P7 的增量空转）。
  6. **server 兜底**：两个工具 try/except 返回明确提示文本（旧索引仍可用），不崩。
- **验证**（全部通过）：
  - 导入链：`import server` 后 `sys.modules` 无 torch/sentence_transformers。
  - 状态机 9 组单测：冷却期不探测/探测失败重冷却/到期探测通过即就绪/切回成功/切回失败
    回滚+冷却/缓存路径冷却期零探测/自动切回/encode OOM 降级。
  - **真实场景**：CUDA 真 OOM（978MiB 分配失败）→ 日志降级 → CPU fp16 加载 1s →
    中文查询命中正确（21.6s 首查，`[块 k/N]` 标记正确）→ 增量 reindex 成功（973 块，
    kb_stale 归零）；显存恢复后清冷却 → 探测通过 → **直接加载 CUDA 成功**（无需等窗口）。
  - 崩溃恢复：系统级崩溃后 MCP server 自动重启加载新代码（112MB 未预载模型 vs 旧版 782MB）。
- **环境提醒**：本机内存长期紧张（空闲常 <2GB）+ C 盘告急会诱发 WinError 1455；
  模型缓存需保持完整（HF 磁盘不足警告时勿删 `~/.cache/huggingface` 下 bge-m3）。

---

## 问题 12：RAG 数据跨机器分发（导出/导入工具 + Linux 支持）（2026-08-06 完成）

- **背景**：`data/` 被 .gitignore，GitHub 同步不携带索引；虚拟机（Ubuntu）上跑
  RAG 需要重新全量嵌入（28s+）且依赖网络与 GPU。需求：导出一个文件，接收端
  一条命令秒级恢复索引（不重算嵌入），兼作测试靶子数据（含完整性自检）。
- **交付**：
  - `export.py`：自动刷新（stale 才增量重建）→ chromadb API 读全库（不加载模型，
    持 index.lock）→ 打包 zip：`payload.jsonl.gz`（id/text/embedding/meta，JSON+gzip）
    + `vault/` 源文件 + `index_meta.json` + `manifest.json`（逐文件 sha256）
    + `AI_GUIDE.md`（注入本次导出信息）→ **导出自校验**（把自己当接收方解包比对）→
    `data/export/` 只留最近 3 个。输出固定 `data/export/obsidian-rag-export-*.zip`（9.4MB/973块）。
  - `import.py`：两阶段防半成品——先 CRC+sha256 全量校验（任一失败中止、目标零改动），
    通过后才：交互确认覆盖（`--yes` 跳过，AI 非交互必加）→ 备份 index_meta →
    持锁 delete+重建+分批 upsert（500/批，纯向量重插）→ count 校验 + 随机 3 块
    embedding 余弦一致性 → 原子写回 index_meta → `vault_export/` 落位 →
    包移入 `data/archive/` → 清临时区。重跑幂等。
  - `AI_GUIDE.md`：AI 自动执行引导（仓库模板 + 包内注入版），含 Ubuntu/Windows 双平台
    命令、验证点、失败分支决策表、只读靶子数据用法。
  - `requirements.txt`：chromadb 1.5.9 / mcp 2.0.0 / sentence-transformers 5.6.1 锁版本，
    torch 平台命令注释（Windows cu128 源 / Linux 默认源）。
  - `tests/verify_export_import.py`：端到端验证套件（39 项断言，标准库无 pytest）。
- **代码改动（最小面）**：
  1. `index.py` 跨平台文件锁：`import msvcrt`（Windows 专属，Linux 上 `import index` 直接崩）
     → `_IS_WINDOWS = os.name == "nt"` + `_lock_acquire/_lock_release` 函数内按平台局部导入
     （msvcrt 字节锁 / fcntl flock），`write_lock()` 接口不变。**Windows 分支逐字保留**。
  2. `index.py` VAULT 环境变量覆盖：`VAULT = os.environ.get("OBSIDIAN_VAULT", 原路径)`，
     本机行为不变；接收端指向 `vault_export/` 防"文件全删"误判清库。
- **过程中发现并修复的 bug**：
  1. numpy 数组 `or []` 触发 truth-value 歧义（export 读库、import 自检两处）。
  2. import 抽查取样逻辑写岔（会积累数百条）→ 引用池 + `random.Random(7).sample(3)`。
  3. 测试脚本对 gzip 二进制 count 行数 → 先 `gzip.decompress`。
  4. **精度陷阱（关键）**：旧 chroma 结构库 embedding 存 float64 原值；新库对 cosine
     空间做**归一化 + float32 存储**（cos=1 但值差 ≤1 ULP，2.98e-8）→ "逐位比对"必然
     失败。自检判据改为**余弦相似度 ≥ 1-1e-6**（检索排名的本质等价判据）。
  5. import 把传入包移入 archive（接收方语义正确）→ 测试脚本改用归档后的包继续后续组。
- **验证**：39/39 全过——回归（import 链/锁/指纹/幂等/检索）、导出 12 项、
  副本导入 9 项（count/meta/vault/归档/清理/中文文件名往返）、覆盖与交互取消、
  **损坏注入**（payload 篡改与包截断 → 中止且目标索引零改动）、留 3 个、**端到端
  检索对比：真库与副本 top-3 结果逐字符一致**。
- **遗留/风险**：
  - fcntl 锁分支无法本机（Windows）实测，交付 AI_GUIDE §5 Linux 验收清单
    （`import index` 无 msvcrt 报错 → 全流程导入 → 首查触发 bge-m3 下载属预期）。
  - 本机 CUDA 仍被 SAC 拦截 `_argkmin` DLL（sklearn 扩展）→ 实测走 CPU fp16 兜底，功能不受影响。
  - 导入库 embedding 存储为 float32+归一化，与原库 float64 检索结果等价（余弦判据保障）。

## 问题 13：残留实例持锁死等 + 无进度可见性（2026-08-07 修复）

- **背景**：某次会话结束后遗留两个 `server.py` 孤儿进程（17:16 启动），持续持有
  `index.lock` 的 msvcrt 字节锁；新会话实例（20:07）在 `write_lock()` 上无限阻塞，
  所有 MCP 调用 30s 超时、进程 CPU 归零看似死机 15+ 分钟。同时索引期间毫无
  进度可见性——MCP 调用超时后既不知道任务在跑、也不知道还要多久、无法判断卡死。
- **交付**：
  1. **写锁超时 + 持有者定位（index.py）**：`_lock_acquire` 改非阻塞轮询
     （`LK_NBLCK` / `LOCK_NB`，0.5s 间隔），`LOCK_TIMEOUT_SECONDS=60` 超时抛
     `LockBusyError`（含持有者 PID 与处置建议）。锁文件记录持有者 PID；
     超时后若持有者已死则清锁重试一次兜底。**不再无限死等**。
  2. **进度文件（index.py）**：`data/index_progress.json`（原子写）——阶段
     （scanning/embedding/writing/done/error）、文件 done/total、块 done/total、
     耗时、ETA（按块进度线性推算）、心跳 `updated_at`。约定：running 时心跳
     超 30s 视为疑似卡死。嵌入分批（`EMBED_BATCH_SIZE=64`）逐批更新心跳。
  3. **后台索引（server.py）**：`reindex_knowledge` 改后台线程立即返回；
     新增 `index_status` 工具输出进度文本（含 ETA 与卡死告警）；`ensure_fresh`
     检测到变更时后台更新、用旧索引先出结果（索引为空的首跑场景例外，同步等），
     杜绝"索引几分钟 + MCP 30s 超时"假死。
  4. **并发防护（index.py）**：`encode_safe` 加线程锁（模型不可重入：后台索引
     线程与检索并发编码会崩）；`server.py` 对残留 running 标志按心跳时效判定
     （超 2×30s 视为死进程残留，不挡新任务）。
- **过程中发现并修复的 bug**：
  1. `_lock_record_holder` 写入 PID 后文件指针未归位 → `msvcrt.locking` 按当前
     指针解锁 ≠ 锁定位置 → PermissionError 覆盖正常流程（解锁前强制 `f.seek(0)`）。
  2. `write_lock` 的 finally 无条件释放锁 → 超时未获锁时也调 UNLCK 抛
     PermissionError 掩盖 `LockBusyError`（改为仅 `acquired=True` 时释放）。
  3. 测试污染：临时 vault 测试写入了真实 Chroma/meta（40 条目）→ 用真实 vault
     增量重建自动清理（"清理 40 个失效块"，块数精确回到 1185）。
- **验证**：
  - 锁竞争：A 持锁 8s，B 超时 3s → 抛 `LockBusyError`（含持有者 PID 与建议），
    无 PermissionError 掩盖、无死等。
  - 进度：真实全量重建中实时轮询 `index_progress.json` 见 "嵌入 768/1185 块"，
    完成 phase=done（1185 块，149s）。
  - 端到端 MCP：stdio 会话 `tools/list` 三个工具齐；`index_status` 显示 done 详情；
    `reindex_knowledge` 秒回（后台启动 PID）；搜索在索引期间不阻塞、结果正确
    （新路径 10-Areas/... 命中）；最终 index_status 干净收束。
- **使用方式（AI/人）**：`reindex_knowledge` 或自动同步启动后，轮询 `index_status`
  看阶段/进度/ETA；心跳 >30s 且 running → 告警并建议查 PID 处置；遇到
  LockBusyError → 按提示结束残留进程重试。
- **补充（自适应卡死基线，2026-08-07）**：初版 `PROGRESS_STALE_SECONDS=30` 是固定
  阈值，慢机器（CPU 嵌入单批可达 1-2 分钟）会正常干活被误报卡死。改为自适应：
  每次 `update_progress` 观测"距上次心跳间隔"，取本次任务最大值 `max_gap_s`；
  卡死阈值 = `max(30s, max_gap_s × 2)`——快机器（CUDA 基线 ~8s）16s 无心跳即警，
  慢机器（基线 ~90s）放宽到 180s 不误报；首次模型加载期（无基线）附带"任务早期
  可能属正常"提示。`server.py` 的残留 running 判定同用自适应阈值。
  验证：6 场景矩阵（快/慢 × 正常/卡死 + 加载期）输出全部正确。
- **补充 2（定时心跳 + 双重判定，2026-08-07 v3）**：自适应基线本质是"事件驱动心跳
  （间隔=硬件性能）"的补偿，规则复杂。用户建议改为固定频率心跳后重构：
  - **独立心跳线程**每 `HEARTBEAT_INTERVAL=5s` 从内存状态强制写盘（刷新 updated_at，
    不动 last_advance_at）；事件更新（批次完成/阶段切换）仍立即写盘推进进度。
    心跳间隔与硬件无关——模型加载期、单批嵌入期间心跳照常走，心跳停止即真异常。
  - **双重判定**（固定阈值）：心跳停 >15s（3×5s）→ 疑似卡死；心跳正常但进度
    （块/文件数）>25s（5×5s）未推进 → 疑似批次内卡死（假活）。
  - `EMBED_BATCH_SIZE` 64→16（进度数字跳动密度 = 停滞检测粒度，吞吐代价可忽略）。
  - 移除 max_gap_s 自适应基线（语义被定时心跳取代）；`server.py` 残留判定回归
    固定 `HEARTBEAT_TIMEOUT`。
  - 验证：三场景全过——事件推进（心跳/ETA/推进时间显示正常）；26s 无推进精确
    触发停滞告警、恢复推进即消除；伪造 60s 前心跳触发卡死告警（含加载期提示）；
    真实运行（50 块/16.6s）模型加载期心跳每 5s 刷新可见。

---

## 问题 14：8GB 卡共享显存溢出 → 400 倍减速假死（2026-08-10 修复）

- **背景/现象**：全量索引后期，嵌入批次从秒级退化到约 15 分钟/40 块；GPU 100%
  占用但功耗仅 38W（正常编码 70-113W）；进程不报错、心跳正常推进，MCP 状态
  显示"疑似卡死"却永不结束。显存监控：nvidia-smi 7742/8151 MiB（94.9%），
  Task Manager 显示独占 6.8GB + **共享显存 5.4GB**（RTX 5060 Laptop，8GB）。
- **诊断过程**：
  1. 索引结束后 GPU 空闲仍占 7742 MiB（fp32 模型仅 2.3GB）→ 缓存分配器池
     只进不出（代码无 `empty_cache`），高水位残留常驻。
  2. 峰值分配 ~12.2GB > 物理 8.1GB → **Windows WDDM 显存溢出不报错，静默
     排入共享显存（系统内存）**：CUDA 层分配永远"成功"，`_is_memory_error`
     兜底永不触发 → 原有 CPU 降级机制对这类故障完全失效。
  3. 带宽崩塌是减速机理：GDDR7 ~375GB/s vs DDR5 系统内存 ~60GB/s 差约 6 倍，
     每批张量横跨两块内存 + 页迁移抖动 → 400 倍减速、功耗爬行（38W）。
  4. 心跳监测盲区："假活"检测只认"进度完全不推进"（>25s），识别不了
     "推得极慢但没停"的病态模式。
- **根因（3 个代码缺陷）**：
  1. CUDA 路径未开 fp16（CPU 路径反而开了）→ 权重 2.3GB + 激活值双份精度。
  2. 批间从不释放显存池 → 池子只涨不缩（空闲也占 7.7GB + 5.4GB 共享）。
  3. `embed_batch_size=16` 过大 → 单批激活峰值把总需求顶破 8.1GB 物理显存。
- **修复（index.py + config，全部本次落地）**：
  1. **CUDA 启用 fp16**（`_load_model`，与 CPU 统一：fp16 优先、失败回退 fp32）：
     权重 2.3→1.1GB、激活值减半。质量损失可忽略（检索指标差 <0.3%，
     CPU 路径此前已在用 fp16）。
  2. **`_release_cuda_cache()`**（新增 helper）：`encode_safe` 每次编码后
     `torch.cuda.empty_cache()`（finally 保证执行），嵌入循环结束再收一次——
     分配器高水位不再残留（空闲 7.7GB→~1.9GB）。
  3. **`embed_batch_size` 16→8**（`data/config.json` 运行时配置 +
     `config.py` DEFAULTS/模板 三处同步）。
  4. **慢批看门狗**（`encode_safe` CUDA 路径）：单批耗时 >30s → 告警计数；
     连续两批仍慢 → 主动 `fallback_to_cpu()`（复用既有降级机制）。WDDM 永不
     抛错，耗时是唯一信号——这是系统识别病态模式的唯一机制。
  5. **自动批次 `_auto_batch_size()`**（新增）：每次编码前按
     `torch.cuda.mem_get_info()` 实时可用显存收紧批次：
     `cap = (free − 3.0GB 固定开销) ÷ 0.4GB/块`，钳制 [2, 32]，配置值仅作
     上限；收紧时打日志。校准参数来自全量实测（fp16 模型+上下文+工作区
     ≈3GB；1500 字长块 ≈0.45GB/块取 0.4 留余量）。
- **决策记录**：
  - 自动批次上限**维持 32**：曾提议收紧到 12，评估后判定 8GB 卡由公式天然
    压在 7-8（free≈5.8GB → (5.8−3)/0.4≈7），32 上限仅在大显存卡上生效，
    无实际风险，维持 32 不改。
  - 修复顺序决策：先落地 3 项显存修复（fp16 / empty_cache / batch 8），
    看门狗与自动批次为增强层；三者可独立回滚。
- **验证**（真实 vault 全量重建，155 文件 / 1544 块，监控 1s 采样）：
  - 总耗时 37.9s（扫描 ~2s + 嵌入 ~32s + 写库 ~3s）；对比修复前"40 块
    ~15 分钟"（约 350 倍提速）。
  - 嵌入期 GPU 80-100% util、功耗 70-113W（满血运行）；峰值显存 5.5GB
    （纯物理，结构上不可能再溢出共享）；空闲回落 ~1.9GB。
  - 看门狗零告警、无 OOM、无降级；数据一致性 meta 155 文件/1544 块
    == Chroma count 1544。
  - 合成压测：1600 块 7.6s（209 块/s）；批间 empty_cache 开销可忽略。
  - 自动批次三场景：正常 free 5.8GB→批 7；模拟占用 4GB（free 3.8GB）→
    批 2 且编码正常；释放后自动回升到批 6-7。
- **操作记录**：
  - 旧 MCP server 进程（旧代码 + 7.7GB 常驻）被终止，opencode 需重启重连
    MCP 工具；新 server 加载 fp16 模型，空闲占用直接降至 ~1.7GB。
  - 期间 vault 大规模重组（152 文件迁移至 PARA 结构），增量索引实际重嵌
    1544 块（近似全量）；Home.md 原地修改。
  - 全量重建后 Chroma 块数 1352→1544（含用户新增 6 个文件，其中 3 个
    24-50KB 转录长文，贡献 ~173 块）。
- **已知遗留（观察项，暂不处理）**：`update_progress` 原子写（tmp +
  os.replace）与并发读进度文件的进程存在 Windows 写竞态，碰撞时打
  `WinError 5` 告警但被吞掉、不影响运行；频率低且无害，留观。

---

## 问题 15：wiki 引用标签污染检索 + 切块逻辑版本化（2026-08-10 修复）

- **现象/需求**：vault 63% 文件含 wiki 链接（150/239），纯引用 `[[机器]]` 的
  标签文本原样进入嵌入与 BM25 → 搜"机器"会命中仅引用它的文件（如"如何制作
  蛋糕.md"引用"机器.md"），而非内容文件；路径型链接
  `[[../../20-Projects/.../AGENTS|别名]]` 的相对路径垃圾（英文目录名）也进向量。
- **决策记录（用户拍板）**：
  1. 无别名纯引用 `[[目标]]` / `[[目标#标题]]` / `[[目标#^块]]` / `![[嵌入]]`
     → **彻底去除**（消除引用噪声）。
  2. 带别名 `[[目标|别名]]`（含表格转义 `\|`）→ **保留别名**（Obsidian 中
     别名 = 读者实际看到的文字，有信息量）。
  3. 统一规则，MOC 等链接密集文件不加特例（简单可预期）。
- **修复（index.py）**：
  1. **`clean_wikilinks()`**：正则 `!?\[\[([^\]]*)\]\]`；`\|` 先还原为分隔符
     再按 `|` 切分（处理表格转义）；别名 strip 后返回，否则返回空串。
     在 `extract_frontmatter` 之后、切块之前统一清洗——索引与 BM25 共用
     同一份清洗后文本。
  2. **`META_VERSION = 2` 版本化**：`save_meta` 写入 `_version` 字段；
     `index_vault` 加载时版本不匹配 → 强制全量重嵌。**动机**：文件指纹
     （mtime+size+md5）感知不到代码升级，此前若改切块逻辑，增量索引会
     静默沿用旧文本；版本化让未来切块迭代自动触发重建。
- **验证**：
  - `clean_wikilinks` 14 个模式单测全过（别名 / 表格转义管道 / #标题 /
    #^块 / !嵌入 / 相邻链接 / 空链接 / 无链接文本）。
  - 全量重建 39s（监控 1s 采样），**1536 块**（原 1544，清洗后 8 块跌破
    切块阈值，符合预期）；一致性 meta 155 文件 / 1536 块 == Chroma 1536；
    `_version: 2` 落盘。
  - 库内 **0/1536 块残留 `[[` 语法**；**0 个文件被清洗成空块**（链接密集
    的 MOC 文件仍有标题/描述内容）。
  - 别名保留抽查全 OK：Home.md "TODO 待办总台"/"MOC-Aerospace"、
    MOC-AI.md "暑期项目 · AI Knowledge System"。
  - **引用对行为**：MOC-AI.md 原含无别名 `[[MOC-Engineering]]` → 重建后
    该文本 0 命中，搜 "MOC-Engineering" top5 无 MOC-AI.md（痛点场景解决）；
    带别名引用（MOC-Ideas.md）按决策 2 保留显示文本，仍可命中（符合设计）。
  - 附带收益：检索结果展示文本不再含 `[[...]]` 语法垃圾。
- **监控发现（全程 1s 采样，均非 bug，记录在案）**：
  - 空闲显存 1.9→4.0GB：两个常驻 MCP server 均已懒加载 fp16 模型
    （各 ~2GB：权重 1.1 + 上下文）——第二次实例是中途某次搜索触发的，
    正常；自动批次自适应 cap 7→6，峰值 7076 MiB < 8151，无共享显存溢出。
  - `WinError 5` 竞态复现 3 次（问题 14 已知遗留；`_write_progress_file`
    已捕获并按设计记录，无影响，仍留观）。
  - **噪声修复**：自动批次原每批都刷"收紧为 N"日志（本次 ~100 行）→
    改为仅 cap 值变化时记录一次；`--full` 时版本升级日志不再误报
    （空 meta 无 `_version`，之前每次全量都打）。

---

## 问题 16：meta 顶层 `_version` 破坏遍历 → 自动同步失效（2026-08-10 修复）

- **现象**：搜索时每次返回"检测到 Vault 变化，但自动更新索引失败：
  'int' object has no attribute 'get'；以下为旧索引结果"。
- **根因**：问题 15 把 `_version: 2`（int）作为顶层字段写入 meta——
  原本 meta 的字典语义是"键=文件路径，值=条目 dict"，`_version` 破坏该
  语义。三处遍历全中雷（`meta.items()` / `meta.values()` 遇到 int 调 `.get` 崩溃）：
  1. `kb_stale` 一致性校验（`index.py:691`）：`sum(info.get("chunks") for info in meta.values())`
  2. `kb_stale` 已删文件统计（`set(meta) - seen`）：即使不崩，`_version` 键
     也会被算作"已删文件"→ `removed=1` → **永远 stale、每次搜索都触发重建**
  3. `index_vault` 空库自愈日志（832）与失效块清理 valid 生成（939）
- **为什么问题 15 验证时没爆**：验证走 `--full`（meta 先清空再重写）+ CLI
  脚本直连检索，不经过含 `_version` 的 meta 遍历路径；MCP server 当时仍是
  旧代码。本次 opencode 重启后 server 加载新代码，`ensure_fresh → kb_stale`
  首次触发即崩。
- **修复（index.py，最小面）**：三处遍历加 `isinstance(x, dict)` 守卫；
  removed 统计改为先过滤出 dict 条目（`meta_files`）再减 `seen`。
- **验证**：`kb_stale` 返回 `stale=False, stats 全 0`（不再误报）；
  832/939 已由同一守卫覆盖，`py_compile` 通过。
- **遗留（长期可选）**：守卫只治标。根治是把 meta 文件改为嵌套结构
  （`{"meta": {...}, "_version": 2}`），需迁移兼容旧 meta——当前守卫 + 版本化
  已足够，暂不做。

---

## 问题 17：桌面可视化控制台（GUI）（2026-08-11 完成）

- **目标**：独立桌面窗口（非浏览器）展示索引状态并支持手动触发，新手友好。
- **技术选型**（与用户三轮讨论敲定）：Flet 0.86（Python + Material 3，
  纯 Python 免 Node）；方案比对了 Qt（颜值上限低）/Electron（重且需 Node）
  /Flet（现代 + 全 Python）。
- **设计**：`docs/specs/2026-08-11-gui-design.md`（经 UI 设计子代理产稿 +
  用户确认，深色监控台 + Teal 主色）。
- **架构（零侵入）**：GUI 进程只读现有 progress/meta 文件 + spawn 索引
  子进程（`python index.py [--full]`），常驻不占显存；仅"搜索测试"临时
  加载模型（走 encode_safe 降级）。与 MCP server 并存靠现有 index.lock。
- **功能**：KPI 卡（文件/块/耗时/三态状态）、进度条+心跳灯（复用现有
  卡死/假活判定）、实时日志（ERROR/WARNING 着色）、增量/全量按钮
  （全量有确认框、默认焦点取消）、搜索测试（复用 hybrid_search）、
  设备条、深浅主题切换、打开文件夹。
- **交付**：`gui/`（app/theme/store/worker/widgets 5 模块）+ 测试
  （`tests/test_gui_store.py` 7 例 + `tests/smoke_gui.py` 无窗口冒烟）。
- **踩坑记录**（flet 0.86 API 变化大，均已在 smoke 测试覆盖防回归）：
  1. `ft.padding.symmetric` → `ft.Padding.symmetric`；
  2. `CrossAxisAlignment.BOTTOM` → `END`；
  3. `ft.alignment.center_left` → `ft.Alignment.CENTER_LEFT`；
  4. `ft.app()` 废弃 → `ft.run(main)`；
  5. `Page.close()` 不存在 → 对话框用 `open=False + update()`；
  6. 按钮 `style=None` 时不能赋子属性 → 构造时传 `ButtonStyle(text_style=...)`；
  7. **教训**：勿用 PowerShell `Get-Content/Set-Content` 批量改含中文的
     UTF-8 文件（ANSI 解码→乱码→写回=损坏且不可逆，三个文件重建）；
     中文文件修改一律用 UTF-8 感知的编辑工具。
- **手动待验证**（自动化难以覆盖）：搜索测试的真实点击流、确认框按钮、
  全量重建全流程、主题切换观感——由用户跑 `python gui/app.py` 体验。
- **v2 迭代（2026-08-11）**：用户反馈 v1 中排"高度塌陷"（进度卡+搜索卡
  无高度锚点被压成 0，心跳灯/搜索框/进度不可见）+ 审美不足 → 经 UI 子代理
  重新设计并实施：
  - **布局纪律**：全窗口仅日志区弹性，其余区块固定高度锚点——header 48 /
    KPI 92 / **中排 280**（进度卡+搜索卡 expand 横向均分）/ 设备条 40；
    冒烟测试新增 `mid_row.height == SIZE["mid_h"]` 断言防回归。
  - **视觉**：深空灰三层底（#0E1117/#161B23/#11151C）+ 薄荷青
    `#4BCEB8`（浅色主题同构换色）；三字体体系（YaHei UI/Bahnschrift 大
    数字/Consolas 等宽）。
  - **组件重写**（theme.py token 化 + widgets.py 全量重写）：KPI 卡带图标
    + accent 短横线；心跳胶囊移至 header 右上（运行呼吸/红卡死/橙假活/
    绿完成/弱空闲）；进度卡 4 阶段 stepper + 36px 百分比 + 双行
    计时/ETA + 块计数；搜索卡 3 层结构（输入/状态条/结果列表）。
  - **耗时卡语义修正**（用户确认）：运行中=实时「mm:ss」+ 阶段名；未索引
    =「—」；其余=上次完成耗时（≥60s「x分y秒」/<60s「x.xs」）+「上次完成
    HH:MM」。
  - **踩坑新增**：`ft.Colors.with_opacity(opacity, color)` 参数顺序是
    **透明度在前**（写反会 `'<=' not supported`，widgets 曾 21 处反序）；
    Windows 控制台 cp1252 打中文会崩 → smoke 测试入口强制
    `sys.stdout.reconfigure(encoding="utf-8")`。
  - **v2 验证**：9 单测（+format_elapsed/format_mmss）+ 无窗口冒烟（含锚点
    断言）+ 真窗 20s 零 traceback（测试后已关闭）。
- **遗留（Roadmap 候选）**：exe 打包（`flet pack`）、主题持久化
  （当前会话内切换，未写入 config.json）、显存占用实时展示（progress
  无该字段，需加后端字段）。

---

## 问题 18：检索质量大修——标题链入文本 + 表格/列表绑定 + 重排器 + 顺序 bug（2026-08-11 修复）

- **现象**：用户搜"个人能力""fluent配置"能定位到正确文件（如
  `FLUENT配置与求解设置.md`），但返回段落质量差、正文块错位；并质疑
  "湍流模型/网格策略与 fluent 配置语义相关，为何排不上去"。
- **诊断（数据实测）**：
  1. **标题不参与检索**（根因）：`split_by_headings` 把标题单独存 metadata，
     嵌入文本与 BM25 只吃正文。Obsidian 笔记"标题=主题浓缩"的强判别信号
     被系统性丢弃 → 词面盲区（"个人能力"21 块 / "fluent"8 块标题命中但
     正文不命中）；dense 侧 FLUENT 干货块（湍流模型/流体域，多为表格）
     相似度仅 0.39-0.56，**低于无关叙述块**（MOC-AI 0.60）。
  2. **表格转述实验无效**：把 `| a | b |` 表格转成 "a: x; b: y" 再嵌入，
     sim 几乎不变（0.41→0.40）——表格体不是病因，标题缺失才是。
  3. **加标题链立竿见影**：`FLUENT配置与求解设置 / 1. 湍流模型...` 拼入后
     三个干货块 sim 0.39-0.56 → **0.59-0.62**，反超所有无关块。
  4. **顺带挖出系统性展示 bug**：Chroma `get(ids=...)` 返回顺序**不保证**
     与传入 ids 一致，而 `_format_result` 用 `zip` 按返回顺序输出 →
     **算法排序正确但展示顺序被静默打乱**，此前所有验收的 pos 数据全被
     污染。重排器评估因此一度失真（分数错位）。
- **修复（index.py / retriever.py / config.py）**：
  1. **标题链拼入块文本**：`split_by_headings` 维护 H1>H2>H3 嵌套路径
     （如 `FLUENT 配置与求解设置 / 2.1 流体域`），每块文本 =
     标题链 + 正文（嵌入与 BM25 同受益）；metadata 存 `hp` 供输出剥离。
  2. **停用 `make_anchor` 中文锚点**（ADR-3）：使命被标题链完全覆盖
     （标题本就含中文词）；anchor 字段退役。
  3. **表格绑定上下文**：表格段落并入直接上文（引导句）+ 直接下文
     （解释/结论），含表格的块整体保留（宁大勿断），消除表格裁断与
     孤立 `---` 残块。
  4. **列表保护**：连续列表项跨空行合并（`任务清单.md` 从 9 个单行块 →
     2 块）；超长列表按**列表项边界**切，永不从项中间剪断
     （`is_list_block` / `split_list_block`）。
  5. **两阶段精排**：dense+BM25 融合 top `rerank_candidates`（默认 10）
     → bge-reranker-v2-m3 cross-encoder 对 (query, 块) 逐对精排 → top_k。
     懒加载、失败自动降级纯融合；`rerank_enabled` 可关。
  6. **`_format_result` 顺序修复**：按 cid 映射后**严格按 ranked 顺序**
     输出（修复 Chroma 乱序假象）。
  7. `META_VERSION` 2 → 3（标题链）→ 4（表格/列表），自动全量重嵌。
- **评估**（5 组查询 × 黄金文件：fluent配置/个人能力/y+控制/网格无关性/OfficeCLI）：
  - 顺序 bug 修复前：top1=2/5、top3=3/5（乱序假象）
  - 修复后纯融合：**top1=3/5、top3=5/5、top5=5/5**（y+控制 4→1、网格 4→2）
  - +重排器：与纯融合持平（融合已够好，重排器兜底模糊语义查询）
  - 表格裁断 0 对；列表 9 对拆散 → 0 真裁断；块数 1559 → 1462
- **验证**：smoke_gui / test_gui_store / test_config_editor（新增
  rerank 配置项 + bool 类型支持）/ verify_export_import 全过；真窗 12s 零
  traceback；GUI 设置页新增 rerank_model / rerank_candidates / rerank_enabled。
- **配置**：`data/config.json` 检索段新增三键（重排模型名、候选数、总开关）；
  重排器首次加载需下载模型 ~1.1GB（一次性）。



## 问题 18：多库支持（一个注册表管理多个 RAG 库，跨库检索）

- **需求**：原系统单 vault → 单索引；用户希望任意 md 文件夹可单独注册为 RAG 库，
  检索时可选单库 / 多库并查 / 全部（默认）/ 反选（全选排除），且每库独立配置。
  核心约束：AI agent 可无歧义调用（先枚举再选库，未知库名报错）；暂不做 GUI；
  PDF/docx 等格式只留扩展位（`extensions` 字段），嵌入模型保持全局（union 检索
  要求同一向量空间）。
- **方案**：单 Chroma 实例多 collection（每库一个 collection + 按库 BM25 缓存），
  否决"每库独立数据目录"（N 个 PersistentClient 开销翻倍、物理隔离本机无用）。
- **实现**：
  1. `library.py`（新）：`data/libraries.json` 注册表增删改查（`list/add/remove/config`）
     + 生效配置合并（null=继承 config.json 全局）+ `resolve_entries` 白名单减法选库
     （未知库名/空集报错并列出可用库）。
  2. `index.py`：`_index_core` 参数化（collection/指纹文件/排除/切块/扩展名按库），
     `index_vault` 保留为 legacy 入口；`index.py --library <名|all>`；进度含 `library` 字段。
  3. `retriever.py`：BM25 全局单例 → 按 collection 缓存字典；跨库重排池（各库融合
     top rerank_candidates 进全局池，cross-encoder 纯文本打分统一跨库分数）；
     重排不可用降级按库归一化合并；结果 `[来源] <库名>/<相对路径>`（同名文件不歧义）；
     同文件封顶键改 (库, rel)。
  4. `server.py`：新增 `list_libraries` 工具；`search_knowledge` 加 `libraries`/`exclude`
     参数（空=全部、"all"=全部、反选=exclude）；`reindex_knowledge(library="")`。
  5. `export.py`/`import.py`：`--library`（默认 all 逐库独立打包）；manifest 记录库名；
     导入目标库未注册可 `--create --path` 自动注册。
  6. 迁移：首次运行自动把旧 vault 合成首个库（collection 沿用 obsidian_kb，
     `index_meta.json` 改名随行，指纹保留**零重建**）。
  7. GUI 最小兼容：`_open_result` 剥库名前缀（多库 GUI 属后续迭代）。
- **踩坑**：① Chroma Collection 对象不可哈希，跨库分组取文档须用库名作键；
  ② `startswith` 不能收 list（effective_config 输出 list，须 tuple）；
  ③ collection 派生名原 strip 尾部 `_`，中英文差异的库名会撞 collection，改保留。
- **测试**：`tests/library_registry_test.py` 8 项（迁移/合并/CRUD/选库语义/空注册表）；
  双库端到端冒烟 11 项（全库/单库/多库/反选/未知名/空集/folder/置信度）全过；
  eval 回归 **top1=4/5、top3=5/5、top5=5/5**（基线 3/5·5/5·5/5，无回退）。
- **文档**：vault 根 AGENTS.md 检索章节 + Obsidian RAG 使用指南加多库选范围；
  AI_GUIDE.md 导入加 `--library`；config 模板注释改指 libraries.json。
- **审计修复**（general 子代理审查后）：`--create` 默认路径先 mkdir；损坏注册表备份
  `.bak` 防覆盖丢失；库路径消失跳过同步保留旧索引（不清库）；BM25 缓存带 count
  快照自愈（外部索引后自动重建）；zip slip 防护（拒绝绝对路径/`..` 条目）；
  选库去重；非法库名条目加载时过滤；collection 覆盖值校验；包名加 hash 防撞名；
  export 单库刷新失败不中断 all 模式；list_summary/_chroma_is_empty 改只读
  get_collection（不产生创建副作用）。单测 8 → 14 项。
- **重排器解包 bug（2026-08-12 定位）**：`rr_scores = reranker.predict([(query, doc_got[(n, c)]) for n, c, _ in pool])`
  中 `pool` 元素是 (name, collection, cid)，解包写成 `n, c, _` 导致 c=Collection 对象
  （不可哈希）→ 每次重排必抛"cannot use 'tuple' as a dict key"，except 静默降级归一化
  合并——**自多库重构起重排路径从未真正运行过**。修复：`for n, _, c in pool`。
  修复后 eval 回到 ADR-7 文档基线 top1=3/5、top3=5/5、top5=5/5（此前 4/5 为降级
  模式的偶然偏优）。防线：`retriever.rerank_failures` 计数，eval 回归检测到即警告。
- **返回截断行边界（表格不拦腰切）**：索引侧"宁大勿断"保留的超长表格块（实测
  OfficeCLI-SKILL.md 单块 4779 字符）返回时被 2000 字符硬切在表格行中间；新增
  `_truncate_at_line`：截断点附近 ±300 字符内找完整行边界收边，行/表格行永不被拦腰切。
- **单例守卫（2026-08-12）**：实测发现 opencode 启动 MCP 时可能连续拉起多个
  server 实例（启动后 1s 内双实例，双份模型常驻 ~4GB + 写锁竞争）。新增
  `singleton.py`：PID 文件 + 存活探测 + atexit 清理，后启动者立即退出。
  实证：opencode 在场实例存在时，新实例被正确拒绝退出；实例退出自动清理 PID
  文件（无残留）。单测 5 项；另发现 opencode 环境会向子进程注入 Ctrl+C
  （KeyboardInterrupt 出现在随机位置，Python 3.14），测试进程 SIG_IGN 免疫。

---

## 问题 19：GUI 多库化（v3）——库下拉 / KPI 双行 / 库管理对话框（2026-08-12 完成）

- **背景**：多库后端（问题 18）落地后 GUI 仍是单库 v2——`store.py` 用单库
  `load_meta()`/`kb_stale(VAULT)`（旧 `index_meta.json` 已改名，实际已失效），
  搜索不带 `libraries`、索引不带 `--library`、打开源文件只剥前缀
  （不同路径的库无法正确定位）。设计文档标注"多库 GUI 属后续迭代"，本次兑现。
- **设计决策（用户确认）**：库选择 = header 下拉（全部库 + 单库）；KPI =
  **双行**（大数字=全库汇总，副行=选中库明细）；库管理 = 独立对话框
  （列库/添加/移除/改配置/打开文件夹），注册/注销走 `library.py` 既有函数，
  与 CLI 行为完全一致。
- **实现**：
  1. `gui/store.py` 重写：按库统计 `meta_stats_for(cfg)`（读 `meta_path(name)`）、
     按库三态 `library_state(cfg)`（`kb_stale` 传库的 meta/collection/排除/扩展名）、
     聚合 `library_snapshot()`（任一 stale→stale；全 none→none；否则 ok）；
     旧 `meta_stats()/index_state()` 改为全部库汇总口径（兼容调用方）。
  2. `gui/worker.py`：`start(full, library="")` → 子进程 `index.py [--library 名]`，
     启动日志带库名。
  3. `gui/widgets.py` 新增 `LibraryPicker`（下拉，全部库+各库名，空注册表禁用）
     与 `LibraryManagerDialog`（库列表含块数/最近索引/覆盖项，添加库=路径+可选名、
     配置=7 个覆盖键留空恢复继承、移除=仅注销/注销并删数据双按钮、打开文件夹）；
     `ProgressCard` 计数行附当前索引库名；`StatusCard` 支持自定义副行；
     `DeviceBar` 附选中库。
  4. `gui/app.py`：header 放下拉 + 库管理按钮；KPI 双行（文件/块=选中库明细副行，
     状态卡=聚合大状态+选中库明细副行，耗时卡附库名）；搜索传 `libraries`；
     增量/全量按钮作用于选中库（全部库=后端默认全库）；全量确认框按目标库报块数；
     `_open_result` 多库感知：来源行拆 `<库名>/<rel>` → 查注册表 → 库路径是
     Obsidian vault（含 `.obsidian`）走 `obsidian://open?vault=<库文件夹名>`，
     任意 md 文件夹库走系统默认打开，旧格式回退主 vault；`_open_vault` 打开
     选中库文件夹。
- **踩坑**：
  1. flet 0.86 `Dropdown` 无 `on_change`（改用 `on_select`）、`TextButton` 无
     `icon_size`/`height`（删参数）。
  2. **双模块陷阱（关键）**：smoke 里 `from app import App` 与 `import gui.app`
     是两个模块对象（sys.path 同时含根目录和 gui/），`patch("gui.app.xxx")`
     对 `App` 方法内的名字不生效（方法引用的是顶层 `app` 模块的绑定）。
     修复：smoke 统一 `import gui.app as appmod; App = appmod.App`。
  3. 测试默认参数引用函数参数（`collection="kb_%s" % name`）→ NameError，改 None。
- **测试**：`test_gui_store.py` 27 项（新增 10 项多库：meta_stats_for 含非 dict
  键、library_state 三态、snapshot 聚合/单 stale/全 none/空注册表、is_library_dir、
  _split_lib_rel）；`smoke_gui.py` 新增 7/8、8/8 步（下拉默认值/库管理对话框构建/
  多库打开系统与 URI 两路/选库参数）；真实窗口 15s 零 traceback。
- **遗留（Roadmap 候选）**：库管理对话框的"添加库"目前为路径文本输入
  （无系统文件夹选择器）；exe 打包；主题持久化（沿用 v2 遗留）。

---

## 问题 19b：GUI 进程残留 + 单例失效（AI 关闭后窗口/CMD 不消失）（2026-08-12 修复）

- **现象**：用户手动点 X 关闭 GUI 正常；但 AI agent（opencode）启动 GUI 后
  "关闭"时，GUI 窗口和 CMD 控制台窗口仍残留，需手动清理。
- **根因**：
  1. **flet 0.86 桌面模式是双进程**：`python gui/app.py` 实际拉起父子两个
     python 进程（实测 32200 父 + 9212 子），`__main__` 在子进程执行。
     AI 只杀父进程 → 渲染子进程 + CMD 控制台残留。
  2. `os.kill(pid,0)` 对 pythonw 子进程探测**误判"已死"**（实测：进程活着
     但探测返回 False）→ 旧版单例守卫失效，重复启动出双实例（4 进程并存）。
  3. 用 `python.exe` 启动必带 CMD 黑窗（控制台程序）；`pythonw.exe` 无窗口。
- **修复**：
  1. **启动用 pythonw**：`pythonw gui/app.py`（无 CMD 黑窗，flet 桌面可跑，
     实测父子双进程同存）。
  2. **`gui/stop.py`（新）**：两层终止策略——①读 `data/gui.pid` 后
     `taskkill /T /F` 整树；②**兜底 WMI 扫描命令行含 `gui/app.py` 的
     全部 python* 进程**（覆盖 PID 文件缺失/误判场景），终止后复查无残留
     并清理 PID 文件。实测：双进程全部清空，PID 文件删除。
  3. **单例守卫重写（app.py）**：弃用 PID 探测，改**文件字节锁**
     （Windows msvcrt / Linux fcntl flock，与 index.py 的 write_lock 同款，
     锁 fd 全局持有防 GC，进程退出自动释放）→ 新实例拿不到锁立即退出。
     PID 文件降级为诊断/兜底用途。
  4. `gui/stop.py` 用法：`python gui/stop.py`（AI 可直接调）。
- **验证**（真实窗口）：
  - pythonw 启动 → 双进程 + PID 文件 → `stop.py` 终止 → 进程树全空、PID 文件
    已清理（实测 6264+23832、32200+9212、34904+36124 三组均全清）。
  - 单例：第一实例在跑时启动第二实例 → 第二实例 2 秒内自行退出，
    进程树保持单实例（2 个 pythonw）；stop.py 后干净。
  - 单测 27 项 + 冒烟 8 步全过。
- **AI 使用约定**：启动 `pythonw gui/app.py`（或 Start-Process 指 pythonw）；
  关闭 `python gui/stop.py`。不要 Stop-Process 单杀 PID。
- **19c 补充（2026-08-12，用户反馈"依旧有进程"）**：残留的其实不是 python，
  而是 **flet.exe**（Flutter 渲染窗口进程）——flet 桌面实际是**三层进程**
  （pythonw 主 → pythonw 子 → flet.exe 窗口）。之前 stop.py 只匹配
  python*/gui/app.py，flet.exe 命令行是 `flet.exe tcp://... <assets>`，父进程
  死亡后成孤儿残留，且 AI 测试期间用 Stop-Process 单杀 python 会制造它。
  **修复**：stop.py 的 WMI 匹配加入 `Name='flet.exe' 且命令行含项目根目录`
  （assets 参数带完整路径）；实测三层进程 22316→24300→20564 一次全清。
  AI_GUIDE.md 新增 §8 GUI 启动/关闭约定（pythonw 启、stop.py 关、禁单杀）。

---

## 问题 20：置信度虚高——第一名恒为 100%，无关内容也显示高置信度（2026-08-13 修复）

- **现象**：排除 Obsidian Vault 后用 agents/skills/test 库搜"FLUENT 配置"，全文毫不相关的块
  也显示 `[置信度 1.00]`（实测连 USER_GUIDE 的"配置与数据位置"都 0.72）；用户对检索可信度产生怀疑。
- **根因**：`retriever.py` 置信度是**相对归一化**——每库融合分除以库内最高分，跨库再除以全局
  最高分（`scores = {k: v / gmax}`）。第一名恒为 1.00，库内没有真相关内容时"矮子里拔将军"，
  弱匹配也被包装成确定命中。排序正确（相对最优），但标签误导。
- **修复**：改为**绝对融合分**：`(dense_weight·1/(1+d) + bm25_weight·b/(1+b)) / (dense_weight + bm25_weight)`。
  dense 距离与 BM25 原始分本就映射到 (0,1)，加权上限 = 权重和，除以权重和即得绝对 0-1 相似度，
  不再除以本轮最高分。检索排序仍按融合分取 top_k（排序保持相对最优，标签变诚实）。
- **验证**：正例（Vault 内搜 FLUENT 配置）召回真内容 `FLUENT配置与求解设置.md` 0.83~0.86、
  `任务流程.md` 0.81、`B3_CFD能力评估.md` 0.80；反例（无 FLUENT 库）最高 0.72 且内容低相关。
  正反例分得开，不再虚高。
- **遗留（认知约束）**：绝对分是"相似度"而非"语义正确性"。0.4~0.7 的块可能只是命中"配置"等
  高频道用词——需结合文件名/标题判断，不能只看数字。已写入 Vault 决策记录 ADR-10。

---

## 问题 21：检索质量全面升级——RRF 融合 / jieba 分词 / 小块索引 / HyDE / 批次机制（2026-08-13）

- **背景**：用户质疑检索质量不佳（相关度低但置信度高、泛化查询命中差），要求研究主流做法并全面改进。
  研究结论（Qdrant/Anthropic/Pinecone/LlamaIndex/arXiv 2024-2026）：加权原始分融合不可靠（量纲不同）、
  候选池应 50-100、块应 ~600 字符+small-to-big、中文 BM25 需 jieba、泛化查询需 HyDE、bge-m3 已落后。
- **改动（5 个提交）**：
  1. **RRF 融合**：dense+BM25 加权(0.6/0.4) → RRF(k=2) 排名融合。旧方案 BM25 无界、b/(1+b)≈1 恒主导，
     dense 语义被废（Qdrant 明确结论）。置信度改为双路排名一致度归一化。
  2. **候选池 10→50**：融合排序有误差，池太小好块进不了重排决赛。
  3. **jieba 分词**：中文 BM25 从纯 2-gram → jieba 词级+2-gram 双通道+停用词。实测"个人能力"→[个人,能力]。
  4. **块 1500→600 + small-to-big**：小块语义纯净；命中碎片块时回填父节全文（[父节全文] 标记）。
     META_VERSION 4→5 全量重建（1759 块）。索引时间 40s（bge-m3）。
  5. **HyDE**（默认关）：本地 LLM（LM Studio qwen2.5-3b）为低置信度查询生成假设文档再检索；LLM 不在线静默降级。
  6. **批次机制修复**（重点）：_auto_batch_size 原 (free-3)/0.4 保守压批次到 6；我初改 (free-1)/0.05 过松放批次到 32，
     导致 Qwen3-Embedding（decoder 架构，attention 显存随 batch×seq² 涨）长块 bs=32 达 7.7GB/8GB，
     WDDM 溢出排入系统 RAM → 页面文件被吃满 + "100% GPU 但 30W" 病态。最终上限固定 8、free<4.5GB 再降 4。
     另修复 index_library 未转发 incremental/full 的 bug（--full 此前从未真正全量重建过）。
- **验证**：12 组评估集（5 原有 + 7 泛化）。bge-m3 与 Qwen3 对比：两者 top1=6/12、top3=9/12 完全持平。
  重建耗时 bge-m3 38s/@1GB 显存 vs Qwen3 ~320s/@4.5-7.7GB → **保留 bge-m3**（重建快、显存安全，评估无差异）。
  失败查询 个人能力（抽象词面零重叠 + B3 笔记缺概括关键词）属笔记质量问题，HyDE 3b 模型生成不稳未能救回。
- **遗留**：bge-m3 → Qwen3 可切换（改 model_name + --full 重建），留了评估对比数据；HyDE 默认关（需 LM Studio）。

---

## 问题 22：深度审计 + 20 项修复——配置回退 / os.kill 杀进程 / 向量污染 / 死键死代码（2026-08-14）

- **背景**：用户要求（1）检查检索、切块、向量化，在约束内提高索引质量；（2）深入 audit 找潜在致命问题，
  先验证真实存在再给清单，不急于修。当前在 Linux 虚拟机（无 GPU、无依赖、9.5GB 磁盘），
  目标工况是 Windows+GPU，需评估验证方式以免搞坏虚拟机。
- **验证手段**：零依赖 stub 沙箱——仓库副本（去 .git/data）+ 注入 chromadb/mcp 最小 stub，
  真实执行仓库代码。切块/分词/BM25/融合/配置/锁/meta 全是纯逻辑，0MB 成本即可验证。
  不装 torch（527MB）、不下模型（bge-m3 2.3GB + reranker 1.1GB），装齐峰值逼近磁盘上限且本机无 GPU。
  Windows/CUDA/GUI 相关结论一律标注"无法在本机验证"。审计报告见 `docs/2026-08-14-index-quality-audit.md`。
- **致命问题（已全部修复，F 编号对应审计报告）**：
  1. **F2 v5 大改被静默回退（最严重）**：`DEFAULTS` 与 `CONFIG_TEMPLATE` 是两份互相矛盾的默认值
     （chunk 600 vs 1500、候选池 50 vs 10）。`load_config` 缺文件时写模板但**返回 DEFAULTS**，
     于是首跑 600/50、第二跑起 1500/10——问题 21 的收益从第二次启动就没了。
     且 `small_to_big` 不在模板里反而一直取 DEFAULTS 的 True，得到最坏组合：1500 大块 + 小块专用的父节回填。
     导出包不含 config.json，**每一份分发副本必然踩中**。
     修：模板值/注释对齐 DEFAULTS（保留全部手写注释）、`template_consistency_errors()` 断言出厂种子、
     `load_config` 对既有 config.json 幂等补写缺键（老机器永远拿不到新键的问题一并解决）。
  2. **F5 `os.kill(pid,0)` 在 Windows 上是终止进程，不是探测存活**：CPython 对非 CTRL_* 信号一律
     `OpenProcess`+`TerminateProcess`。三个调用点：单例守卫会杀掉正在服务的 server 然后自己也退出（一个不剩）；
     锁超时会在 Chroma 写一半时杀掉持锁进程；**GUI 主循环每 1 秒调 `index_busy()`**，会杀掉自己拉起的索引子进程。
     `gui/app.py:47` 早有注释"实测 os.kill 对 pythonw 误判已死导致重复实例"——双实例正是这个 bug 造成的，
     不是它在解决的问题（另一分支 OpenProcess 失败 → 误判已死 → 两个都跑）。
     修：Windows 改 `OpenProcess(SYNCHRONIZE)`+`WaitForSingleObject(0)` 只读探测，singleton 复用同一实现。
  3. **F1 jieba 不在 requirements.txt**：`tokenize` 内裸 import，新机器按 AI_GUIDE 部署后一检索就
     ImportError，被 server 宽 except 吞成"（检索失败：No module named 'jieba'）"，**检索 100% 不可用**。
     修：加 `jieba==0.42.1` + 缺失时降级纯 2-gram。
  4. **F8 代码围栏内的 # 污染向量**：不只是多切一节——伪标题会成为其后所有真实小节的父标题，
     而标题路径要拼进嵌入文本，等于把 Python 注释混进正文向量。修：跟踪 ```/~~~ 围栏状态。
  5. **F9 裸 `[[wikilink]]` 被整个删除**：`[[火箭发动机]]` → `见  一节`。Obsidian 里裸链接是主流写法，
     链接目标恰是最高信号的概念词，被同时从嵌入文本和 BM25 词表抹掉。修：保留目标词，去路径与 #锚点，仅 `![[]]` 删除。
  6. **F20 文件名/title/tags 从不进嵌入**：只写 metadata。与 F9 叠加后概念层信息基本没进索引。
     修：文件级锚点拼进待嵌入文本（逐段去重——短文档 heading 取 title、文件名常与 title 同名，
     不去重会产生重复串、扭曲 BM25），完整前缀存 metadata `ctx` 供检索侧剥离。
  7. **F6 置信度与排序不同源**：排序用重排分、置信度用 RRF 分。实测输出 0.21/0.25/0.30/0.38/1.00——
     声称降序却单调递增，默认配置下的常态，直接误导消费输出的 LLM。修：重排生效时用 sigmoid(重排 logit)。
  8. **F7 BM25 缓存永不失效**：指纹只比 `count()`，而"改一段文字"通常不改块数。GUI 把 index.py 当
     独立子进程拉起，server 里的 `reset_bm25_index()` 根本不会被调用 → 关键词侧一直用旧文本。
     修：指纹改 `(count, index_meta 的 mtime_ns)`。
  9. **F16 `--create` 导入后下一次检索清空索引**：import.py 主动建空目录，而 `kb_stale` 对"空目录"
     不带 missing 标志 → 自动同步判"文件全删" → 清空刚导入的数据。import.py:291 的警告正是这个场景，
     而 `--create` 结构性保证了它成立。修：返回 `emptied` 标志，优先于 version_upgrade（宁可不重建也不清空）。
  10. **F10/F11 死键与死代码**：`fusion_dense_weight`/`fusion_bm25_weight` 赋值后从不读取（AST 确认），
      却挂在 GUI"实时生效"分组下；`hybrid_search_hyde` 零调用方，问题 21 的 HyDE 是未接线的死代码。
      修：权重接进 RRF（DEFAULTS 改 1.0/1.0 保持等权，现有排序不变）；HyDE 由 server 接入，
      新增 `return_top_confidence` 让首轮直接带回置信度，去掉原来"开 HyDE = 2 倍检索开销"。
  11. **其余**：F3 转义引号打断注释剥离致整份配置静默回退（GUI 里 truncate_mark 打个双引号即触发）；
      F4 配置零类型校验（`rerank_enabled:"false"` 是真值，重排照开）；F12 模板与 GUI 各缺同样 5 个键；
      F13 `index_vault` 不转发 incremental/full（问题 21 修了孪生的 `index_library`，漏了这个）；
      F14 `kb_stale` 从不看 `_version`，版本号提升不主动触发重建；F15 等长中文库名派生同一 collection
      且以下划线结尾不合 Chroma 命名规则；F17 `split_sentences` 收不到每库 chunk_max；
      F18 "父节全文"缺命中块、顺序打乱（命中第3块输出 3,1,2）、每段带重复前缀；F19 多行 YAML tags 解析成空串。
- **验证**：新增 `tests/audit_regression_test.py` 19/19 通过；既有套件 `library_registry_test` 13/14
  （唯一失败 numpy 缺失，HEAD 上同样）、`server_singleton_test` 5/5、`test_config_editor` 全绿。
  另跑了完整 E2E（真实 _index_core + 假编码器 + stub Chroma）：切块、ctx 组装、small-to-big 回填全部正确。
- **过程中的额外发现**：
  1. **仓库自带的 `test_config_editor.test_groups_cover_all_defaults` 在 HEAD 上就是红的**，
     报的正是 F12 那 5 个键——这条回归测试早就存在、早就失败，只是没人跑（用 git archive HEAD 复现确认）。
  2. **F17 性质更正**：不是"长段落突破块上限"（单句超限"宁大勿断"是既定设计），
     而是每库 chunk_char_limit 覆盖传不到句子层，会回落到全局配置。
  3. E2E 抓到我自己在 F20 引入的 ctx 重复串 bug，已修并补测试。
- **遗留 / 上线前须知**：
  - **META_VERSION 5→6，必须一次全量重建**（切块规则与嵌入文本都变了）。修好的 F14 会让 ensure_fresh
    自动发现版本变化并触发；也可 `python index.py --library <名> --full`。
  - **重建前先确认 `data/config.json` 的 `chunk_char_limit` 实际值**。补写逻辑不改已存在的键值，
    若它现在是 1500 则重建出来仍是 v4 大块，600+small-to-big 的配套设计拿不到收益。
  - 若 config.json 里已有 `fusion_*_weight: 0.6/0.4`，它们现在是**真生效**的了（等价于给 dense 加权）。
    要保持与此前完全一致的排序需手动改成 1.0/1.0。
  - 中文库名的 collection 会改名（当前唯一注册库 `Obsidian Vault` → `kb_obsidian_vault` 不受影响）。
  - **未审计面**：`gui/widgets.py`(1562 行)、`gui/app.py` 其余部分、CUDA 降级状态机、Windows msvcrt 锁、
    心跳/进度写入原子性、export.py 完整数据完整性路径。
  - **无法在本机验证**：F5 三个 Windows 调用点（Linux 上 os.kill(pid,0) 是良性探测，本机测试全绿）、
    Chroma 1.5.9 Rust 后端是否拒绝退化 collection 名（校验规则在 segment.py，而 PersistentClient 已走 Rust 后端）。

### Windows 实机验证结果（2026-08-14，问题 22 补）

在 Windows 目标机（8GB RTX 5060、32GB RAM、Python 3.14）上按 AGENTS.md 完成全部验证，结论：**全部通过，已 fast-forward 合并到 main（bc34202）**。

- **阶段 1 纯逻辑测试**：audit_regression 19/19、library_registry 14/14、server_singleton 5/5、test_config_editor 0 failures、test_gui_store 0 failures、verify_export_import 39/39。
- **阶段 2 F5**：新写法 _pid_alive 实测 自身=True / 999999999=False / -1=False / 'x'=False；未做双 server 实例实测（GUI/服务当时未在跑）。
- **阶段 3 F2**：真实 config.json 为 chunk_char_limit=600、rerank_candidates=50、small_to_big=true、fusion_*_weight=0.6/0.4（手动改过，未踩首跑陷阱）；补写行为正常——只新增 hyde_enabled/hyde_llm_url/hyde_llm_model/hyde_min_confidence 四个键，既有值与注释未被改动。fusion 0.6/0.4 现已真生效（dense 偏重），已记录待用户决定是否改 1.0/1.0。
- **阶段 4 真实索引**：主库在验证时被 ensure_fresh 自动重建为 v6（1763 块）；test/agents/skills 三个库手动 --full 补建（163/42/137 块），四库全部 _version=6。重建耗时 22-24s/库。
- **评估**：v6 重建后 eval_retrieval 12 组 **top1=5/12、top3=10/12、top5=10/12**（基线 6/9/9）。top3/top5 各 +1；CFD 查询 top1 从 y+ 变为同相关的 ansys 引用集（重排器判断）。
- **可见效果逐条确认**：置信度严格降序（0.73/0.72/0.72，F6 生效）；来源行出现 [已回填父节全文]（F18 生效）；v6 文档带文件级锚点 ctx（F20 生效）；缺失 jieba 时降级日志路径已在代码确认。
- **遗留**：三小库重建完成；Vault 文档（主页/使用指南/架构/决策记录/Roadmap/操作手册）已同步置信度与 small-to-big 语义；retriever.py _format_results docstring 已修正。
