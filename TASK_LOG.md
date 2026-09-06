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

## 问题 23：多格式文档支持 R1——DOCX + 文字层 PDF（2026-08-24）

**范围修订**：原方案 R1 含 MinerU 云端 OCR；用户决定扫描件 OCR 整体后移到末轮（TODO.md 已重排：R1=Word+文字层PDF，R2=GUI，R3(末)=OCR）。R1 零新增配置键、零子进程、零 GUI 改动。

### 实现
- **extractors.py（新）**：唯一入口 `extract_to_markdown(path) -> (md|None, reason)`（与 TODO 原拟的 `-> str|None` 不同：index 落终态需要 reason，故改返回二元组）。DOCX 按 body 子元素保序遍历（标题钳 ###、管道表转义、单元格换行压空格）；PDF 以「文字页占比 ≥0.5」判层，扫描件返回 `(None,"scanned")`。缓存键 `<字节md5>.v1`，原子写，写失败仅跳过缓存；None 不写缓存；启动清扫 >24h 孤儿 tmp。懒加载 import，绝不抛异常、绝不写源目录。TEXT/BINARY/SUPPORTED_EXTS 单一事实来源。
- **index.py**：META_VERSION 8→9；新增 `_load_text()`（原始字节 MD5 指纹——对合法 UTF-8 与旧内容指纹等值，既有条目免迁移；OSError→哨兵 `"unreadable"` 两轮判稳；后缀一律 lower()）、`_skipped()` 单点谓词、`_terminal_entry()` 统一终态（reason ∈ unreadable|extract-failed|empty|tbd|scanned）；主循环 None/unreadable 判定严格先于 TBD；kb_stale 二进制源只比字节哈希绝不提取；converting 进度相位 + progress_text 停滞豁免；`__main__` 逐库 try/except（LockBusy 除外）。
- **library.py**：set_config extensions 白名单校验引用 SUPPORTED_EXTS（小写归一+去重+保序）。**tools/check_notes.py**：跳过二进制源（修 UnicodeDecodeError 崩溃）。**verify_export_import.py**：REPO_FILES 补 extractors.py；vault_export 计数改 manifest 权威清单集合比对。

### 过程中抓到并修掉的三个真 bug（新测试逮住）
1. `_index_core` 正常成功路径漏 `current_rels.add(rel)` → 切块成功的文件被裁剪出 meta、块随即被当幽灵清掉（单点 add 移到 stat 之后统一覆盖所有存活路径）。
2. P7 自愈分支误伤全终态库（每轮把合法终态 meta 清空重落，永不收敛）。
3. **一致性死循环（P7 推广）**：Chroma 部分丢块时（实测：验证中途进程被杀 → WAL 段未持久化，HEBAT3_Technical_Report 的 104 块丢失），meta 期望≠实际每轮报 stale 但增量无块可补、永远修不回。现推广为通用校验：期望≠实际即自动转全量重建（全终态库 0==0 不误伤）。真库实测自愈：1594←1490，38.9s。
- 另收敛一项备案隐患：「既有 md 空正文守卫不落 meta → 每轮误计 added 每轮 stale」随 empty 终态机制一并解决（原列于「明确不做」，因与同一代码路径重合顺带完成）。

### 回归结果（Windows 实机，Py3.14）
test_extractors **16/16**（新）· audit_regression **19/19** · library_registry **14/14** · server_singleton **5/5** · test_config_editor 0 failures · test_gui_store 0 failures · verify_export_import **39/39**。
注：管道环境下跑测试需 `$env:PYTHONIOENCODING='utf-8'` 前缀（交互控制台不受影响）。

### 真实索引影响
v9 升级触发一次全量重建（legacy 入口实测 1594 块 / 80.5s GPU，批次自动收紧 32→8）。**运行中的 GUI/server 若加载的是旧代码需重启**，否则新旧逻辑会交替操作同一 Chroma/meta。

### 遗留
- 扫描件 pdf 在 vault 中存在：当前记 scanned 终态跳过，R3 接 MinerU 后凭 mtime 变化或手动 --full 转正（xsrc 自动重试机制属 R3）。
- 提取器依赖缺失时的优雅降级路径未做单测（ImportError 模拟成本高），靠懒加载+warn_once 兜底。
- 真实 vault 尚未开启 extensions（行为变更，待用户确认后执行 `library.py config "Obsidian Vault" --set extensions=md,pdf,docx`）。

## 问题 24：多格式文档支持 R2——GUI 适配（2026-08-24）

R1 提交（177ede6）后的 GUI 层配套，全部为展示/判定口径对齐，检索链路零改动。

### 改动
- **gui/store.py**：
  - `heartbeat_state` 对 converting 相位豁免停滞告警（与 index.progress_text 双看门狗口径一致：心跳停止仍判 dead，豁免不掩盖真死）；
  - 新增 `meta_issues_for(cfg)`（按 reason 统计 xfail 终态文件数，只读指纹文件）与 `ISSUE_TEXT`（五种 reason 的中文标签+处置指引）。
- **gui/widgets.py**：
  - ProgressCard 阶段条插入「转换」chip（scanning→**converting**→embedding），converting 计数行显示「文档转换 x/y · PDF/DOCX→Markdown」；
  - HeartbeatPill.set_state 增可选 note 参数（运行中文案覆盖，不改状态色/呼吸）；
  - 库配置对话框 extensions 字段 helper 补「支持 md/txt/pdf/docx；扫描件 PDF 暂不支持 OCR（索引时自动跳过）」。
- **gui/app.py**：
  - 刷新循环在 converting 相位给心跳胶囊传 note=「文档转换中（大文件耗时属预期）」，用户不再误读为卡死；
  - 状态卡副行追加提取跳过汇总（形如「⚠ 提取跳过 4 个文件：扫描件×3、不可读×1」），按当前选中库范围聚合。
- **tests/test_gui_store.py**：+3 用例（converting 停滞豁免且 dead 不被豁免掩盖 / meta_issues_for 按 reason 计数与缺文件容错 / ISSUE_TEXT 覆盖全部终态 reason），27→**30 用例全过**。

### 回归
六件套全绿：audit 19/19、library_registry 14/14、server_singleton 5/5、test_config_editor 0 failures、test_gui_store 0 failures（含新增 3 例）、verify_export_import 39/39。

### 备注
- GUI 视觉观感（chip 配色、文案长度）待用户下次开 GUI 人工确认；逻辑层已由单测锁定。
- 真实 vault extensions 启用仍待用户确认（同问题 23 遗留）。

## 问题 25：多格式「人机分权」——默认开启 + Agent 门禁（2026-08-24）

用户需求：多格式默认开启；GUI 可自选格式并持久化；Agent 可继续做文本类索引，但**未经用户批准的格式不得被 Agent 重建/增量纳入**（含检索触发的自动同步）。批准粒度经确认：一次批准长期有效，可随时撤销。

### 实现
- **library.py**：`DEFAULT_EXTENSIONS=["md","pdf","docx"]`（entry.extensions 为 null 时继承 → 现有库与新建库自动默认开启）；OVERRIDE_KEYS/LIST_KEYS 增加 **`agent_formats`**；set_config 校验（仅二进制格式、须已在当前 extensions 启用、允许空=全部收回）；effective_config 输出 `agent_formats = extensions ∩ 已批准`（extensions 收窄时授权自动失效）。
- **index.py**：kb_stale/_index_core 新增 `agent_allowed` 参数——未授权后缀的文件在循环最早期（stat 之前）**冻结**：零 I/O、不转换、不计变更、条目与块原样保留（绝不裁剪清理）；无条目则视同不存在。一致性自愈的期望块数天然包含冻结条目，无误伤。
- **server.py**：Agent 可达的三个入口全部走门禁——
  - `ensure_fresh()`（search 自动同步）：kb_stale/index_library 携带受限集合 `TEXT_EXTS ∪ agent_formats`；存在待批准文件时返回明确提示；
  - `reindex_knowledge(library, allow_new_formats=false)`：新参数。未授权格式列出数量并提示"先向用户确认"；`allow_new_formats=true` = 用户已同意，将新格式写入注册表 `agent_formats` **持久化**并纳入本次任务；
  - `_start_background_index/_run_index` 经 lib dict 的 `_agent_allowed` 键透传。
  - GUI/CLI（人类路径）不带门禁参数，行为不变。
- **gui/widgets.py**：库配置对话框 extensions 文本框升级为 **md/txt/pdf/docx 勾选块**；新增"AI Agent 权限"勾选行（pdf/docx，取消某格式时联动收回其授权）；保存写入两类设置（= 用户设置持久化）。

### 测试与回归
- test_extractors **17/17**（新增 test_agent_gate_freezes_unapproved_binaries：冻结不嵌入/条目保留/受限视角判稳/批准后补齐且不重嵌/新文件两视角行为）
- library_registry **15/15**（新增 agent_formats 校验与交集语义用例；旧断言 ["md"] 按新默认更新）
- 其余四件全绿：audit 19/19、server_singleton 5/5、config_editor 0 failures、gui_store 30 PASS、verify_export_import 39/39。

### 备注
- 默认开启对真实 vault 的实际生效点 = 下一次任何索引运行（md 部分指纹全命中，只新增 pdf/docx 的转换与嵌入）。
- Agent 门禁是"提示+冻结"而非硬拒绝：Agent 始终可以维护文本层；越权风险由冻结语义消除。
- MCP 工具签名向后兼容：allow_new_formats 不传 = false = 严格模式。

### 补充验证（同日，问题 26 前置）：格式撤销与文件增删改语义
新增两个集成用例锁死行为：
1. **取消勾选格式** → 下轮索引自动清除该格式的条目与全部块（回到无该类型版本）；重新勾选后凭提取缓存快速恢复、无需重新解析。
2. **物理删除二进制文件** → 连 Agent 受限视角也会正常裁剪清理（冻结只作用于仍在磁盘上的未授权文件，不给已删文件续命）。
顺手修掉一个被新用例逮住的既有死角：kb_stale 的判空基准含 `_version` 哨兵与非 dict 脏数据——「条目清空后的收敛态」会被 emptied 分支永远误报 stale。现以真实条目数为准；「meta 与文件双空」判稳。
回归：test_extractors 19/19，六件套全绿。期间真库再现 Chroma 分叉（2370 vs 1594，疑似并发写入者），一致性自愈自动全量重建修复并幂等收敛——自愈机制实战有效。

## 问题 27：提取试验台——GUI 单文件转译效果预览（2026-08-24）

用户需求：点按钮选文件上传，旁边返回提取结果，像 Google Translate 左右对照那样预览转译质量；但左右分栏空间利用率低。

### 设计取舍
输入是二进制文件，"左侧原文"没有可展示物——因此不做分栏，**整幅留给产出**：
- 顶部控制行：选择文件 + 开始提取 + 当前后端徽章（本地直提 / MinerU 云端·已配Key）
- 信息徽章行：路由（local / ocr:mineru-cloud）、耗时、字符数、缓存命中、失败原因+处置指引（复用 ISSUE_TEXT）
- 主体两个自绘页签：「渲染预览」（flet Markdown，GitHub 扩展集，表格/标题可读）与「Markdown 源码」（等宽只读框便于复制）
入口：库管理对话框工具栏「提取试验台」按钮。与索引用同一条管线（extract_preview → _extract_full），所见即所得；预览不落索引终态。

### 实现
- **extractors.py**：抽取 `_extract_full()` 返回 (md, reason, route, cached)；公开 API `extract_to_markdown` 保持二元组契约不变；新增 `extract_preview(path)` 输出过程信息 dict。
- **gui/widgets.py**：新增 `ExtractLabDialog`。flet 0.86 控件模型适配：FilePicker 为服务型控件且 `pick_files` 是 async 方法（async 事件处理器直接 await 结果，不再走 on_result 回调）；弃用签名大改的 ft.Tabs，改自绘页签按钮 + visible 切换（版本免疫）。提取在后台线程执行，UI 不冻结。
- 测试：test_extractors 增 `test_extract_preview_contract`（字段形态/pair 契约不回归/缓存命中可见），**23 用例全过**；gui_store 导入级验证组件可构建。六件套全绿。

