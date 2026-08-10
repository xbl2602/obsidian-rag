# 个人 AI 工程助手 — 任务记录

> 开发记录：Obsidian Vault + RAG + MCP + AI Agent 全链路搭建与调优
> 更新：2026-08-10

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

