# Obsidian RAG — 本地搜自己的笔记

> 在自己电脑上，按意思搜自己的 Obsidian 笔记。笔记不出本机。
>
> **没有独显也能用。** 纯 CPU 可以跑，只是第一次慢一些；看图找书的功能可以一键关掉，不影响文字搜索。

<!-- 截图占位：深色统一，1600x900，录好后直接替换同路径文件即可，无需改 README -->
<p align="center">
  <img src="docs/screenshots/00-hero-guiweb-dark.png" width="860" alt="主页（深色）+ 全部笔记的关系图" />
  <br/>
  <em>图 0 占位：主页（深色），替换 <code>docs/screenshots/00-hero-guiweb-dark.png</code> 即可</em>
</p>

<p align="center">
  <a href="docs/demo/30s-search.mp4">▶ 30 秒演示视频占位（视频方案待定，先占位）</a>
  <br/>
  <em>建议内容：输入一句话 → 找到相关笔记 → 展开关联 → 点一下打开原文</em>
</p>

- [1. 核心功能](#1-核心功能)
- [2. 和别家有什么不一样](#2-和别家有什么不一样)
- [3. 界面预览](#3-界面预览)
- [4. 部署（零基础可用）](#4-部署零基础可用)
- [5. 日常使用](#5-日常使用)
- [6. 常用开关](#6-常用开关)
- [7. FAQ](#7-faq)

---

## 1. 核心功能

### 1.1 多个库 + 精细勾选

技术笔记、读书笔记、工作文档可以分成多个库，各自独立管。每个库可以勾选要管哪些文件夹、排除哪些文件，甚至精确到某一个文件。

- 新文件默认怎么算（跟随 / 纳入 / 排除），可以统一设。
- 文件夹排除了，但其中某一个文件点名要收，可以单独加回来；反过来也行，谁更具体听谁的。
- 在用的筛选规则只有一套，界面上看到的和实际建索引用的是同一套，不会两边不一致。
- 让 AI 改勾选要走两步确认；你自己在界面上点，不用确认码，点错了有提示。
- 实现只有一处：`selection_in` / `selection_out` + `selection_new_files`，判定全在 `library.decide_included`，文件枚举只走 `collect_md_files` 一个漏斗。

<p align="center">
  <img src="docs/screenshots/01-library-selection.png" width="820" alt="库管理 + 文件夹/文件勾选（深色）" />
  <br/><em>图 1 占位：库列表 + 勾选树（深色），替换 <code>docs/screenshots/01-library-selection.png</code></em>
</p>

### 1.2 多种文件能读

Markdown、纯文本、PDF、Word 默认都能进库。文字版 PDF 直接读（`pymupdf4llm`）；带图片的 PDF（扫描件、PPT 混排的课件）整本一起处理，不会只收文字页、丢掉图片页。

- 没开图片识别时，图片版会先诚实记下“这次没收”，开了之后下轮自动补，不用全部重建。
- 云端识别（MinerU cloud）和本机识别（MinerU local）两种方式，本机方式不花钱、不出内网（见第 4 章可选部分）。

<p align="center">
  <img src="docs/screenshots/02-extract-scan-status.png" width="820" alt="哪些文件没生效及原因（深色）" />
  <br/><em>图 2 占位：没生效的文件和原因（深色），替换 <code>docs/screenshots/02-extract-scan-status.png</code></em>
</p>

### 1.3 按意思找得准

不只看关键词有没有出现（BM25 + jieba 中文分词），也看意思像不像（BGE-M3 向量），两路名次融合（RRF，默认等权）后再用重排器挑出最好的几段（候选池 50）。命中小块时会自动带回所在小节全文。把握小的标出来，太弱的直接不推，宁可少推一条。

> 实测（2026-09-09，真库 `agents`，134 块）：查 `agent skill` 取 3 条，首条 `[来源] agents/ui-agent.md (## 工具闸门) [块 2/4] [置信度 0.20·中相关]`，后两条 0.06 / 0.04 标弱相关。首次跑要加载模型（约数十秒，cuda），之后毫秒级。

### 1.4 知识有人管

- 每个文件收没收、为什么没收，在「文件生效明细」里逐条看得到，点一行就能打开原文；命令行可用 `index_failures` 查同一份原因。
- 笔记之间的双向链接能顺着查（`note_relations`）：搜到一条，可以顺手展开和它相关的笔记。
- 能找内容几乎一样的重复笔记（`find_duplicates`，MinHash + LSH），只给建议，不自动删。
- 换电脑搬家有导出/导入（带校验），不用重新算一遍。
- 定期自动清理没用的缓存和已删库的残留；哪一轮没跑顺就跳过清理，不硬来。

<p align="center">
  <img src="docs/screenshots/03-file-detail-panel.png" width="820" alt="文件生效明细（深色）" />
  <br/><em>图 3 占位：文件生效明细（深色），替换 <code>docs/screenshots/03-file-detail-panel.png</code></em>
</p>

### 1.5 看图找书（可关）

PDF 可以按页面找（WEMM 页向量，服务 `:9101`）：记得某页长什么样、但不记得写了什么字时好用。文字建好后页面库自动跟上，不用手动跑。

- 看图服务用时才拉起，不用时自己退出（闲 5 分钟卸显存、30 分钟退出），不占资源。
- 同一时间只让一个大模型占显卡（显存仲裁：拉 WEMM 前要等 ≥5.5GB 空闲；搜文字优先抢占），探测失败也不挡路；没显卡的机器也能跑文字搜索，这功能关掉就行。

<p align="center">
  <img src="docs/screenshots/04-wemm-navigate.png" width="820" alt="按页面找书（深色）" />
  <br/><em>图 4 占位：按页找 + 页面预览（深色），替换 <code>docs/screenshots/04-wemm-navigate.png</code></em>
</p>

### 1.6 人和 AI 分权 + 两个桌面界面

AI 触发的整理默认只碰纯文本和你批准过的格式（`agent_formats`），没批准的文件先冻住（内容留着，不删），批准一次长期有效，随时可收回。

桌面界面有两个，新版（pywebview 壳 + 深黑玻璃 + 全库关系图，`python guiweb/app.py`，推荐）和经典版（Flet，`python gui/app.py`），后台共用同一套数据。界面只是观察者，不加载模型、不直写数据；经典版必须用 `gui/stop.py` 关闭。

<p align="center">
  <img src="docs/screenshots/05-guiweb-graph.png" width="820" alt="全库关系图（深色）" />
  <br/><em>图 5 占位：全库关系图（深色），替换 <code>docs/screenshots/05-guiweb-graph.png</code></em>
</p>

---

## 2. 和别家有什么不一样

| 维度 | 本项目 | 常见替代 |
|---|---|---|
| 隐私 | 笔记和图片都在本机处理 | 要上传原文或数据到云 |
| 诚实 | 图片页认不出就直说“这次没收”，不拿半份结果糊弄 | 文字页收了、图片页静默丢掉 |
| 省心 | 开了识别后，以前没收的下轮自动补，不用推倒重来 | 改一次设置就要全量重建 |
| 管得细 | 能管到某一个文件，文件点名可穿透文件夹排除 | 只有全局黑白名单 |
| AI 安全 | AI 改范围要两步确认；没批准的格式先冻住 | AI 能直接大改 |
| 界面 | 界面只看不碰，关窗口不留残留进程 | 界面里直跑模型，关了还占资源 |

---

## 3. 界面预览

规范：**只截深色**，建议 1600x900。以下全是占位，截图存同名路径即可。

| 编号 | 内容 | 路径 |
|---|---|---|
| 图 0 | 主页（深色）+ 全库关系图 | `docs/screenshots/00-hero-guiweb-dark.png` |
| 图 1 | 库管理 + 勾选树 | `docs/screenshots/01-library-selection.png` |
| 图 2 | 没生效的文件和原因 | `docs/screenshots/02-extract-scan-status.png` |
| 图 3 | 文件生效明细 | `docs/screenshots/03-file-detail-panel.png` |
| 图 4 | 按页找 + 页面预览 | `docs/screenshots/04-wemm-navigate.png` |
| 图 5 | 全库关系图 | `docs/screenshots/05-guiweb-graph.png` |
| 图 6 | 设置页 | `docs/screenshots/06-settings.png` |
| 图 7 | 经典版界面 | `docs/screenshots/07-flet-classic.png` |

<p align="center">
  <img src="docs/screenshots/06-settings.png" width="820" alt="设置页（深色）" />
  <br/><em>图 6 占位：设置页（深色），替换 <code>docs/screenshots/06-settings.png</code></em>
  <br/><br/>
  <img src="docs/screenshots/07-flet-classic.png" width="820" alt="经典版（深色）" />
  <br/><em>图 7 占位：经典版（深色），替换 <code>docs/screenshots/07-flet-classic.png</code></em>
</p>

短视频（**方案待定**：放仓库还是挂外链以后再定，先占位）：

1. `docs/demo/30s-search.mp4` — 搜一句话 → 展开关联 → 打开原文。
2. `docs/demo/60s-library-select.mp4` — 新建库 → 勾选/排除 → 建索引 → 看生效明细。

---

## 4. 部署（零基础可用）

> 主流程只到“文字能搜”。图片识别、看图找书、接 AI 都在后面“可选”里，新人可跳过。
> 以 Windows + PowerShell 为准；Linux 把 `.venv\Scripts\` 换成 `.venv/bin/` 即可。

### 4.1 准备

- Windows 10/11，Python 3.14，磁盘至少留 4GB（第一次搜会自动下载用到的模型，要等几分钟）。
- 一个放笔记的文件夹。本 README 用自带的 `demo-vault/` 做例子，你换成自己的 Vault 路径就行。

```powershell
$env:PYTHONIOENCODING = "utf-8"
python --version  # 期望看到 3.14
```

### 4.2 下载

```powershell
git clone <仓库地址> obsidian-rag
cd obsidian-rag
```

### 4.3 装环境

```powershell
python -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
# 有 N 卡用这行装加速版（不要装 cu130，会被系统拦截）:
.venv\Scripts\pip install torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
# 没 N 卡 / 只用 CPU，用这行:
# .venv\Scripts\pip install torch==2.11.0
```

看到没有红色报错就算成。

### 4.4 注册演示库

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python library.py add ".\demo-vault" --name 演示库
.venv\Scripts\python library.py list
```

想加自己的库，把路径换成你的 Vault 路径再跑一遍 `add` 就行。

### 4.5 建索引

```powershell
.venv\Scripts\python index.py --library 演示库
```

屏幕会走完扫描 → 读文件 → 建索引。图片版 PDF 没开识别时会记“这次没收”，是正常的，后面开了会自动补。

### 4.6 搜一下验证

```powershell
.venv\Scripts\python -c "from retriever import hybrid_search; print(hybrid_search('演示库里的一个词', top_k=3))"
```

第一次要下载模型（几十秒到几分钟），能看到 `[来源] … [置信度 …]` + 正文就是成了。真实例子见 1.3 节实测。

### 4.7 打开界面（二选一）

新版（推荐）：

```powershell
.venv\Scripts\python guiweb/app.py
```

经典版（启动和关闭要用专用方式，否则关不干净）：

```powershell
Start-Process -FilePath "$PWD\.venv\Scripts\pythonw.exe" -ArgumentList "gui\app.py" -WorkingDirectory "$PWD"
# 关闭：
.venv\Scripts\python gui/stop.py
```

### 4.8 可选：图片识别

- 云端方式：去官网注册拿一串 Key → 在设置页打开“扫描件识别”并粘贴 Key。以前没收的图片版下轮自动补。
- 本机方式：不出内网、不花钱，但要另装环境、下载十几 GB 的模型，适合有显卡且笔记多的。步骤较长，新人先跳过，需要时再看 `AI_GUIDE.md`。

### 4.9 可选：看图找书的开关

默认开着。有显卡体验最好；没显卡或觉得慢，在设置页关掉就行，不影响文字搜索。

### 4.10 可选：让 AI 来搜（通用模板）

本项目是一个标准输入输出的检索服务，三件套讲清即可：**用哪个 Python 启动（command）+ 启动哪个文件（args）+ 仓库在哪（cwd）**。下面是通用样子，各家 AI 工具照着填：

```json
{
  "type": "stdio",
  "command": "C:\\path\\to\\obsidian-rag\\.venv\\Scripts\\python.exe",
  "args": ["server.py"],
  "cwd": "C:\\path\\to\\obsidian-rag"
}
```

接好后 AI 就能用 9 个工具：搜知识（`search_knowledge`）、看有哪些库（`list_libraries`）、重建索引（`reindex_knowledge`）、查关联笔记（`note_relations`）、按页找书（`navigate_knowledge`）、读全文（`read_document`）、找重复（`find_duplicates`）、看失败原因（`index_failures`）、看图片服务状态（`wemm_status`）。改了设置后把连接重连一下即生效。

---

## 5. 日常使用

```powershell
# 所有库都建一遍
.venv\Scripts\python index.py
# 看哪些没生效
.venv\Scripts\python -c "import server; print(server.index_failures())"
# 换电脑搬家（不用重新算）
.venv\Scripts\python export.py --library 演示库 --output data/export/演示库.zip
.venv\Scripts\python import.py --yes data/export/演示库.zip
```

界面里：改勾选 → 开始建索引 → 去生效明细核对 → 搜一条展开关联看看。

---

## 6. 常用开关

都在设置页改，和直接改 `data/config.json` 是同一份，后保存的生效。

| 开关（设置页中文名） | 默认 | 说明 |
|---|---|---|
| 索引格式 | md,pdf,docx | 哪些后缀进库 |
| AI 可用格式 | 纯文本类 | AI 触发时能碰哪些，没批准的先冻住 |
| 图片识别 | 关 | 关 / 云端 / 本机 |
| 看图找书 | 开 | 没显卡就关掉 |
| 新文件默认 | 跟随 | 新文件算纳入还是排除 |
| 精排 | 开 | 关掉搜得快一点、准头降一点 |

---

## 7. FAQ

1. **没显卡能用吗？** 能。装 CPU 版就行，第一次慢，之后正常用；看图找书关掉。
2. **图片版 PDF 怎么是空的？** 默认没开识别，会先记“这次没收”。开了之后下轮自动补，去生效明细看原因。
3. **AI 说改不动勾选？** 正常的，AI 改范围要两步确认。你自己在界面点一下就行。
4. **改了设置要重建吗？** 文字相关的改完下轮自动补；只有换模型、改切块大小这种才要全量重建，界面会提示。
5. **界面关不干净？** 经典版必须用 `gui/stop.py` 关，别直接杀进程；新版直接关窗口就行。

---

*英文版见 `README.en.md`。配图与视频为占位，录好后替换 `docs/screenshots/` 与 `docs/demo/` 同名文件即可。*