### 体验修订（同日，用户实测反馈六项）
1. 防重入 + 明确动画：进行中按钮禁用并改文案「提取中…」，新增**不确定进度条**；
2. 动态提示：底部说明按当前生效后端实时生成（本地直提→「扫描件将被跳过」；云端→「可能数十秒」），不再静态误导；
3. 活动秒表（心跳）：进度条旁每 0.7s 刷新「⏱ Xs 运行中 · 超时预算 ~Ys」，死机与否一目了然；中断收尾语义成文——daemon 线程随 GUI 进程消亡、缓存原子写至多留孤儿 tmp（启动清扫回收）、预览不碰 meta/Chroma 无需回滚；
4. 后端可选：新增「跟随全局 / 本地直提 / MinerU 云端」下拉，**单次覆盖**仅影响本次预览（extract_preview(backend=…) 参数穿透），不污染全局配置；
5. 渲染净化：新增 sanitize_render_md——<b>/<i> 转 **/*，<u>/<span> 等裸 HTML 剥除（flet Markdown 不渲染裸 HTML 会原样显示）；源码页保持原样以源码为准；
6. 复用实例打开时重置为干净待命态（防上次中途关闭遗留禁用按钮）。
测试：test_extractors **25 用例全过**（+sanitize 净化、+backend 单次覆盖不污染全局）；gui_store 0 failures；audit 19/19。

## 问题 28：双链关系图——出链/入链查询（不影响检索排序）（2026-08-25）

用户想要类似 Obsidian 反向链接面板的能力：给定一篇笔记，查它链接到谁（出链）、谁链接到它（入链）。问题15（2026-08-10）已经把 `clean_wikilinks()` 定成"清洗 `[[wiki链接]]` 为纯阅读文字后再切块/嵌入"——链接目标词绝不能重新混进嵌入文本，那正是问题15要修的污染（例如"蛋糕的制作方法.md"提了一句 `[[如何制作奶油]]`，链接目标词留在嵌入文本里会导致搜"制作奶油"命中错的那篇）。这条清洗行为本轮完全不动。设计上把关系数据做成与检索完全旁路的第二条管道：两条管道共享同一段原始正文，一条不变（清洗→切块→嵌入排序），另一条纯粹旁路（抽取链接目标→存进 meta→按需反查），后者不进嵌入、不进 BM25、不影响任何排序。本轮只做后端 + MCP 工具，GUI 展示留待下一轮。

### 实现
- **index.py**：新增 `extract_wikilink_targets(text)`，与 `clean_wikilinks` 共用同一条 `[[...]]` 正则与解析规则，但取"目标"而非"显示文字"（`[[目标|别名]]` 取目标、`[[目标#标题]]` 去锚点取目标、`![[嵌入]]` 与 `[[#本文锚点]]` 不计入）。`_index_core` 的文本与二进制两条正文分支里，都在 `clean_wikilinks(body)` 清洗**之前**先算出 links，清洗动作本身一字未改；meta 成功条目新增 `links` 字段（去重排序后的目标名列表）。
- **回填机制**：新增 `_links_missing(entry)`——非终态条目缺 `links` 键即判定需要重跑，与既有的 `_backend_changed` 同属"惰性触发重试"：不强制 `--full`，下一轮增量索引里该文件自然穿透快速路径重新处理一次（因为快速路径不区分"只是缺个字段"与"内容变了"，穿透后走的是完整的重新分块+重新嵌入），之后即收敛。终态（xfail/tbd）条目天然没有 `links`、也不该有——`_skipped` 已排除它们，不会被这个机制误拉回正常处理分支。`_index_core` 两处快速路径（size+mtime 分支、hash 分支）与 `kb_stale` 对应两处同步加了 `not _links_missing(...)` 判断（AGENTS.md 架构红线 6 的教训：Agent 门禁那次两侧必须同步改，否则一侧收敛一侧不收敛，永远误报/漏报 stale）。
- **`resolve_note_relations(meta_file, target)`**：出链/入链查询，完全基于当前 meta 现算、不持久化 inlinks（入链是全局反向索引，维护缓存比现查更容易过期；库是个人笔记量级，现算成本可忽略）。target 支持库内相对路径或不含扩展名的标题（按文件 stem 匹配，同 Obsidian wikilink 引用写法）；标题重名时任取其一，不追求消歧（与 Obsidian 本身行为一致）；断链（目标文件不存在）静默不出现在出链里；自链不计入自己的出链/入链。
- **server.py**：新增 MCP 工具 `note_relations(path, library="")`，库选择语义对齐 `search_knowledge`（空 = 默认库，经 `resolve_entries` 解析；只能定位单库，不支持 "all"，因为一篇笔记只属于一个库；默认库解析出多个时报错提示显式指定 library）。
- 零新增配置键（无需开关，没有链接的库自然空转）；`META_VERSION`（仍 9）与 `extractors.EXTRACT_VERSION` 均未动——这次改动不影响切块/嵌入的文本内容，不在这两个版本号的语义范围内。

### 测试
- **audit_regression_test.py** 新增 `test_extract_wikilink_targets`（裸链接取目标、带别名取目标而非别名——与 `clean_wikilinks` 方向相反、路径+锚点剥离、嵌入不计入、纯锚点不计入、去重、表格转义管道），**21/21 通过**（+1）。
- **test_extractors.py** 新增 6 例（`_IsoEnv` 隔离 + 假编码器，不加载真模型/真 Chroma）：链接抽取基本用例；回填机制端到端（手工删 meta 条目的 `links` 键模拟"功能上线前的旧索引"→下轮自动补齐且其余字段不变、编码调用次数证明确实被重新处理而非跳过）；`kb_stale` 同步生效（缺 `links` 判 stale/changed，验证两侧机制真的同步而非只改了一边）；终态条目缺 `links` 不被强制重跑；`resolve_note_relations` 端到端（含自链排除、断链静默丢弃、查询不存在标题返回 `resolved=False`）；同名标题歧义不崩溃。**38/38 通过**（+6）。
- 七件套回归全绿：audit_regression **21/21**、test_extractors **38/38**、library_registry **15/15**、server_singleton **5/5**、test_config_editor 0 failures、test_gui_store 0 failures、verify_export_import **39/39**（真库导出/导入/检索演练，含一次真实 hybrid_search）。

### 遗留
- ~~GUI 展示（关联笔记入口，"这篇笔记的出链/入链"面板）留待下一轮~~ 已在问题29完成。
- 真实 vault 的现有 meta 条目普遍缺 `links` 字段：下一次任何增量索引运行（含 `search_knowledge` 触发的自动同步）会对当前已索引的每个非终态文件穿透一次快速路径、重新分块+重新嵌入以补齐该字段，效果上类似一次全库重跑，但只发生一次，之后恢复正常增量跳过。这是 `_links_missing` 机制的预期行为（用于在不动 `META_VERSION` 的前提下补齐存量数据），非 bug，但用户下次触发索引时应预期到这次性能开销。本轮跑七件套回归时（2026-08-25）该次性重建已实际触发（Chroma 2674 块 vs meta 1898 块 → 自动全量重建），验证了这条预期成立，且与本轮 GUI 改动无关。

---

## 问题 29：双链关系图——GUI 展示（关联笔记内联展开）（2026-08-25）

问题28完成了双链关系查询的后端与 MCP 工具，GUI 展示留到了这一轮。本轮把这条能力接进语义检索卡：检索到一条结果后，除了展开正文，还能再点一下同一行新增的"关联笔记"按钮，内联看到这篇笔记的出链（它链接到谁）与入链（谁链接到它），不用切到 Obsidian 里翻反向链接面板。纯展示层接线，`resolve_note_relations` 原样复用、一字未改。

### 实现
- **gui/store.py**：新增 `note_relations_for(cfg, target)`，模式照抄既有的 `meta_issues_for`——只读该库 meta 指纹文件，不加载模型、不碰 Chroma；任何异常（含 meta 缺失/损坏）一律折叠为安全默认值 `{"resolved": False, "file": None, "outlinks": [], "inlinks": []}`，不外泄异常。内部调用 `index.resolve_note_relations(meta_path(cfg["name"]), target)`。
- **gui/widgets.py**（`SearchCard`）：`__init__` 新增可选回调 `on_relations=None`。`show_results()` 的 `_render_state` 新增 `relations_shown`（当前展开"关联笔记"区的结果下标集合）与 `relations_cache`（下标→查询结果，避免同一条结果反复展开时重复调用回调）。每条结果标题行在"在 Obsidian 中打开"按钮旁新增一个图标按钮（`ft.Icons.HUB`，实测在项目当前 flet 0.86.5 环境下存在，无需换用候补图标），tooltip"查看关联笔记（双链）"，点击走独立的 `_toggle_relations(i, rel)`——与控制正文展开的 `_toggle`/`expanded` 完全独立的另一个开关，互不干扰：只看正文、只看关系、两者都看、两者都不看，四种组合都成立。展开态下追加渲染"出链（本文链接到）：…\n入链（谁链接到本文）：…"；`resolved=False`（笔记已改名/移动，meta 里查无）时给出"未找到该笔记的索引记录，可能已重命名或移动"的兜底提示，不留空白也不报错。未接 `on_relations`（默认 None）时按钮不挂点击事件（与 `open_btn` 无 `on_open` 时的处理方式一致）。
- **gui/app.py**：`SearchCard` 构造新增 `on_relations=self._note_relations`；新增 `App._note_relations(rel)`，复用 `_open_result` 已在用的 `_split_lib_rel`/`_lib_by_name` 把结果行的 `<库名>/<相对路径>` 前缀解析回库配置，再调 `note_relations_for`；库名未知（旧格式结果行、或库已被移除）时直接返回 `resolved=False`，不抛异常。
- 零新增配置键；未改动 `index.py`/`server.py`/`extractors.py`——后端与 MCP 工具在问题28已验证正确，本轮纯粹是给已有能力接一个 GUI 入口。

### 测试
- **test_gui_store.py** 新增 `test_note_relations_for`：照抄 `test_meta_issues_for_counts_xfail_by_reason` 的打桩模式（`patch.object(gstore, "meta_path", ...)` 指向临时 meta.json），验证出链/入链解析正确（含按文件 stem 匹配不含扩展名的标题查询）；meta 指纹文件不存在时返回 `resolved=False` 而不抛异常。
- 新增 3 例：本项目第一次直接单测 `SearchCard`，不搭真实 flet Page/窗口——`_render_results()` 本身不碰 page，测试绕开真实点击事件派发，直接调用 `_toggle_relations(i, rel)`：①首次展开触发一次查询、收起不重查、再展开命中缓存不重复查询，且验证关系展开不影响正文展开的 `expanded` 集合（两个开关互相独立）；②`resolved=False` 时结果卡片渲染兜底文案；③未接 `on_relations` 时直接调用 `_toggle_relations` 也不抛异常、不产生缓存条目。
- 七件套回归全绿：audit_regression **21/21**、library_registry **15/15**、server_singleton **5/5**、test_config_editor 0 failures、**test_gui_store 0 failures（43 例，+4）**、test_extractors **38/38**、verify_export_import **39/39**（真库导出/导入/检索演练）。

至此双链关系图功能全部完成（后端 + MCP 问题28、GUI 问题29）。

## 问题 30：MinerU 云端 API 路径 bug 修复 + 文字层 PDF 可选送 MinerU（`pdf_text_backend`）（2026-08-26）

用户今天配好真实 `mineru_api_key` 后做的首次真实冒烟测试意外发现一个既有 bug：`_mineru_cloud_extract` 里硬编码的两处接口路径是错的——提交用的 `{_MINERU_BASE}/file-protocol/batch`、轮询用的 `{_MINERU_BASE}/file-protocol/batch/{batch_id}`，实测均返回 HTTP 404（纯文本 `404 page not found`，路由层面不存在，不是鉴权/参数错误）。查官方文档（https://mineru.net/apiManage/docs）并实测校正，正确路径是提交 `POST {_MINERU_BASE}/file-urls/batch`、轮询 `GET {_MINERU_BASE}/extract-results/batch/{batch_id}`（请求/响应体字段名本身没错，只是 URL 路径错）。**后果：`pdf_scan_backend=mineru-cloud` 这个功能自问题26（R3a）上线以来，任何真实调用都会 404**，被异常折叠机制悄悄吞成 `extract-failed`/`scanned` 终态，表现为"静默跳过"而非崩溃或报错——不会引发用户警觉，但从未真正 OCR 成功过一次。既有 `test_extractors.py` 的 mock HTTP 用例全部显示通过，是因为 mock 只验证"代码怎么调用 requests"，从不检查 URL 字符串是否是服务器上真实存在的路径，这类 bug 结构性地不在其覆盖范围内。

顺带落地了 2026-08-25/26 讨论、记录在 TODO.md Backlog 里的一个架构问题：`pdf_scan_backend` 此前只在"扫描件"分支生效（本地对扫描件零处理能力，该开关实质是"要不要为唯一能用的路径 MinerU 付费"）；有文字层的正常 PDF 分支完全写死走本地 `pymupdf4llm`，没有任何开关——而这条分支恰恰存在真实的质量/成本权衡（MinerU 结构识别更准，用户可能想为质量付费）。本次新增独立开关 `pdf_text_backend`（local/mineru-cloud），语义与 `pdf_scan_backend` 不同、不复用同一个键；送云端时 `is_ocr=False`，不为已有文字重复付 OCR 的钱。

### 实现
- **extractors.py**：
  - `_mineru_cloud_extract(path, is_ocr)` 签名新增必填参数 `is_ocr`（不设默认值，两个调用点都必须显式传），修正两处 URL，docstring 同步更正契约描述并记录本次修复。
  - 新增 `get_text_backend()` / `TEXT_BACKENDS = ("local", "mineru-cloud")`，写法照抄 `get_scan_backend()` 的防御风格（非法值回退 local）。
  - `_extract_pdf` 重构：删掉函数顶部"提前 resolve backend"那行（旧代码在还不知道文件是否扫描件之前就把 `backend` 无条件解释成扫描件语义，会污染文字层分支）；改为两个分支各自独立 resolve 自己的配置键——扫描件分支 `scan_backend = backend or get_scan_backend()`（语义不变），文字层分支新增 `text_backend = backend or get_text_backend()`，`== "mineru-cloud"` 才送云端（`is_ocr=False`，路由标"mineru-text"），其余任何值（含扫描件分支专用的 "none"/"mineru-local"）一律安全落到本地直提，不报错不崩溃。
  - `_extract_full` 的缓存路由候选列表新增独立标签 `"mineru-text"`（不复用 `"ocr:mineru-cloud"`），换后端旧缓存天然失效。**在此基础上发现并修正一个必要的额外问题**：字面按方案给的 `routes` 4 元素恒定列表（`["ocr:mineru-cloud","ocr:mineru-local","mineru-text","local"]` 无条件全查）会让 `_cache_get` 的"任一路由命中即真"逻辑失效——该逻辑的正确性建立在"同一文件字节只会由一条路由成功产出"这个假设上，扫描件相关的两个 `ocr:*` 路由仍满足这个假设（文件是否扫描件由内容确定性判定，与配置无关），但 `local`/`mineru-text` 不再满足：同一份文字层 PDF 在不同 `pdf_text_backend` 下会产生两个都合法但内容不同的成功结果。若不修，切换 `pdf_text_backend` 后如果另一路由恰好已有历史缓存，会被假命中，切换永远不生效——这正好是任务给出的测试要求 4 明确要锁死的行为，字面实现和这条测试要求相互矛盾。修法：`_extract_full` 现在按当前 `backend` 覆盖/全局 `pdf_text_backend` 只计算并加入**唯一**一个文字层路由标签（`mineru-text` 或 `local`，二选一），扫描件的两个 `ocr:*` 路由不受影响、逻辑不变。
  - `_mineru_cloud_extract` 的"未配 Key"分支按 `is_ocr` 拆分处理：`is_ocr=True`（扫描件）保持原样返回 `(None, "scanned")`；`is_ocr=False`（文字层）改返回 `(None, "extract-failed")`——沿用 "scanned" 会让 GUI 提示"发现扫描件 PDF...或改用文字层版本"，而触发这个分支的文件本来就是文字层，这条建议对用户是自相矛盾的误导。
  - 模块顶部 docstring 补文字层 PDF 路由说明段。
- **config.py**：DEFAULTS 新增 `"pdf_text_backend": "local"`；CONFIG_TEMPLATE 对应位置加注释块（紧跟 `pdf_scan_backend` 之后、`mineru_api_key` 之前，共用同一账号/Key/超时预算），第八节标题从"扫描件 OCR"改为"PDF 提取后端"（现在管两类场景）；`template_consistency_errors()` 校验通过。
- **gui/config_editor.py**：GROUPS 里"扫描件 OCR"分组改名"PDF 提取后端"并加入 `pdf_text_backend` 字段。
- **gui/widgets.py**（`ExtractLabDialog` 试验台 + `SettingsDialog` 设置页）：
  - 试验台"跟随全局 / 本地直提 / MinerU 云端"下拉的值是 `"auto"/"none"/"mineru-cloud"`（穿透为 `backend=None/"none"/"mineru-cloud"`）。`_extract_pdf` 重构前，这个下拉对文字层文件是**完全的死选项**——`backend` 只在扫描件分支被读取，选"MinerU 云端 OCR"对着一份正常 PDF 点提取，界面不报错但也绝不会真的调云端，静默照常走本地直提。重构后自动生效，新增端到端测试 `test_extract_preview_backend_override_reaches_text_layer_branch` 验证。
  - **顺带发现并修复一个上传前置确认的漏问缺口**：试验台的"云端上传需先弹确认框"逻辑（`_run` 里 `if self._effective_backend() == "mineru-cloud"`）原本只读 `get_scan_backend()`；dropdown=auto 时，若用户只把 `pdf_text_backend`（而非 `pdf_scan_backend`）设为 mineru-cloud，这个判断会漏判——文字层文件会在用户没看到任何确认框的情况下被真实上传到第三方。修：新增 `_effective_text_backend()` + `_will_call_cloud()`（两个后端任一为 mineru-cloud 即需确认，宁可多问不能漏问），`_run()` 改用后者。确认框文案同步改为不预设"一定是 OCR"（文字层送云端时实际是 `is_ocr=False` 的结构识别，不是 OCR）。
  - 设置页 `SettingsDialog._save()` 存在同构的既有确认机制（`pdf_scan_backend` 切到 mineru-cloud 需先确认，因为库里存量扫描件会被批量外传），但只检查这一个键——新增的 `pdf_text_backend` 若不纳入同一机制，用户在设置页把它切到 mineru-cloud（今后每份文字层 PDF 都会被上传）会完全没有任何确认提示，与既有设计原则不一致。已将检查泛化为对两个键都生效（`_switches_to_cloud` 提取为独立方法，`_confirm_cloud_backend` 接收本次触发的键集合、按实际涉及的键列出对应文件类别）。
  - 试验台底部动态提示（`_hint_text`，即 TASK_LOG 问题26 记录的"本地直提→扫描件将被跳过；云端→可能数十秒"那段）原文只讲扫描件场景，对文字层文件场景完全沉默（不是说错，是没提，但现在这个下拉对文字层文件也真正生效了，沉默会让用户读不到任何与自己文件相关的信息）。最小化调整：在原有文案后追加一句读 `get_text_backend()` 的独立分句，说明文字层 PDF 在当前后端下的实际处理方式，不改动原有那两句的措辞。
- **is_ocr 参数改动波及的既有测试**：4 处 `ex._mineru_cloud_extract = lambda p: (...)` monkeypatch 补上 `**_kw` 容错（否则新的关键字参数 `is_ocr=` 会让这些桩函数抛 TypeError）；`_FakeRequests` 的 `get()` 轮询 URL 匹配、`post()` 请求体记录同步改为新路径/可断言的 json 载荷。

### 测试
- **URL 回归**：`test_mineru_cloud_extract_uses_correct_api_urls_both_is_ocr_values`（直接断言 mock 记录到的 POST/GET URL 字符串本身，覆盖 is_ocr=True/False 两个调用点，而非只看"提取成功"这种弱结论）；`test_mineru_cloud_happy_path_and_cache_route` 同步补强 URL 断言。
- **pdf_text_backend 默认值零行为回归**：`test_pdf_text_backend_default_local_unchanged`（不设置/显式设为 local 时路由仍是 "local"，且用调用计数断言绝不触达 `_mineru_cloud_extract`）。
- **pdf_text_backend=mineru-cloud 开启行为**：`test_pdf_text_backend_mineru_cloud_routes_with_is_ocr_false`（断言 is_ocr=False 参数值、路由 "mineru-text"、缓存命中/未命中）。
- **缓存路由隔离**：`test_pdf_text_backend_cache_route_isolation`（同文件先 local 后切 mineru-cloud 不假命中旧缓存，两条缓存独立共存，切回 local 仍命中原缓存）——这条测试就是抓住上面那个"字面 routes 列表与测试要求矛盾"问题的用例。
- **is_ocr 两个调用点**：直接调用层面 `test_mineru_cloud_extract_uses_correct_api_urls_both_is_ocr_values` 覆盖两者；路由层面扫描件走 `test_mineru_cloud_happy_path_and_cache_route`（True）、文字层走 `test_pdf_text_backend_mineru_cloud_routes_with_is_ocr_false`（False）分别覆盖。
- **未配 Key 时文字层分支的 reason 值**：`test_mineru_no_key_text_branch_folds_to_extract_failed_not_scanned`。
- **试验台端到端**：`test_extract_preview_backend_override_reaches_text_layer_branch`（文字层文件 + backend="mineru-cloud" 真调云端且 is_ocr=False，backend=None 时真走本地，两条路径内容互斥验证未被串用）；`test_extract_lab_auto_backend_confirms_when_only_text_backend_is_cloud` + 对照组 `test_extract_lab_auto_backend_no_confirm_when_both_local`（消费上面发现的漏问缺口修复）；`test_settings_text_backend_switch_requires_confirm`（设置页仅切 `pdf_text_backend` 同样需确认）。
- 六件套 + 本轮涉及套件全绿：`test_extractors` **44/44**、`test_gui_store` **0 failures**（含全部新增用例）、`test_config_editor` **0 failures**、`audit_regression` **21/21**、`library_registry` **15/15**、`server_singleton` **5/5**、`config.template_consistency_errors()` 通过、`verify_export_import` **39/39**（真库导出/导入/检索演练）。

### 遗留
- 本项目实际生产库目前**没有**真正开着 `pdf_scan_backend=mineru-cloud` 跑过（已向用户确认），因此这次不存在需要手动挽救的存量数据。但如果以后出现类似情况——曾经开着某个 MinerU 相关后端真实跑过、产生了失败终态——这些条目会卡在旧 `xsrc` 签名下不会被 `current_backend_sig()` 的自动重试机制捡回来（签名字符串本身没变，变的只是代码内部行为/URL），需要用户手动 `--full` 才会重新受益于本次修复。
- 已用本地直提成功索引过的文字层 PDF，用户开启 `pdf_text_backend=mineru-cloud` 后**不会自动重新处理**（这类文件不是终态失败条目，不在 `current_backend_sig`/自动重试机制的适用范围内），需要用户 `--full` 重建才会用新后端重新提取——与 `chunk_char_limit` 等配置类改动的一贯做法一致，本次未新增"检测到配置变化就自动重建"的机制。
- 线B（图片语义描述）、线C（MinerU 结果 LLM 后处理去噪）本轮未动代码，讨论/决策/开放问题原样保留在 TODO.md，留待以后单独开轮。

## 问题 31：GUI 设置页重构——分类导航 + 常用/开发者分层 + 枚举可视化选择（2026-08-26）

用户反馈设置页三个可用性问题：①44 个字段平铺在一个长列表里滚动，"文本混在一起难以定位"；②常用设置和开发者参数没有区分，锁轮询间隔这类几乎永不动的东西和知识库路径并列；③封闭枚举/布尔/模型名全靠手输文本——用户面对 `pdf_scan_backend` 不知道合法值是 `mineru-cloud` 这种魔法字符串，面对 `model_name` 不知道有什么模型可选、`true/false` 也要手打。纯 GUI 层重构，不触碰索引/检索管线，META_VERSION / EXTRACT_VERSION 均不变。

### 实现
- **gui/config_editor.py**：GROUPS 从"组名→字段列表"的二元组列表升级为带元数据的 dict 列表——每组含 `title`/`level`（basic=常用 / advanced=开发者，basic 组整体排在前面）/`icon`/`desc` 一句话说明；分组从 10 个按使用频率重组为 11 个（知识库、模型、PDF 与云端 OCR、检索输出为常用组；融合与排序调优、切块粒度、排除规则、HyDE、性能与硬件、锁与心跳、导出导入为开发者组）。新增 FIELD_META 每键元数据：中文 `label`（吸收原 widgets._cli_name 的映射并补齐此前裸奔的 hyde_*/pdf_*/rerank_* 等 16 键）、`hint` 一句话说明（CONFIG_TEMPLATE 注释的浓缩版）、`rebuild` 标记（结构类配置，GUI 据此打 ⟳ 提醒）、`choices` 封闭枚举（pdf_scan_backend: none/mineru-cloud；pdf_text_backend: local/mineru-cloud）、`suggest` 推荐候选芯片（model_name 四个嵌入模型、rerank_model 三个重排模型，开放值仍可手输不设限）、`secret`（mineru_api_key 渲染成密码框可反显）。写回层（load_raw/save_value/_replace_value/apply_updates/_value_to_json/kind_of/missing_keys/ALL_KEYS）全部原样保留，云端上传确认的数据契约不受影响。
- **gui/widgets.py** `SettingsDialog` 重构为主从布局：左侧 196px 导航列分「常用」「开发者」两小节（含 rebuild 组的 ⟳ 角标），右侧单组详情面板（组图标+说明+该组字段），一次只看一组，彻底消灭长滚动。控件按元数据分流渲染：bool→Switch（不再手打 true/false）；有 choices→Dropdown（选项即合法值，不再猜字符串；若 config.json 被手改成枚举外的值会保真显示为"（当前配置值）"，保存不会静默改写）；有 suggest→输入框+推荐模型芯片（点击回填，仍可自由输入任意 HF 模型标识）；secret→密码框。每个字段的 hint 直接展示在控件下方（⟳ 开头的说明 = 改后需全量重建），标题栏常驻图例。对话框尺寸 680×540→800×560。删除已无引用的 `_cli_name`/`_kind_hint`。
- **打开即刷新**：新增 `_refresh_values()`，每次 open() 从最新 CFG 回填全部控件值——修复既有缺陷（对话框对象随 App 常驻，外部手改 config.json 后再开设置页看到的还是构造时的旧值）。
- 对外契约保持：`_fields[key]=(输入控件, kind)` 结构、`_save`→`_switches_to_cloud`→`_confirm_cloud_backend` 云端二次确认链路原样，tests/test_gui_store.py 三个设置页用例不改一字通过；smoke_gui 的 `_fields`/`_dlg` 断言同样兼容。

### 后记（同日）：「知识库」组语义澄清
用户指出多库架构下设置页却只见"一个库"，误导源头是重构时沿袭的组描述"数据源根目录"。事实：真正的库列表在 `data/libraries.json` 注册表（GUI 工具栏「📚 库管理」管理，每库可覆盖 extensions/exclude/collection），config.json 的 `vault`/`collection_name` 是单库时代遗留的全局默认——现仅剩三个作用：`_migrate_legacy()` 首库自动迁移源（迁移完成后改它对已注册库零影响）、GUI 打开非注册库结果时的兜底路径（app.py `VAULT_DIR`）、接收端 `OBSIDIAN_VAULT` 场景。已把组名改为「知识库（全局默认）」、desc 明确指向库管理、两键 label/hint 同步改写；不隐藏这两键是因为测试契约要求设置页覆盖全部 DEFAULTS 键（test_groups_cover_all_defaults），且旧单库路径（`index_vault(VAULT)`）仍是受支持用法。

追加（用户追问模型与排除语义后）：①澄清推荐芯片≠本机已装清单——本机 RAG 管线实际只有 bge-m3 + bge-reranker-v2-m3 两个模型，芯片是 HuggingFace 推荐候选、点选后首次使用才下载，现芯片行上方有说明文案、当前在用的候选标「✓ 使用中」并高亮描边；②重排/HyDE 组 desc 与 hint 改为白话两步走解释（融合粗筛→cross-encoder 精排；HyDE=先让本地 LLM 写假设答案再检索）；③排除规则与切块粒度组 desc 及字段 hint 明确"全局默认值、可在库管理→库配置按库覆盖"，其中 tbd_exclude_ratio 标注仅全局生效（不在 library.OVERRIDE_KEYS 内）。

### 测试
- test_config_editor.py 新增 4 条静态契约（10 用例全绿）：`test_groups_structure_valid`（dict 结构必备键、level 只取两值、常用组整体在前、键不跨组重复）、`test_field_meta_complete`（每个暴露键必须有非空中文 label 与 hint，FIELD_META 无幽灵键——新键漏写 meta 在测试期就红）、`test_choice_fields_match_defaults`（枚举 choices 必须包含 DEFAULTS 默认值与 mineru-cloud 选项，防止下拉默认值错位导致保存静默改写）、`test_structural_keys_marked_rebuild`（9 个结构类配置必须标 rebuild=True）。
- 六件套全绿：config_editor 0 failures、smoke_gui 全过（44 字段构建）、audit_regression **21/21**、test_gui_store **0 failures**（46 用例，含三个设置页云端确认用例零改动通过）、library_registry **15/15**、server_singleton **5/5**、test_extractors **44/44**、verify_export_import **39/39**。

### 遗留
- SettingsDialog.apply(colors) 仍只存色不重绘（主题切换后已打开的设置对话框沿用旧配色）——重构前即如此，本轮未扩大范围。
- default_libraries 等列表类字段仍是逗号分隔文本输入（写回层 `_value_to_json` 已能拆分），后续可考虑做成勾选块，但库集合是动态注册的，需要先解决"对话框打开时拉取注册表"的依赖方向，本轮不做。

## 问题 32：索引进度看板误报修复——停滞宽限机制 `stall_grace_until`（2026-08-26）

council 两轮评审（`.council-state/round-plan-3/4/`）确认的四个「正常运行被误报心跳停滞」根因：**R1** update_progress 合并语义导致宽限/状态字段跨事件、跨任务残留；**R2** 宽限若用"后写者胜"合并会被更短的值反向缩短；**R3** converting 相位置位后有三个未还原出口（提取失败/空 body/切块），豁免窗口泄漏；**R4** write_lock 排队上限 60s 远超 STALL_TIMEOUT 25s，等锁必误报，且无变更路径直达时 phase 还停在 scanning。机制经方案门两轮评审定稿（maker-v2 + 复审六条强制/建议项）：给进度报告引入**自过期、默认自清**的宽限字段——写入方传相对秒数 `stall_grace_s`（永不落盘），落盘键为绝对截止时间戳 `stall_grace_until`；停滞看门狗在宽限内不判 stalled，心跳停止（DEAD）判定永远优先于一切豁免。

### 实现
- **index.py**：
  - 新增硬编码常量（不进 config，防"永久静默开关"；注释含升级矩阵与阈值漂移声明：lock_timeout>180 或冷加载>300 时对应窗口回退现状误报，非恶化）：`STALL_GRACE_MAX_S=600 / STALL_GRACE_MODEL_LOAD=300 / STALL_GRACE_WRITE=180`。
  - update_progress 改造：pop 掉 kwarg `stall_grace_s` → 默认移除既有 `stall_grace_until`（一次性豁免语义：任何普通进度事件终止宽限）→ 正数值才按 `max(旧值, now+min(grace_s, MAX))` 重写（防回退攻击）→ 非数值/≤0 不写（fail-closed）；docstring 注明语义。心跳 `_heartbeat_tick` 整表拷贝原样保留该字段（宽限在静默窗口内存活靠它）。progress_start 锁下独立 pop 显式清残留（与默认 pop 双保险；不持锁调 update_progress 防不可重入死锁）。
  - 新增 `_stall_grace(seconds, **fields)` 两段式守卫助手（复刻 _report_device 先例）：锁内取内存快照判 running+pid==本进程，锁外才调更新——守卫挡住"无任务写噪音"与"接手强杀残留文件复活死任务"（后者会拖垮 server._index_running 放行逻辑）。判定侧新增 `_stall_grace_left(p, now)` 唯一入口（running + isinstance 数值 + now<截止，非法值视为无宽限）。
  - 四个埋点：①get_model 缓存未命中实际加载分支内 cuda/cpu 两路各一处（显式 MODEL_LOAD 常量；禁止放函数入口——否则每批刷新等于永久静音看门狗）；②_try_switch_back_cuda 入口（盖住 fp16→fp32 双次串行加载与失败回滚恢复全程）；③fallback_to_cpu 收尾标记（其后的静默重载发生在调用方 _encode 内，由埋点①cpu max 合并续写）；④write_lock 前 `phase="waiting-lock"` + 宽限 → 拿锁后 `phase="writing"` 续宽限（waiting-lock 为新增 phase 值，全部消费方核对安全降级：widgets stepper idx=-1 兜底、PHASE_COLOR.get 默认值、progress_ratio 走 else、server 不消费 phase）。
  - G7 单点还原：extract_to_markdown 返回处立即 `update_progress(phase="scanning")`，一处覆盖三个出口——converting 豁免窗口严格闭合于转换真实耗时（600s 级 MinerU 云端 OCR 也完全在窗内，MinerU 不加埋点）。
  - progress_text 分支重排（优先级即判定顺序，与 GUI 镜像勿重排）：DEAD 原文不动 → converting 白名单原文不动（无条件生效不依赖字段，旧读新兼容）→ 宽限内信息行（含 PID[缺失容错为 ?]+已安静秒数+剩余秒数，无告警字样）→ stalled 告警原文 → 正常行。
- **gui/store.py**：heartbeat_state 插宽限分支（DEAD 先于一切 → converting 白名单保留 → in_grace 改判 RUNNING 色/呼吸不变 → stalled），判定表达式与 index._stall_grace_left 逐字镜像、互指注释；新增纯函数 `heartbeat_note(progress)`：DEAD-first 短路（红 DEAD 胶囊绝不配"宽限内"文案）、converting 文案保留、宽限内输出"模型加载/写库中（已安静 Ns，宽限内）"、否则 None。
- **gui/app.py**：内联三元换 heartbeat_note 调用；KPI 行阶段名经 PHASE_TEXT 中文映射（waiting-lock 不裸显英文内部值）。
- **gui/widgets.py**：仅 PHASE_TEXT/PHASE_COLOR 各加一行 `"waiting-lock": "等锁"/"accent"` 映射（不进 stepper）。
- **gui/config_editor.py**：stall_timeout hint 追加"；特定阶段（转换/模型加载/写库）有内置宽限"。
- server.py / extractors.py / config.py 零改动；META_VERSION=9 / EXTRACT_VERSION=2 未动（不影响切块与提取内容）。

### 测试
- **audit_regression_test.py** +16 例（21→37，风格对齐既有 PASS/FAIL + `_ProgressIso` 隔离：清内存表 + PROGRESS_FILE/DATA_DIR/DEVICE_STATE_FILE 重定向临时目录，save/restore 全局）：写侧 G1①普通更新清除、G1②跨任务残留（progress_start 双保险+结构断言 pop 在锁外）、G2 max 合并+clamp、G3 守卫四分支（空内存不写/异 pid 字节不变/同 pid≈now+s/残留文件原样）、助手不可重入死锁（行为探针 acquire(False)+AST 结构断言 With 块内无调用）、类型防御+clamp+判侧 fail-closed、kwarg 永不落盘；判定侧 C2 核心 test_progress_text_grace_states 三断言（宽限信息行含 PID+安静秒数无告警字样/过期恢复告警/心跳冻结仍 DEAD）+ PID None 容错 + G8 converting 无字段白名单边界 + 心跳 tick 保留宽限字段；C3 六条 CUDA 用例（fake torch 注入 sys.modules，index 全懒加载已验证）：②切回入口结构（先于 old=_model 与 _load_model("cuda")，注释含回滚覆盖声明）、③降级收尾标记存在性与尾部顺序+_encode 调用方重载、①双分支结构（缓存命中分支之后、各自先于加载、恰两处）、冷却期内重复降级/切回序列 clamp≤600 且 max 不回退（慢批降级链：③→get_model 重写→②回滚全序列）、无运行任务全部埋点零写入、④waiting-lock→writing spy 序列（真 Chroma 仅落临时目录+假编码器，断言紧邻顺序+两相位带宽限+done 终态无宽限）。
- **test_extractors.py** +1（44→45）：test_converting_phase_restored_after_extract——spy 快照序列断言 converting 后紧跟 scanning、后续无 converting 残留 + 结构断言还原语句位于提取调用与第一个出口分支之间（单点物理覆盖三出口）。
- **test_gui_store.py** +4（46→50 用例 0 failures）：C2 的 test_heartbeat_grace_running_then_expired_then_dead、非法类型 fail-closed、heartbeat_note 六态（含 DEAD-first 短路与已安静秒数）、双看门狗一致性（六个样本两侧结论逐一对照，压住镜像表达式漂移）。
- 六件套全绿：audit_regression **37/37**、library_registry **15/15**、server_singleton **5/5**、test_config_editor **0 failures**、test_gui_store **0 failures（50 例）**、test_extractors **45/45**、verify_export_import **39/39**（真库导出/导入/检索演练）。

### 备注
- 升级过渡期矩阵（方案 §7.1）：旧代码读新文件 = 现状行为（缺字段走原逻辑，converting 白名单无条件兜底）；新代码读旧文件 = 宽限缺失照旧告警；任意方向混跑不劣于引入前。阈值漂移同理：用户调大 lock_timeout 超 180 或冷加载实际超 300 时对应窗口回退现状误报，非恶化。
- 备案（reliability N2）：同进程并发污染（server 后台索引中检索线程触发设备切换写宽限进索引记录）——有界、fail-open、下次进度事件即清除，不改。
- 已知残余：embedding 单批 >25s 仍会暴露（现状如此，批次间有进度事件，属真实病态应暴露）；MinerU 600s 级安静期靠 converting 白名单而非宽限覆盖（§7.2 覆盖链，锁定用例成对）。

### diff 门后记（同日）
council diff 门 6 委员评审：security/product/redteam/performance 四席 PASS；architect 与 reliability 独立报告同一 blocker——_try_switch_back_cuda 的 del old 位于 try 块内且先于 log()/_report_device()，这两句抛异常（stderr 管道断裂等）时 except 回滚分支引用已删除的名字 → UnboundLocalError 掩盖原始异常、回滚未完成，与红线 1"收尾代码自己抛异常击穿容错承诺"同构。修复采用强化变体：引用释放改 old = None 并移至 try 块末尾全部可抛调用之后（若仅原位替换 del→None，_report_device 抛异常时回滚会把 _model 恢复成 None 丢掉 CPU 模型）。按纪律先写复现用例验证 RED 再修绿：test_switchback_rollback_survives_report_device_crash（monkeypatch _report_device 抛哨兵，断言不外泄 + index._model is cpu_model 身份比对恢复 + 冷却重武装 + 原始异常折叠进诊断）。checker 复核 9/9 PASS（audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store 0 failures、extractors 45/45、verify_export_import 39/39），LSP possibly-unbound 报警消除。

## 问题 33：MinerU 云端请求补 `model_version` 参数 + `pdf_text_backend` 新增 `mineru-local` 占位入口（2026-09-02）

用户与 Claude 在另一条调研会话里通读 MinerU 官方 API 文档后发现：`_mineru_cloud_extract` 的提交请求体从未包含 `model_version` 字段——不是"选择了较弱的 pipeline 模式"，是压根没做选择，服务端按未声明时的默认版本处理（官方文档建议显式传 `vlm` 以获得更高精度，尤其是密集公式、复杂版面场景）。这个遗漏不会以任何错误形式暴露：请求正常返回 200，产出正常写入缓存，只是解析精度低于本可获得的水平——与问题30那次的 404 路径错误不同，问题30会让功能整体失效且容易被察觉，这次是"能用但一直没用最好的模式"，更隐蔽。

顺带处理了另一件事：`pdf_text_backend` 目前只有 `local`/`mineru-cloud` 两个值，本地部署模型（无论是 MinerU 本地 vlm/pipeline 模式、还是未来可能接入的其他本地工具）在"文字层 PDF"这条路径上完全没有入口占位——`pdf_scan_backend`（扫描件分支）已经有 `mineru-local` 这个占位值（问题26起，TODO.md backlog 记录尚未实测联调），但文字层分支没有对应物。而用户的核心场景（工程课件）大多数是有文字层的 PDF，不是扫描件，这条路径反而更常用。

### 实现
- **config.py**：DEFAULTS 新增 `"mineru_model_version": "vlm"`（pipeline | vlm，非法值回退 vlm）；CONFIG_TEMPLATE 对应位置加注释块（紧跟 `pdf_text_backend` 之后、`mineru_api_key` 之前）；`pdf_text_backend` 的注释追加 `mineru-local` 说明；`template_consistency_errors()` 校验通过。
- **extractors.py**：
  - `EXTRACT_VERSION` 2→3：请求体新增字段属于"产出内容会变化"的改动，必须让旧缓存（用未指定版本时的服务端默认产出）整体失效重提，不能只改代码不动版本号——否则用户切换后感知不到任何变化（问题9节前调研反复强调的这一点，这次真正落地）。
  - 新增 `get_model_version()`（照抄 `get_scan_backend`/`get_text_backend` 的防御风格：懒加载 config、非法值回退）。
  - `_mineru_cloud_extract` 提交请求体加 `"model_version": get_model_version()`，两个调用点（扫描件 `is_ocr=True`、文字层 `is_ocr=False`）都自动生效，不需要分别处理。
  - `TEXT_BACKENDS` 新增 `"mineru-local"`；`_extract_pdf` 文字层分支新增该值的处理：**安全退化为本地直提**（走既有 `pymupdf4llm.to_markdown` 路径），只打印一次警告，不返回 `None`。这里的语义特意与扫描件分支的 `mineru-local` 处理（直接跳过不产出）区分开写进了注释——扫描件分支本地零处理能力，跳过是唯一选项；文字层 PDF 本地 pymupdf4llm 本来就能产出内容，"选了本地模型入口但没实现"退化成"不产出"是倒退，所以退化目标是本地直提。缓存路由标签仍记 `"local"`（`_extract_full` 的路由计算逻辑天然如此，因为产出内容确实等价，未改动那段代码）。
  - `get_text_backend()` docstring 补充 `mineru-local` 占位说明。
- **gui/config_editor.py**：`pdf_text_backend` 的 `choices` 加入 `("mineru-local", "本地部署模型（占位，尚未实现，自动退化为本地直提）")`；新增 `mineru_model_version` 的 FIELD_META（label/choices/hint）并加入"PDF 与云端 OCR"组的 fields 列表（否则 `test_groups_cover_all_defaults`/`test_field_meta_complete` 必红——新键漏写元数据在测试期就会暴露，这是问题31留下的静态契约机制生效的一个例子）。
- **gui/widgets.py**：`ExtractLabDialog._hint_text()` 的 `text_tail` 字典补 `"mineru-local"` 分支说明文案（此前若 `_effective_text_backend()` 返回这个值，`.get(..., "")` 会静默落空字符串，用户选中这个选项预览文字层文件时看不到任何相关说明——这正是该方法自己在注释里警告过的那类问题，之前只是没预料到会新增这个枚举值）。试验台下拉本身（`_dd_backend` 三档：跟随全局/本地直提/MinerU云端）不新增选项——它是"单次体验效果差异"用的简化下拉，不是 `SCAN_BACKENDS ∪ TEXT_BACKENDS` 的穷举展示，一个已知会退化的占位选项放进去意义不大，与既有设计定位一致，不改。

### 测试（tests/test_extractors.py，新增 7 例）
- `test_get_model_version_default_and_invalid_fallback`：默认 vlm；显式设 pipeline 生效；大小写不敏感；非法值回退 vlm。
- `test_mineru_cloud_extract_sends_model_version_both_is_ocr_values`：请求体必须携带 `model_version` 字段且跟随配置变化——`is_ocr` 两个取值 × `model_version` 两个取值共 4 种组合都断言请求体实际字段值（不是只断言"提取成功"这种弱结论，问题30已经用同样的教训写过一次）。
- `test_mineru_cloud_extract_default_model_version_is_vlm_when_unset`：config 里完全不设这个键时（例如旧 config.json 未升级），请求体仍必须落到 `vlm`，不能悄悄退回"没有这个字段"的旧行为——这正是本次要修的缺陷本身，必须专门锁死。
- `test_pdf_text_backend_mineru_local_degrades_to_local_no_network`：`mineru-local` 退化为本地直提、产出内容与 `local` 路径逐字节一致、route 落在 `local`、缓存正确写入命中、且用调用计数断言零网络调用（用 monkeypatch 计数替代 `_mineru_cloud_extract`，不依赖真实网络/Key）。
- `test_get_text_backend_accepts_mineru_local`：`get_text_backend()` 认可这个新值为合法（不被非法值防御误伤回退成 `local`——那样配置页选中它、读回时会"看起来什么都没选"，与 GUI 层 `test_choice_fields_match_defaults` 的假设脱节）。
- 六件套完整回归本次未能在开发环境跑通（本机 `.venv` 依赖 chromadb/torch，本次改动过程中用的是另一个受限沙箱，只装了 pymupdf/pymupdf4llm 单独验证了 extractors.py 层面的 5 个新用例，全部真实通过，非仅语法检查）；`config.py`/`gui/config_editor.py` 相关的静态一致性校验（`template_consistency_errors()`、`test_groups_cover_all_defaults`/`test_field_meta_complete`/`test_choice_fields_match_defaults` 的逻辑本体）已在沙箱里手工复现验证通过。**用户本机跑一次 `.venv\Scripts\python tests\test_extractors.py` 和 `.venv\Scripts\python tests\test_config_editor.py` 走完整六件套仍是必要的收尾动作**，本次改动未做过。

### 遗留
- 与问题30相同的性质：本项目实际生产库目前没有真正开着 `pdf_text_backend=mineru-cloud` 或已生效的旧 `model_version` 缺省调用长期跑过（云端调用此前完全没做过验证性实测，属于第一次真正配置齐全后使用），因此不存在需要手动挽救的存量数据；但已用本地直提成功索引过的文字层 PDF，本次 `EXTRACT_VERSION` 递增会让它们的本地直提缓存同样失效重提——这是预期行为（版本号是全局的，不区分"这次改动其实只影响云端分支"），下一轮索引会重新提取但产出内容不变（本地直提逻辑本身未改），只是多一次无意义的重复计算，暂不优化。
- `mineru-local` 目前仍是纯占位——扫描件分支自问题26起就有这个值但从未实测联调（TODO.md backlog 未完成），本次只是把同样的占位机制补齐到文字层分支，让两个分支的配置结构一致、为将来真正接入本地模型（MinerU 本地部署或其他工具）铺好统一入口，没有新增任何本地推理能力，也没有安装任何模型。
- 并行/批量加速改造（滑动窗口限流、有界并发提取、错误分类重试等）本次未动——`index.py` 主循环是高度状态化的单线程扫描/切块/落盘流程，共享大量可变状态（`meta`/`current_rels`/`new_ids` 等），贸然并发化风险远高于本次两处改动，且是架构级决策，按 AGENTS.md"拿不准的设计决策：停下问用户，不要自行扩大范围"，留给用户确认具体方案后再单独开一轮实施，不在本次一并做。

## 问题 34：混合型 PDF 整本按扫描件路由（2026-09-03）

### 背景
用户在另一条调研会话（留档：桌面《obsidian-rag-pdf-研究纪要.md》）里用两份真实工程
课件实测出 `_extract_pdf` 的架构性缺陷：**整份一刀切判定**——`文字层页占比 < 0.5 →
整本按扫描件`，对「PPT 原生文字页 + 教材扫描图」混装的课件两个方向同时翻车：

- **ManometerEquation.pdf**（文字页 3/9 = 0.33）：整本判扫描件，未开云端时零内容入库，
  连 3 页真文字也被丢弃；
- **Note9.pdf**（文字页 8/13 = 0.615）：整本判文字层 PDF 走 pymupdf4llm 直提，第 9-13 页
  **全部例题**（纯图片页）被**静默丢弃**——提取"成功"、无任何异常信号，错误内容直接
  入库。这比提取失败更危险：agent 按导航打开的是一本缺了全部例题的残本。

更糟的是判定极不稳定：某页文字层恰好 9 个字符（卡在 `_TEXT_PAGE_MIN_CHARS=10` 阈值下
方一个字符）就能让占比跨过 0.5 线，整本走向随机翻转。

### 决策
用户拍板：**混合型整本按扫描件处理，不做逐页拆分拼接**——两个不同引擎的产出缝在一起
会有格式/顺序接缝，检索时容易出怪结果；整本交给同一个引擎（MinerU 云端 vlm）从头读到
尾，产出一份连贯完整的 Markdown。配额代价（纯文字页也被视觉认字一遍）在个人库量级下
可忽略（每日 1000 页最高优先级额度余量大）。同轮决策：**read_document MCP 工具暂缓**
（用户主力模型已具备视觉能力，"导航确认 → 直接整份阅读原 PDF"路径成立，md/txt 笔记
agent 平台本就能按路径读；纯语言模型路径当前无真实使用者）——门禁设计存档进 TODO
暂缓条目，将来启用不必重新论证。

### 实现
- **extractors.py**：
  - `_extract_pdf` 重写分拣：逐页检测文字层不变（`_TEXT_PAGE_MIN_CHARS=10`），判定从
    「占比 < 0.5」改为「**存在任何图片页**（`text_pages < page_count`）→ 整本走扫描件
    分支」。阈值只决定"这一页算什么"，整本走向只看"有没有图片页"——边缘字符抖动
    不再能让整本判定翻转。
  - 扫描分支收拢：**只有 `mineru-cloud` 送云端**（is_ocr=True，整本一次提交不逐页拆分），
    其余任何取值（none 默认 / mineru-local 占位 / 文字层语义的 local / 未知值）一律
    跳过落 scanned 终态。收拢顺带封死旧代码的一个口子：旧分支对未匹配取值会
    fall-through 到云端调用（如试验台把 `local` 覆盖值传进扫描分支时）。
  - 未启用云端时的警告文案更新为「PDF 含图片页…已整本跳过」（覆盖混合型与纯扫描件）。
  - 删除 `_TEXT_PAGE_RATIO` 常量（占比阈值层废除，避免留下"看起来还在用"的死规则）。
  - `EXTRACT_VERSION` 3→4：v3 及更早版本对混合型文件会产出半份 local 结果，v4 语义下
    这类文件要么整本云端要么 scanned，产出不同；版本号递增使旧缓存（含 local 路由的
    半份结果）整体失效，防止 v4 的 text_route 候选误命中 v3 半份缓存。
- **index.py / server.py / GUI 零改动**：scanned 终态的 xsrc 自愈机制现成覆盖"开启云端
  后下一轮自动整本重试转正"；试验台的联网确认弹窗（`_will_call_cloud`）按扫描后端
  判定，与新路由天然一致。

### 测试（tests/test_extractors.py，+6 例，56/56）
- `test_mixed_pdf_whole_file_scanned_without_backend`：混合型未开云端 → 整本 scanned；
- `test_mixed_pdf_text_majority_also_routes_to_scan_branch`：Note9 形态（文字页占多数，
  旧规则 ratio=0.8 会判文字层直提）→ 现在同样整本扫描分支；
- `test_mixed_pdf_whole_file_cloud_ocr_when_enabled`：开云端 → is_ocr=True + route=
  ocr:mineru-cloud + 整本一次提交（files 数组单元素，不逐页拆分）；
- `test_all_text_pages_still_local_route`：纯文字层 PDF 回归不变（route=local）；
- `test_scan_branch_only_mineru_cloud_sends_to_cloud`：none/mineru-local/local/未知值
  四种取值全部跳过且零网络调用（注入会炸的网络桩，fall-through 即红）；
- `test_page_text_threshold_9_vs_10_chars`：9 字符页=图片页、10 字符页=文字页的阈值边缘。
- 六件套全绿：extractors **56/56**、audit **38/38**、registry **15/15**、singleton **5/5**、
  config_editor/gui_store 0 failures、verify_export_import **39/39**（同时补上了问题33
  当时未在本机 .venv 跑过的 7 例——本轮全量通过）。

### 遗留
- 存量影响：本机真实库当前没有任何 PDF，升级零迁移负担。将来若发现"老 PDF 还是旧
  结果"，做一次全量重建即可（EXTRACT_VERSION 已保证缓存不会假命中，全量只是省心）。
- 开启云端后的第一轮若恰逢云端故障，混合型文件会从"半份"变成"提取失败终态"（旧块
  清理），恢复后自动重试转正——"宁可诚实空缺、不留半份"原则的代价，接受。
- MinerU 云端批量并行加速（8.2.4 四步方案 + 9.6 健壮性结论）按既定纪律单独开轮，
  紧随本轮实施（见问题 35）。

## 问题 35：MinerU 云端批量并行加速（2026-09-03）

### 背景
TODO backlog 既定项：`_mineru_cloud_extract` 是"提交→上传→轮询[`time.sleep` 原地阻塞]
→下载"的单文件阻塞流程，`_index_core` 主循环逐文件顺序处理、零并发原语——库里有几十
上百份课件要送云端时逐份串行等待，是纯网络 I/O 浪费。方案经调研会话定稿（四步法 +
九个工程健壮性问题的结论），用户批准本轮与问题34 连续实施、各占一个提交。

### 认知基线（方案设计阶段确认，实施时不再重新论证）
- 自建 MinerU 服务的并发配置（环境变量/启动参数/扩容）与云端托管 API 完全无关，不可照搬；
- 官方频控：三个提交接口共用 50 个文件/分钟（滚动）、5000 文件/天、1000 页/天最高优先级
  （超出降级不拒绝）、单批 ≤50 文件——个人库量级距离触顶有量级余量，限流是"体面退让"
  的边界情况，不是要突破的瓶颈；
- "同时处理中任务数"上限官方未公布，并发数必须保守起步（默认 3，做成配置可实测摸高）；
- 只并行网络 I/O 段：`ThreadPoolExecutor` 足够（无需多进程）；所有共享状态变更保持在
  主线程单线程执行，天然无竞态。

### 实现
- **config.py / gui/config_editor.py**：新增 `mineru_concurrency`（int，默认 3，进
  `_POSITIVE_KEYS`；1=串行=并行化前的旧行为，回退用）与 `mineru_rate_per_minute`
  （int，默认 45，0=不限速；对应官方 50/分钟频控留安全余量）。两键进「PDF 与云端
  OCR」组 FIELD_META/fields（test_config_editor 静态契约强制，问题31 机制生效）。
- **extractors.py**（并行基础设施）：
  - `_classify_mineru_code` + `_MineruSubmitError(kind, code)`：提交错误三分类——
    transient（网络异常/429/-10001/-60007/-60009）指数退避+抖动重试（`_SUBMIT_MAX_
    ATTEMPTS=4`，429 尊重 `Retry-After` 头封顶 60s）；fatal（-60002/-60004/-60005/
    -60006）立即失败不重试；token（A0202/A0211）置全局失效标志 `_token_invalid`，
    同批后续请求快速失败、调度方取消未启动任务（停止为注定失败的请求烧频控配额）。
    未知错误码按 transient 处理（宁可多试一次，不误杀）。
  - `_window_delay`/`_submit_gate`：滑动窗口限速（发送时间戳队列 + 最近 60s 计数），
    每次提交尝试（含重试）各占一槽；持锁等待=提交节奏串行化，正是限速目的。
  - 断点簿记 `data/extract_cache/mineru_pending.json`（`_pending_add/_remove/_match/
    mineru_pending_prune`）：上传成功即落 `{batch_id: {path, route, md5}}`，进程被杀
    后条目留存；下一轮同一文件提取时按 path+md5 匹配 → `_mineru_resume` 续接服务器
    端结果（轮询/下载/写缓存），绝不重复提交。终态语义：成功/failed/gone/no-md/empty
    → 移除条目；timeout/download/网络异常 → 保留条目（服务器端任务可能仍在跑或结果
    仍可取）。簿记文件放提取缓存目录下——试验台的隔离缓存目录天然隔离其预览任务
    的中断条目，不会污染生产簿记（红线 7 同一教训）。孤儿清理（文件不在本轮集合/
    字节已变化）在云端段开始时执行。
  - `_mineru_poll_result`：轮询+下载从 `_mineru_cloud_extract` 抽出（新鲜提交与续接
    共用同一条路径）；返回结构化 `(md, why)`，调用方按 why 决定簿记去留。
  - `classify_extraction(path, backend, md5)`：分流预判，与 `_extract_pdf` 路由条件
    严格同源（漂移只影响并行收益、绝不影响产出正确性——真正执行仍走 extract_to_
    markdown 完整路径）；返回 `(kind, pages, is_ocr, route)`，kind=cloud 才攒批。
  - `mineru_cloud_extract_for_parallel(path, is_ocr, key)`：worker 显式入口——PyMuPDF
    不保证多线程安全，worker 只做纯网络 I/O 绝不触碰 pymupdf（路由判定全部在主线程
    classify 阶段完成）；md5 透传免重复读盘。
  - `mineru_quota_add/today`：每日文件数/页数计数（`mineru_quota.json`，翻篇自动清零）。
    仅提醒非硬门禁——降级≠失败，阻断反而制造问题。
- **index.py**（主循环改造，四步法落地）：
  - ①分流：二进制分支先 `classify_extraction` 预判，cloud 且并发>1 → 攒进 `cloud_jobs`
    （rel/fpath/st/bhash/is_ocr/route/pages）继续扫描；inline（缓存秒回/本地直提/
    快速失败）按原路径当场提取。扫描段绝不原地阻塞等云端。
  - 成功路径抽取 `_store_chunks(rel, st, bhash, front, body)` 闭包：链接抽取→清洗→
    空内容防御→两级切块→meta 写入→进度，文本类/本地二进制/云端结果三条路径共用
    （与抽取前内联版本逐行等价）；G7 单点还原语义不变（converting→scanning 紧跟
    extract_to_markdown 返回处）。
  - ②③并行执行+主线程收口：云端段 `ThreadPoolExecutor(max_workers=min(并发, 任务数))`
    包 `mineru_cloud_extract_for_parallel`，`as_completed` 逐个回主线程——失败落
    `_terminal_entry`（带 xsrc）、成功走 `_store_chunks`；Token 失效时 `fut.cancel()`
    取消未启动任务（被取消文件本轮不动 meta，下轮自然重试）。
  - ④进度批量语义："待送云端（已收集 N 个）"→"MinerU 云端并行处理 N 个文件（并发
    M，中断的任务自动续接）"→"云端处理中：已完成 K/N（文件）"；converting 相位豁免
    停滞告警天然覆盖云端长等待。
  - 配额提醒：分发前按 classify 页数预估，今日累计将超 800 页（1000 页额度的 80%）
    打日志提醒"会被降优先级、变慢"。
- server.py / retriever.py / GUI 运行时零改动（config_editor 元数据除外）。

### 测试（tests/test_extractors.py，+11 例，67/67）
- extractors 层：`test_rate_limiter_window_math`（满额等待/窗口滑动/0=不限）、
  `test_mineru_submit_retry_transient_then_success`（两次网络失败后退避重试成功，
  断言 POST 计数与退避记录）、`test_mineru_submit_respects_retry_after`（429+头 →
  恰按 7s 等待）、`test_mineru_submit_fatal_code_no_retry`（-60005 仅 1 次 POST 零退避）、
  `test_mineru_token_error_sets_flag_and_folds`（置全局标志+后续调用零请求）、
  `test_mineru_pending_record_resume_after_interrupt`（轮询途中"被杀"→簿记留存→
  下轮续接：POST 计数不增、簿记清空、缓存写入）、`test_mineru_pending_prune_orphans`
  （孤儿/变更文件条目清理）、`test_classify_extraction_matrix`（8 分支矩阵）、
  `test_mineru_quota_counter`。
- index 端到端（_IsoEnv 隔离）：`test_index_cloud_parallel_dispatch_and_chunking`
  （3 份含图 PDF，Barrier 断言真并发 ≥2，meta 出块、簿记清空）、
  `test_index_token_abort_skips_remaining`（4 任务并发 2：仅 1 个真正上云，其余快速
  失败/取消，索引正常完成）。
- 开发中修了一处自测暴露的 bug：`_mineru_submit` 对 token 类错误 raise 前漏调
  `_token_invalid.set()`（测试先红后绿，正是"Token 失效不置标志则调度方无从取消"）。
- 六件套全绿：extractors **67/67**、audit **38/38**、registry **15/15**、singleton **5/5**、
  config_editor/gui_store 0 failures、verify_export_import **39/39**。

### 备注
- 模拟并发下的真实网络（429/降级）仍属人工冒烟范畴；官方未公布并发上限，`mineru_
  concurrency` 保持保守值 3，用户实测无 429/无大量降级后可逐步调高。
- 断点续接只能挽回"已上传成功"的任务（提交失败的任务服务器端不存在，重提交即是
  正常路径）；簿记文件随缓存目录走，试验台预览中断的条目随临时目录销毁。
- 并行只覆盖"网络 I/O 等待"的重叠；嵌入/写库仍与之前完全相同（锁外编码+锁内写入），
  不在本轮范围。

## 问题 36：`mineru_concurrency=0` 最大吞吐模式 + 轮询瞬时异常退让（2026-09-03）

### 背景
问题35 交付后用户提出：多文件同时送改成可选的"最大限度送"——接近 Limit 就停下，
等结果返回继续送，直到完工。关键澄清：官方的"Limit"是**每分钟提交数**（三接口共用
50/分钟滚动窗口），不是"同时在跑任务数"（该数字官方未公布）；因此 max 模式的正确
形态不是"猜一个更大的并发数"，而是**把固定并发上限拿掉、让既有的滑动窗口限速闸门
成为唯一节流阀**——窗口没满立刻送（最大限度），接近频控原地等（停下），窗口滑动
自动续送；任务完成腾出的线程让排队文件立刻补位（等结果返回继续送），直到全部完工。

### 实现
- **index.py**：`_mineru_concurrency()` 语义扩展——`0`（或负数）= 最大吞吐模式；
  `1` = 串行（旧行为）；`≥2` = 固定并发（默认 3 不变）。max 模式下线程池开到
  内部上限 `_MINERU_MAX_POOL=128`（防异常规模任务撑爆本机线程；个人库规模到不了，
  到达即意味着先撞上每日页数配额，多排队无害）；分流判定条件由 `>1` 修正为 `!=1`
  （max 模式返回 0，旧条件会把它误判成串行走内联路径——自测前人工审查发现）。
- **extractors.py**：`_mineru_poll_result` 响应分流修正——429/5xx 属**瞬时异常**，
  deadline 内按 Retry-After（封顶 60s）或 2×轮询间隔退避后继续轮询，绝不误判任务
  失败（max 模式在途任务多、轮询请求密，撞限流必须体面退让）；404/非 JSON 响应
  （服务器不认识该 batch）才判 `gone`，且 json 解析失败不再外泄（旧代码遇到非 JSON
  响应体会异常外泄 → 外层折叠但簿记条目永久滞留，卡死在"续接一个不存在的任务"上）。
- **config.py / gui/config_editor.py**：`mineru_concurrency` 移出 `_POSITIVE_KEYS`
  （0 是合法取值），DEFAULTS/模板/GUI hint 同步三档语义。

### 测试（+4 例，71/71）
- `test_mineru_concurrency_max_mode_parsing`：0/负数→max、1→串行、5→5、垃圾值→3；
- `test_index_max_mode_all_jobs_in_flight`：5 任务 barrier(5) 全员同时在飞（无固定
  并发上限的端到端证明），meta 正常出块；
- `test_mineru_poll_429_transient_retries_within_deadline`：429 按 Retry-After 等 2s、
  500 按 2×轮询间隔等 6s，最终成功且簿记清空；
- `test_mineru_poll_gone_nonjson_removes_pending_entry`：404 非 JSON → gone + 簿记
  条目移除（堵永久滞留）。
- 六件套全绿：extractors **71/71**、audit **38/38**、registry **15/15**、singleton **5/5**、
  config_editor/gui_store 0 failures、verify_export_import **39/39**。

### 备注
- max 模式的实际节流 = `mineru_rate_per_minute`（默认 45/分钟）；任务耗时分钟级时
  稳态在途数 ≈ 提交速率 × 任务时长，个人库规模下先撞到的通常是每日 1000 页优先级
  配额（超出降优先级、任务变慢但仍完成——超时走既有"簿记保留、下轮续接"路径）。
- 默认值仍为 3：max 模式是**可选项**，用户按需把 `mineru_concurrency` 设为 0。


## 问题 37：WEMM 页级视觉导航 + read_document + 近似文档去重（2026-09-03）

### 背景
纯 bge-m3 文字索引只能定位"哪份文档命中"，检索结果也没有页码概念；扫描件 PDF 更是
整本无字（问题34 的遗憾缩影）。用户提出：加一个**页级视觉导航**——把每份 PDF 的每一页
渲染成图、交给多模态嵌入模型编码成"每页一个向量"，检索时告诉 AI"内容在哪个 PDF 的哪一页"，
从而让有视觉能力的模型直读原 PDF 对应页。配套两条支撑功能：`read_document`（按文件取完整
MD 正文 + 绝对路径）与**近似文档去重**（找出库内内容几乎相同的重复文档）。

### 决策
- **模型 WeMM-Embedding-2B**（腾讯微信视觉团队，多模态 2B，512 维 matryoshka），
  **本地 transformers 服务** `wemm_server.py` 跑在**全局 Python**（torch 2.11+cu128、
  transformers 5.14.1，RTX 5060 Laptop 8GB VRAM 实装 5.08GB 可容纳）；**项目 .venv
  零依赖**——`.venv` 只通过 HTTP 调本地服务，加载模型/编码全部发生在服务进程。
  弃用 Ollama（`/api/embed` 不接受图片、`/api/chat` 不输出 embedding，issue #7677 未解）。
- **两套向量库彻底分离**（红线：绝不混向量空间）：文字索引 `obsidian_kb`（bge-m3 1024 维）
  与页级 `obsidian_kb.wemm`（WeMM 512 维 + 独立 meta `data/wemm_meta_<库>.json` +
  独立版本号 `WEMM_VERSION=1` 独立自愈）；`navigate_knowledge` 绝不与 bge-m3 文本分数混合。
- **默认关闭（隐私/资源优先）**：`wemm_backend=off`；`wemm_server.py --unload-after` 空闲
  释放显存与 bge-m3 共存。
- **门禁/红线**：页级索引在 stat 前冻结未授权文件（红线6/7，零渲染零编码零 I/O，条目与页
  原样保留不裁剪）；`read_document`/去重只读既有提取缓存（`read_cached_markdown` 零触发，
  绝不后台启动扫描件 OCR / 云端 MinerU，红线7）。

### 实现
- **wemm_server.py**（全局 Python 本地看图服务，`python wemm_server.py --port 9101`）：
  `GET /health`（模型/维度/显存）；`POST /embed`（image 页图 base64 或 text → 512 维
  归一化向量）；不支持的 dim 返回 400；日志只含方法/路径不含图文内容（API Key/内容不泄漏）。
  已实测：文本"manometer pressure gauge fluid mechanics"与 Manometer 页图 cos 0.558，
  无关"quantum entanglement" cos 0.357，确定性可复现。
- **wemm_indexer.py**（项目 .venv）：pymupdf 逐页渲染（DPI 120，内存完成不写盘）→ base64 →
  HTTP 编码 → 写独立 `wemm_<collection>`；增量靠 size+mtime 快速路径 + MD5 字节指纹；
  一致性自愈（meta 期望页数 ≠ Chroma 实际 → 全量重建）；版本升级强制重建；删除文件精确清理
  页向量并裁剪 meta；统一终态（empty/extract-failed 不产向量也落 `_terminal_entry` 防 stale
  死循环）；CLI `--library name|all --full --backend on|off`。
- **wemm_retriever.py**：`wemm_search(query, libraries, top_k)` → 文字查询编码 → 对每库
  `wemm_<collection>` 余弦检索 → `(库, 相对路径, 绝对路径, 页码, score)` 降序；backend off /
  服务不可用时返回空 + 明确提示；某库页库损坏跳过不阻断整体。
- **server.py 两个新 MCP 工具**：
  - `navigate_knowledge(query, top_k, libraries, exclude)`：页级视觉导航，返回
    "库/[相对]（绝对路径）第 N 页 + 相似度"，需 wemm_backend 开启。
  - `read_document(library, path)`：按库内相对路径或不含扩展名标题定位（复用文字索引 meta），
    返回源文件绝对路径 + 完整正文；md/txt 直读源文件，pdf/docx 走只读缓存（未提取提示先索引）。
- **extractors.read_cached_markdown(path)**：零侵入缓存读（红线7实现的落点）——只查既有提取
  缓存，绝不触发新提取。
- **dedup.py**（文本级 MinHash+LSH，纯标准库 hashlib）：正文 → 4-gram 碎片 → bottom-k
  MinHash 签名（保留 k 个最小互异哈希；满 k 用 |A∩B|/k、未满用精确交并比）→ LSH 分桶
  （16 段×4 行）→ 桶内 Jaccard ≤ 阈值 → 连通分量分组；**只读建议绝不删改文件**；md/txt 直读源、
  pdf/docx 只读缓存（未提取跳过计数）。MCP 工具 `find_duplicates(library, threshold)`。

### 测试（新增 3 个测试文件：test_wemm_indexer 26 例、test_wemm_retriever 13 例、test_dedup 19 例）
- indexer：页数=向量数、门禁零渲染零编码、增量子自愈、内容变化重编码、删除精确清理、
  损坏 PDF 落 extract-failed 终态、版本升级全量重建、源目录零写入、collection 命名隔离。
- retriever：backend off 空+提示、服务 down 空+提示、命中排序/过滤/绝对路径透出、
  libraries 过滤、空页库不报错。
- dedup：sketch 自比 1.0 / 异文近 0 / 短文精确比、相同文档检出组、阈值过滤、完全不相关
  无组、未提取 pdf 计 skipped、连通分量归并、源目录零写入。
- 实测冒烟：临时库 ManometerEquation.pdf（9 页）→ WEMM 入库 9 向量 → navigate 查询
  "manometer pressure gauge fluid" 命中第 2/1/5/8 页（cos 0.51–0.58）；
  `read_document` 对 Obsidian Vault 一篇 md 返回绝对路径+全文；`find_duplicates` 对
  Obsidian Vault 扫 164 份无重复；冒烟后临时库/页库/服务已清理。
- 全量回归：六件套（audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store
  0 failures、extractors 71/71）+ 新三套全绿。

### 备注
- WEMM 页导航默认关闭，开启步骤：Config→视觉导航（WEMM）设 wemm_backend=on/local →
  `python wemm_server.py --port 9101`（全局 Python）→ `wemm_indexer.py --library <名> --backend on`
  → navigate_knowledge 即可用。
- 639 个 PDF 的全库页索引是**重活**（~2.4s/页冷启动、热态更快），建议按需对单个库开；
  `--unload-after N` 空闲释放显存与 bge-m3 共存。
- 去重是纯文本级、建议性质，不产向量不改索引，可放心对任意库跑。

## 问题 38：失败溯源（index_failures）+ WEMM 可确认手段（wemm_status）+ 渲染 DPI 档位 + 真实全量建库验证（2026-09-04）

### 背景
问题37 交付两件事：`index_failures` 的姊妹思路已在问题35/36 中提出（「失败清单」诊断工具），
以及用户要求**确认真实库上 WEMM 是否真的生效、由自己亲眼确认**（不能在测试冒烟里自证）。
两个诉求：
(A) 把"提取静默失败"变成可溯源清单；
(B) 让"WEMM 到底建没建、生效没生效"有用户可确认的手段。
另：复杂度发现——**单页嵌入耗时随渲染 DPI 强相关**（非问题37 里估的固定 ~2.5s/页）：
40 DPI≈0.5s/页、60≈2.5s、90≈13.2s、120≈25s；渲染本身仅 0.1s。LECTURE NOTE 共 **382 页**
（18 份 PDF，流体力学课件），60 DPI 全量约 16 分钟、120 则 2.6 小时。

### 决策
- (A) 加诊断工具 `index_failures(library, include_ok)`：读 `index_meta_*.json` 按终态原因
  （unreadable/extract-failed/empty/tbd/scanned）分组列出失败文件，并对「下轮将自动重试」的
  条目标注 `〆`（判定与 `_backend_changed` 同源：reason∈scanned/extract-failed 且
  `xsrc != current_backend_sig()`）。事件日志里的 API Key 仍不进内容（红线8）。
- (B) 两者都做：**加确认工具 `wemm_status()`**（每库 PDF 数/页向量/后端开关/服务存活/渲染失败
  清单，一眼确认真能用）+ **真建全库 WEMM 页索引实证**。渲染 DPI 做成**用户可选项**（默认 60），
  档位 40/60/90/120，改后需 `--full` 重建才能生效——把"快但糊 vs 慢但清"的选择交给用户。
- 长驻 MCP server 的 `CFG` 是 import 时快照：用户中途开关 wemm_backend/改 DPI 后旧进程读不到。
  `navigate_knowledge`/`wemm_status` 改为调用时用 `config.load_config()` 现读 WEMM 相关键。

### 实现
- **server.py 两个新 MCP 工具**（插在 `_dedup_report` 后）：
  - `index_failures(library="", include_ok=False)`：按库分组列失败文件 + `〆 下轮将自动重试`
    标注；import `current_backend_sig` 与 `REASON_*` 共用常量（不硬编码）。
  - `wemm_status()`：读每库 `wemm_meta_*.json` + 实时 config（后端/DPI）+ `health()`，
    报「库：N 份 PDF、M 页向量、K 份渲染失败」；后端 off 时清晰提示不可用。
  - 新增 `_wemm_cfg()` helper：调用时现读 `config.load_config()` 取 wemm_backend/wemm_url/
    wemm_render_dpi 三键，`navigate_knowledge` 门禁与 `wemm_status` 状态都用它（不信任快照）。
- **config.py / gui/config_editor.py**：新增 `wemm_render_dpi`（int，默认 60，choices 40/60/90/120，
  FIELD_META 标 rebuild:True → 改后需 `--full` 重建）。config.json 已自动补写该键。
- **wemm_indexer.py**：`WEMM_RENDER_DPI` 120→60；`index_wemm_library` 读 `wemm_render_dpi`
  并按 `render_page_b64(..., dpi=dpi)` 生效。

### 真实建库与端到端验证（用户可亲眼确认）
- 全库页索引（`--library all --backend on`，60 DPI，后台跑 ~16 分钟）：只有 LECTURE NOTE 有
  PDF（18 份/382 页），Obsidian Vault/test/agents/skills 均 0 PDF → 页库正确为空。**382 页向量、
  18 份 PDF meta 全部落库**。
- `wemm_status()`：`wemm_backend=local，渲染分辨率 60 DPI`、看图服务存活
  （tencent/WeMM-Embedding-2B）、LECTURE NOTE 报「18 份 PDF、382 页向量」，其余库「尚未建页索引」。
- `navigate_knowledge("Navier-Stokes equation viscous incompressible flow")` 返回真实命中：
  Bernoulli 方程 PDF 第 6/11/7 页（相似度 0.62/0.61/0.59）、Fluid Statics 第 33 页等，含绝对路径。
- 说明：`index_failures` 报 LECTURE NOTE 有 5 份 `extract-failed`（FLUID MECHANICS_*），但 WEMM
  页向量对这些 PDF 照样建出来了——页级视觉导航与文字提取相互独立，即使文字层提取失败也能看图导航。
- 交付过程的环境坑（记录备用）：Windows 下 `.venv\Scripts\python.exe` 是**重定向 shim**，会再
  spawn 一个真实解释器子进程——同一 launch 永远显示为"2 个 python.exe（同 cmdline）"，曾误判为
  双开去"杀重复"结果把真 worker 杀了。判定单实例要认 shim+子进程成对，别按进程数。后台长任务
  用 `schtasks /Run`（脱离本 shell，避免工具对前台子进程的 tree-kill），`/TR` 命令行有 261 字符
  上限需包一层 .cmd。

### 测试（test_extractors.py：71/71——含配置隔离修复）
- 修复了一批**既有环境暴露的测试隔离 bug**：真实 `data/config.json` 把 `pdf_scan_backend`/
  `pdf_text_backend` 都设成 `mineru-cloud`（带真 Key），而 extractor 用例的**前置假设**是后端为
  默认 local/none，读到真实配置即报「存在测试间配置泄漏」，一次挂 33 例。
  - 根因：`config.CFG` 是进程启动时从真实 config.json 一次性加载的全局单例，被用户生产配置污染。
  - 修法（不改用户磁盘 config.json）：`_run_all()` 包一层快照——跑测前把 OCR/路由相关键重置为
    `config.DEFAULTS`，测完原地还原。
  - 另两个遗留：`test_preview_job_process_isolation`（子进程 Windows spawn 重读真实 config →
    文字 PDF 被带偏去云端）→ 显式 `backend="local"`；`test_preview_job_uses_isolated_cache`
    （依赖环境全局 `pdf_scan_backend=none`）→ 用例内显式锁定 none。
  - 修复后 extractors **71/71 稳定**（两次跑一致）。
- 全量回归：extractors 71/71、audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store
  0 failures、verify_export_import 39/39、test_wemm_indexer 26、test_wemm_retriever 13。

### 备注
- 用户要亲证 WEMM 生效：**重启 MCP server**（加载新 server.py + 现读 config），然后调
  `wemm_status()`（看 382 页向量）→ `navigate_knowledge(...)`（看真实命中页）。
- `index_failures` 暴露的待办：`~$BAT3_Technical_Report.docx` 是 Word 锁临时文件（可删）；
  LECTURE NOTE 5 份 FLUID MECHANICS_* `extract-failed`（xsrc=当前签名，不会自动重试，需人工处理）。
- 60 DPI 是速度/精度折中；要更高版面清晰度可改 `wemm_render_dpi` 后 `--full` 重建（耗时见背景）。


---

## 问题39：全面质量审查修复轮（2026-09-04）

### 背景
用户请另一 agent 完成了问题37（WEMM 页级视觉导航 + read_document + 去重）与问题38（失败溯源
+ wemm_status + DPI 档位）后，要求复查其质量。审查（两个独立通读 + 关键结论逐条源码复核）
确认了红线合规面扎实（门禁镜像、零触发提取、无 Key 泄露、去重只读、页图不出本机、测试隔离），
但发现一个 P0、若干 P1/P2，本轮全部修复。用户约束：只改本项目文件、零联网零下载、不动本地
配置环境；跨工作区（Vault 文档组在 D:\_STOREROOM 另一仓库）本轮跳过待用户决策。

### 修复清单（按严重度）
1. **WEMM 失败终态死寂（P0）**：`wemm_indexer` 的 size+mtime 快速路径对终态条目照跳，日志写
   "记入终态待重试"却无任何重试机制——看图服务在索引中途抖一次，该 PDF 永久退出页级导航，
   直到人工 `--full`。这是统一终态红线想防的"死循环"的对偶缺陷"死寂"。修法：终态与成功条目
   一律携带 `xsrc = wemm:<模型>:<维度>:<DPI>` 能力签名；快速路径要求 `xsrc` 匹配且非终态；
   失败条目每轮真重试（失败原因多为服务不可用，重试成本仅一次 page_count/首页渲染即失败，
   可忽略）；改 DPI/换模型自动全量重渲染（原实现只能靠人工 --full，页库会静默滞留旧档）。
2. **写库假账（P1）**：原实现整库一把 `collection.upsert`（Chroma 单批有上限，大库直接炸），
   且 meta 在渲染循环里就记了成功页数——upsert 失败 → 下轮 count 失配 → 整库重编码 → 再失败
   的烧 GPU 循环。修法：分批 upsert（1000/批）+ 全部批次成功才把成功条目并入 meta（`pending_ok`
   延迟落账），写库失败宁可下轮重渲染，不留假账。
3. **显存管理（P1，用户重点）**：`wemm_server.py` 三处——①启动即加载 5.1GB 模型 → 改懒加载
   （启动只绑端口，首个 /embed 才进显存，"需要才拿去"）；②`--unload-after` 只在"有新请求进来"
   时检查空闲，而空闲的定义恰恰是没有请求，永不触发 → 改后台守护线程每 30s 检查 + 卸载时
   clear 引用 + gc + empty_cache 真正释放；③/health 抢模型锁 → 模型加载/编码期间 health 被
   挡 5s 超时，检索方误报"服务不可用" → 改快照读不持锁。另：dtype 参数兼容新旧 transformers
   （`dtype=`+`torch_dtype=` 双传，加载后校验并告警，防旧版静默 fp32 显存翻倍）；编码结束后
   再刷 `_last_use` 防刚编完就被判空闲。
4. **配置热读自相矛盾（P1）**：问题38 声称修了"现读 config"，但只修了外层——`navigate_knowledge`
   外层现读判 on 放行，内层 `wemm_search` 仍读 import 快照判 off 拒绝。修法：新增
   `config.reload_config()`（原地更新共享 CFG dict），`_wemm_cfg`/`ensure_fresh`/`reindex_knowledge`
   /`find_duplicates` 统一在任务边界调用——顺带修掉更重的同类问题：长驻 MCP 进程里 agent 触发的
   reindex 此前完全感知不到用户中途在 GUI 补的 OCR Key/切的后端。
5. **navigate_knowledge 库范围违约（P1）**：docstring 承诺"空=默认库"，实际传 None 给
   `wemm_search` = 搜全部注册库（test/agents 等非笔记库混入）；"all"+exclude 被丢弃。修法：
   统一走 `resolve_entries(libraries, exclude, defaults=...)`。**提示死循环**：工具让 AI"先调
   reindex_knowledge 跑 WEMM 页索引"，而 reindex 根本不建页库——改为指路 CLI
   `python wemm_indexer.py --backend on`。
6. **read_document 补齐存档设计（P2）**：问题37 实现与 TODO 存档设计不符——抬头缺字数/产出
   方式、正文超 2 万字符截断。修法：`read_cached_markdown` 命中改返回产出路由（原样丢弃），
   抬头补 `字数：N　产出方式：本地提取/MinerU 云端 OCR/…`，正文不截断（工具定位就是交付全文）。
   read_document 的保留系用户委托处置待办（2026-09-04"针对代办方案自行决定"），已在 TODO 记录
   下线路径。
7. **index_failures 判定与实际行为相反（P3）**：`will_retry` 要求 xsrc truthy，而 index 的
   `_backend_changed` 对缺 xsrc 的旧条目（None != sig）会真重试——溯源结论说"不重试"实际会重试。
   修法：直接复用 `_backend_changed` 同一谓词；空串 reason 折叠为 `unknown` 并在报告尾部兜底
   渲染（原来从报告无声消失）。
8. **dedup bottom-k 估计量偏置（P2）**：满 k 时直接 |A∩B|/k，把"在两边 bottom-k 里但大于并集
   第 k 小值 z"的交集元素多算——阈值附近边界对系统性偏高（假阳性）。修法：z = 并集第 k 小值，
   分子只数 ≤z 的交集元素。附反例 k=2：A={1,3},B={2,3} 真值 1/3，旧实现估 0.5。
9. **页级检索写副作用（P2）**：`wemm_search` 查询路径用 `get_or_create_collection`——查询会给
   生产 Chroma 创建空 collection。改 `get_collection`；单库异常不再静默吞掉，逐库汇总进 err。
10. **wemm_server HTTP keep-alive（P2）**：HTTP/1.1 下 404/超限分支不读净请求体，同连接下一
    请求把残留字节当请求行解析。修法：先读体再路由 + 错误响应置 close_connection。
11. **死代码/风格（P3）**：wemm_indexer 未用常量与导入清理；server 未用 REASON_* 导入、
    `import os as _os`、函数内重复导入；dedup 硬编码扩展名（含不存在的 "markdown"）改
    `TEXT_EXTS | BINARY_EXTS` 单一事实来源；`dedup._report` 提升为 `format_report` 供 server
    复用（删两处逐行重复）。

### 测试（新增 5 用例；wemm_indexer 36、dedup 23、extractors 72）
- `test_failed_pdf_retried_next_round`（P0 复现先行：服务抖动 → 终态带签名 → 恢复后次轮转正）
- `test_sig_change_reencodes`（DPI 改档自动重渲染 + meta 签名更新）
- `test_sketch_jaccard_bottomk_z_truncation`（z 截断反例 + 满签自比 1.0 + 单侧空 0）
- `test_read_cached_markdown_zero_trigger_and_route`（未命中不触发不写缓存 + 命中返回路由 +
  unsupported 拒绝）
- 其余全量回归：audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store 0 failures、
  wemm_retriever 13、verify_export_import 39/39。

### 备注
- 既有 WEMM 页库（问题38 建的 382 页向量）的 meta 条目无 `xsrc` 字段，问题39 后首轮增量会
  整体重渲染一次（一次性成本，正好把旧 DPI 向量统一到当前档位），此后稳定。
- `wemm_server.py` 跑在全局 Python（项目外依赖），本轮只改项目内文件未动全局环境；懒加载
  改动对用户透明：启动后 /health 返回 loaded=false，首个导航/索引请求自动加载（首次多等
  数十秒）。
- Vault 文档组（20-Projects/Obsidian RAG/）的同步更新本轮按用户约束跳过（跨工作区），待用户
  决策后补。

---

## 问题40：GUI「文件生效明细」面板——逐文件确认生效状态（2026-09-04）

### 背景
用户反馈两个点：①确认 WEMM/MinerU 是否生效，现有手段（wemm_status / index_failures /
"382 页向量"这种数字）不直观——看不出**哪个文件**是否正确生效；②问题38 交付的失败清单
"列出展开"体验没做好（MCP 输出只有 AI 能看，且 GUI 状态卡上的失败提示只有计数没有文件名）。

### 交付（零侵入，红线4：只读 meta/注册表，不加载模型、不碰 Chroma）
- **store 层（gui/store.py）**：
  - `file_index_rows_for(cfg)`：单库逐文件"未正常入索引"明细（rel + reason + will_retry），
    will_retry 复用 `index._backend_changed` 同一谓词——GUI 说"下轮会重试"就真会重试；
  - `wemm_status_for(cfg)`：单库逐 PDF 页索引状态（页向量数 / 渲染失败原因），
    meta 不存在或为空 = 尚未建页索引；
  - `wemm_backend_state()`（现读 config）/ `wemm_service_probe(url)`（127.0.0.1 回环
    短超时探测，人话返回：模型已进显存 / 待首次请求加载 / 未启动；只读绝不拉起服务）。
- **widgets 层**：新 `FileStatusDialog`——库下拉 + 两个分区：
  - 文字索引区：✔ 正常 N 份；每个失败/跳过文件一行（图标 + 文件名 + 人话原因 +
    "✅ 下轮索引将自动重试"标注）；
  - WEMM 区：后端未开启给三步开启指引；已开启则后台线程探测服务存活（不阻塞 UI）、
    每份 PDF 一行"已建 N 页向量"或"渲染失败：<原因>"；**点任意行用系统默认程序打开
    那份 PDF**（tooltip 显示绝对路径）——配合 navigate_knowledge 返回的页码翻页对内容，
    眼见为实；
  - 主界面入口：顶部工具栏新增"文件生效明细"按钮；状态卡的 ⚠ 失败后缀文案同步指引。
- flet 0.86 API 适配（沿用既有先例）：Dropdown 事件为 `on_select`（构造器不收 on_change）、
  `ft.Padding(...)` 而非 `ft.padding.symmetric`、Container 只有 `on_hover`（e.data=='true'
  为悬入）。

### 测试（test_gui_store.py：55 用例）
新增 5 例：失败明细与重试判定（xsrc 与签名比对两个方向）、meta 缺失空态、WEMM 逐行/
缺失语义、服务探测三分支（mock health）、对话框无窗口构造冒烟（提前抓 flet API 错位——
on_change/on_exit/padding 三个 API 错位全是这个用例先抓出来的）。

### 附记（同轮）：真库演练"双份模型并存"溢出修复
用户实测发现跑回归时两个 python 进程同时持显存（一份 bge-m3 在主进程、一份在检索对比
子进程），8GB 卡直接 WDDM 溢出。根因：`verify_export_import` 第 0 节的主进程检索让模型
常驻，第 6 节又拉子进程各加载一份。修法：`index.release_model()` + `retriever.release_reranker()`
（释放常驻模型 + gc + empty_cache，下次懒加载回来；生产 server 路径不调用——检索模型
常驻是响应速度的根基），演练在第 6 节拉子进程前先让主进程吐模型；子进程本身串行。
验证：39/39 全过，15s 间隔采样全程无"两进程并存"。

### 附记（同轮）：明细面板点开失效文件的 WinError 2
用户点行打开 PDF 报 WinError 2（系统找不到指定的文件）。排查：`test` 库
（Desktop	est）整个文件夹已删除，但文字索引 meta 还留着 17 条旧记录（LECTURE NOTE
的 18 份 PDF 全部在位）。修法：明细行渲染时现查文件在位性——已不在原位置的行换
灰色图标 + 行内注明"文件已不在原位置（下轮索引自动清理）"且**不可点击**；即便点了
（如窗口刷新间隙），`_open_any_file` 也把 FileNotFoundError 折叠成人话提示而非裸错误。
旧记录本就会在下轮索引裁剪（meta 裁剪纪律），GUI 只是把它说破。

---

## 问题41：GPU 显存仲裁——单模型在线、按需拉起、用完即关（2026-09-04）

### 背景
用户拍板：WEMM 是核心功能，应默认开启且全自动——"有需要的时候再自动拉起，用完了直接
自动关闭，确保不和其他模型同时在线"。此前 WEMM 三件套（开关/服务/页库）全部手动，且
9月3日 冒烟遗留的旧 wemm_server 进程一直占着 5.09GB 显存（旧代码启动即加载、无卸载）。

### 交付：gpu_arbiter.py（显存仲裁，纯标准库，.venv 与全局 Python 共用）
1. **服务生命周期**：`ensure_server()` 幂等按需拉起 wemm_server（分离进程、日志
   `data/wemm_server.log`、PID `data/wemm_server.pid`；已有实例只等不重拉；拉起失败折叠
   为 (False, 人话提示) 指向 `wemm_python` 配置）。navigate_knowledge 与 wemm_indexer
   CLI 在需要时调用。**旧实例已清理**（PID 43276，冒烟遗留）。
2. **用完即关**：wemm_server 默认 `--unload-after 300`（空闲 5 分钟卸显存，原为不卸）+
   新增 `--idle-exit 1800`（卸载后再空闲 30 分钟、无在途请求 → 进程自退出，下次按需再拉起）。
3. **显存互斥（单模型在线）**：
   - WEMM 加载前 `wait_for_vram(≥5.5GB)`：bge-m3 在线时不硬抢，等它让路（检索侧空闲
     自动卸载），超时（900s）报错本条请求而非溢出；
   - bge-m3 加载前（index.get_model）`_vram_maybe_evict_wemm()`：空闲显存 <3.5GB 且
     WEMM 在线 → 发 `POST /evict` 抢占（检索优先），WEMM 被抢占的编码批次由问题39 的
     失败终态记账下轮自动重试；
   - MCP server 新增 GPU 空闲卸载守护：600 秒无检索/索引活动 → `release_model()+
     release_reranker()`（复用问题40 接口），"完工直接卸载"；
   - navigate_knowledge 开始前主动释放本进程的 bge-m3/reranker（页级导航用不到它们），
     给 WEMM 让位。
4. **fail-open 铁律**：显存探测（torch → nvidia-smi 兜底）失败返回 None，所有仲裁路径
   视作"无法判断 → 不阻塞不抢占"——仲裁机制自身故障绝不影响检索/索引可用性。

### 配置
- `wemm_backend` 默认 off → **on**（DEFAULTS/模板/GUI）；用户 config.json 本就已是 local（等价开）。
- 新增 `wemm_python`（默认 "python"）：拉起 wemm_server 用的全局 Python（须已装 torch），
  GUI「视觉导航」组同步。
- GUI wemm_status/明细面板文案更新：服务未启动 → "下次导航自动拉起"。

### 测试（新增 test_gpu_arbiter.py：28 例；全套件变十件套）
探测 fail-open / wait 分支 / evict 折叠 / ensure_server 四分支（已运行不重拉、冷启动
拉起+PID 落盘、存活实例只等、拉起失败折叠）/ 端口与 PID 边界。测试先红后绿抓出两个真
bug：**父进程日志句柄泄漏**（with 修复）与**遗留旧实例**（本机 9101 真有旧服务在跑）。
九件套全绿 + gpu_arbiter 28/28。

### 备注（显存时间线，用户视角）
- 全静默：0 模型在线（server 10 分钟自动卸、WEMM 5 分钟卸 + 30 分钟退）。
- 检索：bge-m3 上（~2GB），完走自动下。
- 导航/页索引：WEMM 上（5.1GB），检索若同时发生会先抢占 WEMM（页索引失败批下轮续）。
- 任何时刻最多一个模型驻留显存；加载都走懒加载，磁盘加载代价按用户决策不值一提。

### 附记（同轮）：建页库接入索引管线——增量/全量重建自动触发
用户指出预期：建页库应跟随索引自动跑，而不是只有命令行。落地：
- `index_library` 文字索引完成后自动调用新增的 `_wemm_auto_phase(lib, full, agent_allowed)`：
  wemm_backend off → 静默跳过（零开销）；先 `release_model()+release_reranker()` 把本进程
  bge-m3 让出（文字嵌入已完成，显存互斥）；再进 `index_wemm_library`（增量/全量标志透传、
  Agent 门禁透传）；**任何异常只记日志，页级导航问题绝不波及文字索引成果**。
- `index_wemm_library` 改服务**懒拉起**：首个真正需要渲染的文件才 `ensure_server`，
  无变更轮次零拉起零开销（替代 main() 的预检，CLI/自动两路共用同一逻辑）；拉不起时
  该文件记 extract-failed 终态（带 wemm 签名，下轮自动重试）。
- 文案同步：wemm_status / GUI 明细面板的建库指引改为「跑一次索引自动建页库」。

### 附记（同轮）：「功能有但没接上」专项审计——4 实锤修复
按用户要求做接线完整性审计（config 键读写对照、TODO/占位标记、公开函数引用、高危路径核查）。
结论：config 全部键有读者；源码占位标记全部是有文档背书的刻意设计（mineru-local 本地部署
入口）。实锤并修复：
1. **串行路径 Token 永久锁死（P1）**：`mineru_token_reset()` 只在并行云端段调用——
   `mineru_concurrency=1` 的串行路径在长驻 server 进程里一旦置位 Token 失效标志就永远
   快速失败，用户补好 Key 也要重启进程。改为**按索引轮次复位**（_index_core 开头）：
   轮内首次 Token 错误后其余文件仍快速失败（不烧频控配额），下一轮自动恢复。
2. **配额记账串/并不对称（P2）**：`mineru_quota_add` 只在并行调度方统一入账——串行路径
   真实提交从未计入每日配额，800 页预检提示对串行用户失效。改为**提交点记账**：
   `_mineru_cloud_extract` 在每次真实提交（_pending_add 之后）调 `mineru_quota_add(1, pages)`，
   页数由 _extract_pdf（两分支）与并行 worker（cloud_jobs 已带）透传；续接与缓存命中
   天然不计（不走提交行）。
3. **首跑同步误触发页库全量建库（回归自堵）**：接线审查发现 server.ensure_fresh 的空库
   首跑分支同步调 index_library——若不设防，一次搜索会同步阻塞在数十分钟的页库建库上。
   `index_library` 增 `wemm_sync=True` 参数，首跑分支传 False（页库交下一轮常规索引）。
4. **AI_GUIDE 文档断链**：补 §9——WEMM 自动化行为、GPU 仲裁、诊断顺序、新 MCP 工具速查。
5. 小清理：`gpu_arbiter.WEMM_MIN_VRAM_GB` 成为 wemm_server `--min-vram` 默认值（单一事实
   来源）；index.py 移除已无调用的 `mineru_quota_add` 导入。
测试：extractors 73/73（+1 配额记账三段式：提交计 1 / 续接不计 / 缓存不计）、
wemm_indexer 49/49（+1 wemm_sync=False 跳过接线）；既有 6 个用例的假函数签名随
pages 参数同步更新。
- 测试 +4（wemm_indexer 47）：接线三态（on/off/异常吞并）+ 懒拉起（首轮恰好 1 次、
  无变更零拉起、拉起失败落可重试终态）。**测试纪律新增一条教训**：懒拉起接线后，未打桩
  的既有用例曾尝试真 spawn wemm_server（_wait_health 干等 120s 表现为套件挂死）——
  套件级默认替换 ensure_server 为假实现，杜绝测试拉起真实 GPU 服务。

## 问题42：guiweb——pywebview 桌面壳 + 全库图谱 GUI（与 Flet 版并存）（2026-09-06）

**动机**：demo 验证了「深黑玻璃 + 全库图谱」方向后，评估 GUI 架构：Flet 的
Python↔Flutter 双进程 JSON 桥扛不住逐帧画布动画（力导/涟漪/拖拽跟手），玻璃拟态
表现力受限。选型 **pywebview（单依赖，原生窗口 + WebView2 GPU 合成）+ HTML/JS
前端 + Python 后端原封复用**：Python 3.14 实测可用（.opencode/pywebview_smoke.py），
净增 1 依赖、可卸掉 flet-desktop 数百 MB 运行时。原 Flet GUI（gui/）**原样保留**，
两套并存（独立锁文件 data/guiweb_instance.lock）。

**架构**（guiweb/，约 4600 行）：
- `contracts.md`：前后端唯一契约（28 个 js_api 方法 + 4 类推送事件）
- `bridge.py`：js_api 桥——快照/库管理/索引/检索/设置/诊断/试验台/导出导入。
  复用 store/worker/config_editor/library/extractors/dedup 全部现有模块，
  零逻辑重写；检索结果在**后端集中解析为结构化 JSON**（替换文本协议正则散落）；
  检索/语义边均为纯读路径（不碰 Chroma、不写文件）
- `graph_data.py`：全库图谱纯函数——meta 的 links 字段直接建双链图（同
  resolve_note_relations 规则）、PDF 管线四态从 meta 终态推导（done/queued/
  failed/none，缺失在力学结构上就是"没有子节点"，WEMM/MinerU 是否生效图上
  可验证）、WEMM 页节点（>24 页折叠组节点）、磁盘未识别 PDF、主题族归簇
- `semantic.py`：可选语义边（encode_safe 编码「标题+路径」，阈值≥0.62，
  每节点 top-4 邻居，内存缓存按指纹失效）
- `app.py`：文件字节锁单例守卫（移植）+ pywebview 窗口 + 1 秒 snapshot 推送线程
- `ui/`：深黑玻璃生产前端（demo7 皮肤 + demo1 视图），六视图（图谱/检索/库/
  索引/试验台/诊断/设置）+ 动态岛 + 日志抽屉 + 全部确认门禁（全量红确认/
  移除勾选/导入逐字/云端同意），离线零 CDN
- `wiring_check.py`：接线静态检查——契约方法三向对齐（bridge/mock/app.js）、
  id 引用完整、无外链（离线铁律），纳入回归

**真实 bug 修复（可复现）**：
1. 检索整体提示行被渲染成幽灵结果（Flet gui/widgets 同样存在）：低置信查询的
   「（本次查询整体置信度偏低…）」游离行被当来源行渲染成畸形结果卡。
   guiweb.parse_search_text 归为 notice 条目渲染横幅。
2. mock 数据曾把真实 mineru_api_key 写进仓库文件（get_settings 导出未脱敏）——
   已清除；教训：任何"导出真实配置到代码/文件"的操作必须先过 secret 字段脱敏。

**测试**：tests/test_guiweb.py 45/45（解析 7、格式化 3、主题 7、双链 3、图谱
管线/页节点/折叠/hub/范围 12、语义边 4、接线检查 1、…）；gui/test_gui_store.py、
test_config_editor.py 基线零回归；接线检查全绿；pywebview 真机冒烟
（窗口+WebView2+双向桥）通过，真实数据实跑验证（5 库快照/397 节点图谱/
失败明细/WEMM 探活）。

## 问题43：guiweb 真机首轮反馈修复——设置页空下拉根因 + 原生路径弹窗 + 图谱检索反馈（2026-09-06）

**动机**：用户真机反馈三件事——图谱页检索"没有正确生效"、设置页"几乎全部多选项
不生效"、路径类输入只会粘贴，要求点一下弹原生选择窗口。

**根因定位（computer-use 驱动真机 + 运行日志取证）**：
1. **设置页空下拉（真凶，JS 空数组 truthy）**：`fieldRow` 用 `if (f.choices)`
   分支，而 bridge.get_settings 对无选项字段返回空数组 `[]`——JS 里空数组是
   truthy，导致几乎所有字段（含 bool 开关、文本框）被渲染成**零选项的空 select**，
   整个设置页不可用。修复：`if (f.choices && f.choices.length)`。浏览器与真机
   截图双重复现，Python 侧 apply_updates 回写链路实测无辜（临时副本 roundtrip
   全对：str/int/bool/list 落盘 + 注释保留）。
2. **图谱检索"没生效"**：日志证实检索链路本身通（旧实例 charmap 报错来自修复
   前进程；新实例 36.4s 冷启动成功后 5-6s 常速）——真正的问题是冷启动 30-60s
   界面零反馈，且 `G.searching` 守卫静默吞掉重复点击。修复：检索按钮 busy 态
   （禁用 + "检索中…"）+ 重复点击 toast 提示；顺带修掉 **clearGLit 不清理上一轮
   `.g-conf` 置信度角标**的残留 bug（浏览器复现：两轮连续检索旧角标滞留）。
   另离线核对真实数据 id 匹配：真检索命中（FLUENT 配置.md）与图谱节点
   `lib|rel` 完全对上。
3. **原生路径弹窗**：新增契约方法 `pick_path(mode, start)`（pywebview
   FOLDER_DIALOG/OPEN_DIALOG，start 用输入框现值定位起始目录），接线三处：
   设置页 vault（选文件夹）与 wemm_python（选文件）、添加库弹层路径、提取
   试验台文件路径。浏览器 mock + 接线检查同步。

**测试**：test_guiweb.py 48/48（新增"空 choices 渲染守卫"回归 3 用例——
长度判断存在、裸 if (f.choices) 禁绝、真桥 choices 一律数组）；十件套全绿
（38+15+5+0fail+0fail+73+49+13+23+28+48）+ verify_export_import；接线检查
全绿（pick_path 三向对齐）；浏览器实测两轮连续检索角标不残留、保存链路 toast
正常、busy 态往返正确。

## 问题44：检索置信度语义锚——实测分布重标定 + 分档词 + 工具说明写明读法（2026-09-06）

**动机**：用户质疑"检索分数最低只见 0.50、最高只见 0.70,0.73 和 0.50 看着只差
23%,实际却是'精确命中'vs'完全无关'的差别——这么小的数值差会不会误导 AI agent"。

**实测取证（九组真库查询：3 确定命中 / 3 模糊口语 / 3 库中不存在）**：
- 噪音地板 0.50~0.52（重排器 logit≈0 = "无法判断"，不是"半相关"）；
- 确定命中 top1 也只到 0.73（"GPU 显存仲裁"这条 vault 明确有整章内容的查询
  top1 仅 0.61）；模糊口语型全部 0.50~0.56。
- 结论：有效动态范围只有 0.50~0.73,用户体感完全属实。
- 附带发现：`confidence_drop_threshold=0.40` 的噪音护栏在 sigmoid 分数下**永远
  不触发**（地板 0.50 > 0.40）,形同虚设;warn=0.55 才是日常真正起作用的护栏。
  drop 保留作降级路径兜底（RRF 分场景单路第 2 名 0.375 仍需要它）,注释如实改写。

**修复（只改展示与说明,排序/过滤逻辑零改动）**：
1. **语义锚**：`retriever._format_results` 来源行升级为
   `[置信度 0.73·高相关]`。分档边界取自实测分布：高相关 ≥0.65
   （`CONF_TIER_STRONG`,与实测强命中带 0.65~0.73 对齐）、中相关 ≥warn(0.55)、
   其余弱相关——中/弱分界直接引用 warn 阈值,单一事实来源。
2. **两套 GUI 同步**：flet `_parse_src` 与 guiweb `_RE_CONF` 正则兼容带档位
   后缀（旧格式继续兼容）;配色档位按实测重校准 flet `_conf_color`
   0.75/0.5 → 0.65/0.55、guiweb `scoreBadge` 同步。
3. **工具说明写明读法**：`server.search_knowledge` docstring 增加置信度解读段
   （绝对相似度、区间天然偏窄、噪音区语义、优先按排序引用、弱相关引用前核实）,
   供 LLM 破除百分比直觉;config 模板与 GUI 配置编辑器的阈值注释同步如实。
4. **评估后不做**：sigmoid 温度拉伸——单调变换不改变排序,对 agent 的解读错位
   无实质帮助,只会移动错位位置;真正的解法是语义锚 + 说明。

**测试**：audit 新增 `test_confidence_tier_semantic_anchor`（分档函数边界、
展示层接线、GUI 正则/配色一致性、分档不进排序路径）;test_gui_store 增
`test_parse_src_confidence_with_tier` + 配色档位断言更新;test_guiweb 增
`test_parse_confidence_tier_suffix`。十件套全绿 + verify_export_import。
