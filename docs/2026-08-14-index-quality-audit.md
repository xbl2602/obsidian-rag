# 索引质量与致命问题审计（2026-08-14）

> 状态：**审计完成，F1–F20 已全部修复并回归**（见 §7 修复台账）。
> 本文记录**已由执行验证**的结论，与**尚未验证**的线索严格分开。
> 验证环境：Linux 虚拟机，Python 3.14.4，无 GPU、无依赖安装。目标工况为 Windows + GPU，
> 因此凡涉及 Windows / CUDA / GUI 的结论均标注为「无法在本机验证」。
>
> ⚠️ **修复引入 `META_VERSION = 6`，需要一次全量重建**（切块与嵌入文本都变了）。
> 重建前请先读 §7 末尾的「上线前须知」。

---

## 0. 一句话结论

> **2026-08-13 的 v5 检索大改（块 1500→600、候选池 10→50、small-to-big、RRF、jieba、HyDE）
> 在真实运行中基本没有生效。** 首跑之后每一次启动，`config.json` 都把 `chunk_char_limit`
> 改回 1500、`rerank_candidates` 改回 10；HyDE 整个特性没有任何调用方；
> `fusion_*_weight` 两个配置项是死键；而 jieba 根本不在 `requirements.txt` 里，
> 全新部署一检索就报错。

需要立刻人工确认的一件事：**在 Windows 机器上打开 `data/config.json`，看 `chunk_char_limit`
到底是 600 还是 1500。** 这决定了现有索引是 v5 设计的小块，还是 v4 的大块。

---

## 1. 验证方法与环境约束

### 1.1 为什么不能直接跑真实环境

| 项目 | 数值 |
|---|---|
| 可用磁盘 | 9.5 GB |
| GPU | 无 |
| 已安装依赖 | 无（chromadb / mcp / sentence-transformers / flet 均未装） |
| torch wheel | 527 MB |
| bge-m3 模型 | ~2.3 GB |
| bge-reranker-v2-m3 | ~1.1 GB |

装齐真实栈峰值逼近磁盘上限，且本机无 GPU，跑出来的性能/质量结论也不可迁移到目标机。

### 1.2 采用的方案：零依赖 stub 沙箱

把仓库整体复制到 scratchpad（不含 `.git` / `data`），再注入 `chromadb` / `mcp` 的
最小 stub 模块，即可**真实执行仓库代码**（切块、分词、BM25、融合、配置、锁、meta 全是纯逻辑）。

复现方式：

```python
import sys, os
S = "<scratchpad>"
sys.path.insert(0, S + "/stubs")     # chromadb / mcp 的最小 stub
sys.path.insert(0, S + "/harness")   # 仓库副本
os.chdir(S + "/harness")
import index, retriever, library     # 零重依赖导入成功
```

stub 覆盖：`chromadb.PersistentClient` / `Collection`（add/get/query/delete/count/upsert）、
`mcp.server.MCPServer`（tool 装饰器 + run）。`jieba` 另用 types.ModuleType 临时注入以跑通
下游逻辑——**注意这只是为了测试后续管线，jieba 缺失本身就是 F1 号问题**。

### 1.3 分级验证计划（给目标机用）

| 层级 | 内容 | 磁盘 | 建议 |
|---|---|---|---|
| **T0** | 切块/分词/BM25/融合/配置/锁/meta — 纯逻辑 | 0 MB | ✅ 已完成，本文全部结论出自这里 |
| **T1** | 加 chromadb + 假向量：真实读写、增量、孤儿块、删除、导入导出往返 | ~200 MB | ✅ 推荐下一步（chromadb 不依赖 torch） |
| **T2** | 加 MiniLM(90MB)：encode 路径、归一化、hnsw:space | ~1.5 GB | 可选；质量结论不可迁移，管线 bug 可暴露 |
| **T3** | 真实 bge-m3 + reranker | ~5–6 GB | ❌ 不建议在本 VM，峰值逼近磁盘上限 |

**绝不在本 VM 验证**：CUDA 降级状态机、Windows `msvcrt` 锁、flet GUI（未装且无显示）、
`gui/stop.py` 的 taskkill/WMI 逻辑。

---

## 2. 已执行验证确认的问题

按严重度排序。每条都给出复现方式与实测输出。

### 🔴 F1 — jieba 不在 requirements.txt，全新部署检索必然失败

`retriever.py:71` 在 `tokenize()` 内 `import jieba`，无 try/except、无降级。
而 `requirements.txt` 只有 4 个包：

```
chromadb==1.5.9  mcp==2.0.0  sentence-transformers==5.6.1  flet==0.86.5
```

实测：`ImportError: No module named 'jieba'`。

**传播路径**：`hybrid_search` → `get_bm25` → `BM25.__init__` → `tokenize` → ImportError
→ 被 `server.py:184` 的宽 `except Exception` 吞掉 → 返回
`（检索失败：No module named 'jieba'）`。

**影响**：任何按 `AI_GUIDE.md §2.2`（`pip install -r requirements.txt`）部署的机器，
**检索 100% 不可用**，且 §3.4 的冒烟测试 `hybrid_search('免费证书', top_k=3)` 必挂。
现有开发机能跑，只是因为 jieba 是手动装进去的。

**修复**：`requirements.txt` 加 `jieba`；并给 `tokenize` 加 ImportError 降级（退回纯 2-gram）。

---

### 🔴 F2 — 首跑与第二跑配置不一致，v5 大改被静默回退

`config.py` 里有两份互相矛盾的默认值：

| 键 | `DEFAULTS`（18–70 行） | `CONFIG_TEMPLATE`（72–234 行，首跑写盘的那份） |
|---|---|---|
| `chunk_char_limit` | **600** | **1500** |
| `rerank_candidates` | **50** | **10** |

`load_config()` 的逻辑是：文件不存在 → 写模板 → **但返回的是 `DEFAULTS`**；
文件存在 → 读文件。于是首跑用 600/50，第二跑起用 1500/10。

实测输出：

```
=== 首跑（无 data/config.json）===
  chunk_char_limit  = 600     rerank_candidates = 50
=== 第二跑（读首跑写下的 config.json）===
  chunk_char_limit  = 1500    rerank_candidates = 10
=== DIVERGENCE ===
  chunk_char_limit:  run1=600 -> run2=1500
  rerank_candidates: run1=50  -> run2=10
```

**影响**：
1. TASK_LOG 问题19 记录的 v5 全部收益（小块语义纯净、候选池扩大）**在第二次运行起就没了**。
2. `small_to_big` 因为**不在模板里**，反而一直取 DEFAULTS 的 `True` —— 于是得到最坏组合：
   **1500 字的大块 + 专为 600 字小块设计的父节回填**，父节在大块之上再叠一层，token 白烧。
3. `META_VERSION=5` 的注释写着「chunk_char_limit 1500→600」，但索引实际按 1500 建，
   却被盖上 `_version=5` 的章 —— 版本号校验永远通过，索引**永久停在 1500**。
4. 模板里那段注释还写着「建议值 1200~2000」，与 v5 设计方向相反，会把人带偏。

#### 设计意图澄清（2026-08-14 补）

`config.json` 的设计目的是**给不同工况快速调参的可编辑面**，不是 `DEFAULTS` 的镜像。
这个前提是对的，本条问题**不是**「config.json 覆盖了 DEFAULTS」—— 那正是它该做的事。

问题在另外两点，都与「可调面」这个目的**同向**、而非冲突：

**(a) 出厂种子值与代码自述的意图矛盾，且没人调过它。**
run1→run2 的变化不是任何人在调参，是系统自己把 v5 调回了 v4。
而作者意图是无歧义的：`META_VERSION = 5` 的注释写着
「v5: chunk_char_limit 1500→600（小块语义纯净；配合 small-to-big 回填）」，
TASK_LOG 问题19 也记着「块 1500→600 + small-to-big」。
2026-08-13 改了 `DEFAULTS` 和 `META_VERSION`，漏改了模板。

**更糟的是模板里的注释也停在 v4**：`"chunk_char_limit": 1500` 上方写着
「建议值 1200~2000；修改后务必 --full 重建对比效果」。
既然这个文件的用途就是给人调参，**它现在同时提供了错的现值和错的建议区间**——
调参面在误导调参的人。

**(b) 有 5 个键永远到不了这个可调面。**
`load_config()` 只在文件**不存在**时写模板（见 293-299 行），
**既有的 config.json 永远不会获得新增键**。
所以 `small_to_big` 和 4 个 `hyde_*` 键（F12）在任何一台已运行过的机器上
**都不可能出现在 config.json 里**，只能静默取 `DEFAULTS`。
一个调不到的键，等于这个「快速调参」目的对它完全失效。

**(c) 新证据：每一份分发出去的包都必然踩中。**
`export.py:127-137` 的打包清单是
`payload.jsonl.gz` / `index_meta.json` / `vault/*` / `manifest.json` / `AI_GUIDE.md`
—— **不含 `config.json`**。
所以按 `AI_GUIDE.md` 部署的每一台新机器都是「无 config.json」状态：
首跑写模板 → 第二跑起 1500/10。
开发机因为 config.json 是历史遗留（且可能手工改过）也许没事，
**但所有分发副本一定中招**。

#### 修正后的修复方案（保留可调面设计）

~~原方案「让 CONFIG_TEMPLATE 由 DEFAULTS 生成」是错的~~ —— 那会毁掉注释，
而带注释的可调模板正是这个文件的价值。改为：

1. **只改模板里的数值与过时注释**，让它等于 `DEFAULTS`（600 / 50），
   并把「建议值 1200~2000」改写成符合 v5 小块设计的说明。手写的注释文档全部保留。
2. **把缺的 5 个键补进模板**（以及 `config_editor.GROUPS`，见 F12），
   让它们真正可调 —— 这是在兑现这个文件的设计目的，不是削弱它。
3. **加一条廉价的一致性断言**（纯逻辑、无依赖，可进 tests）：
   `set(模板键) == set(DEFAULTS键)` 且对应值相等。
   只约束「出厂种子」，完全不限制用户此后怎么改 config.json。
4. 顺带考虑：给既有 config.json 做**缺键补写**（读到文件后，把 `DEFAULTS` 里有
   而文件里没有的键追加进去并加注释），否则以后每次加新键都会重演 (b)。

---

### 🔴 F3 — 值里出现转义引号 → 整个 config.json 静默失效

`config.py:249` 的注释剥离器：

```python
if c == '"' and prev_c != "\\\\":     # 拿 1 字符的 prev_c 去比 2 字符的字符串
```

`prev_c` 恒不等于 `"\\\\"`，转义处理是死代码。于是字符串里的 `\"` 被当成字符串结束，
后面的 `//` 被当注释整行砍掉。

实测：

```
输入 : "truncate_mark": "say \" hi // not a comment",  "default_top_k": 9
输出 : "truncate_mark": "say \" hi          <- 行尾被吃掉，JSON 断裂
json.loads -> JSONDecodeError
load_config 后 default_top_k = 5（文件里写的是 9）
```

**可达路径不是理论上的**：`truncate_mark` 是 GUI 设置页的可编辑文本框，
`config_editor._value_to_json` 用 `json.dumps` 序列化 —— 用户只要在里面**打一个双引号**，
写回的就是 `\"`，下次启动**全部配置静默回退默认值**，只有 stderr 一行提示。

---

### 🟠 F4 — 配置加载零类型校验

`load_config()` 只判断键名在不在 `DEFAULTS`，不校验类型/范围。实测：

```
chunk_char_limit  = '六百'        (str)    -> 后续切块比大小时 TypeError
rerank_enabled    = 'false'       (str)    -> bool('false') is True，重排照开
exclude_dirs      = 'not-a-list'  (str)    -> 会被当字符串逐字符迭代
default_top_k     = -5            (int)    -> 负数照收
```

配合 F3（手改配置极易触发解析失败），配置层整体缺乏防呆。

---

### 🔴 F5 — `os.kill(pid, 0)` 在 Windows 上是「终止进程」，不是「探测存活」

Python 官方文档明确：Windows 上 `os.kill` 除 `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` 外，
**任何 sig 值都会无条件终止目标进程**（CPython 实现为 `OpenProcess` + `TerminateProcess(handle, sig)`）。
`sig=0` 也不例外。

仓库有**两处**共用这个「存活探测」：

- `index.py:74` `_pid_alive()`
- `singleton.py:24` `pid_alive()`（docstring 自述「与 index.py 同一策略」）

三个调用点，在 Windows 上的后果分别是：

| 调用点 | 触发时机 | Windows 后果 |
|---|---|---|
| `singleton.py:56` `acquire_singleton` | 每次 server 启动且 PID 文件有存活记录 | **杀掉正在服务的那个 server**，然后自己也 `sys.exit(0)` → 两个都没了，MCP 直接不可用 |
| `index.py:839` 锁等待超时 | 写锁等待 >60s 后 | **在 Chroma 写到一半时杀掉持锁进程**（TerminateProcess 无清理、锁不释放）→ 索引损坏风险 |
| `gui/store.py:162` `index_busy()` | **GUI 主循环每 1 秒一次**（`gui/app.py:241` `while True` + `asyncio_sleep_1s`，300 行调用） | 索引一开始跑就被 GUI 杀掉 |

第三条的前提已确认成立：`index.py:113` `base.setdefault("pid", os.getpid())`
—— 进度文件确实带 `pid`，`index_busy()` 的 `if pid and _pid_alive(pid)` 会真的走到 `os.kill`。

另外 `tests/server_singleton_test.py:74` 的 `assert pid_alive(os.getpid())`
**在 Windows 上会把测试进程自己杀掉**。

**旁证**：`gui/app.py:47` 已有注释「实测 os.kill 对 pythonw 误判"已死"导致重复实例」——
说明作者在 Windows 上确实撞过 `os.kill` 的怪异行为，但诊断成了「误判已死」（`OpenProcess`
失败抛 OSError 的那条分支），只在 GUI 里绕道 taskkill/WMI，**没有回头修 `singleton.py`
和 `index.py`**。

⚠️ **无法在本 VM 验证**（Linux 上 `os.kill(pid,0)` 是良性探测，正因如此本机测试全绿）。
但两条分支都是坏的，无论命中哪条：
`OpenProcess` 成功 → 目标被杀且返回 True（"存活"）；`OpenProcess` 失败 → OSError → 返回 False（"已死"）。
**它在 Windows 上不是一个存活探测函数。**

**修复**：Windows 分支改用 `OpenProcess(SYNCHRONIZE|PROCESS_QUERY_LIMITED_INFORMATION)` +
`WaitForSingleObject` 判断，或直接用 `psutil.pid_exists()`。

---

### 🟠 F6 — 重排开启时，展示顺序与 `[置信度]` 数值来自两套体系

`hybrid_search` 里：排序用 reranker 的分数（`ranked_pool`），
但 `[置信度]` 取的是 RRF 融合分（`combined[cid] / (2/(k+1))`）。两者无关。

实测（用一个把顺序反转的假 reranker）：

```
[来源] kb/f4.md ... [置信度 0.21]
[来源] kb/f3.md ... [置信度 0.25]
[来源] kb/f2.md ... [置信度 0.30]
[来源] kb/f1.md ... [置信度 0.38]
[来源] kb/f0.md ... [置信度 1.00]
```

一份声称「按相关度降序」的结果，置信度却**单调递增**。
`rerank_enabled` 默认 `True`，所以这是**默认配置下的常态**。
消费这份输出的 LLM 会被数字直接误导。

附带一个刻度问题：RRF(k=2) 上限 `2/(k+1)=0.667`，
**dense 排第一但 BM25 无命中的块，置信度恰好 = 0.50**；两路都第一才 1.00。
纯语义命中的好块天然只能拿 0.5 分。

---

### 🟠 F7 — BM25 缓存在「块数不变」的编辑下永不失效

`get_bm25()` 用 `collection.count()` 做缓存指纹。但**改一段文字**通常不改变块数。

实测：

```
缓存 doc[0] 编辑前: 文档0 火箭发动机 推力 参数
（外部进程重建索引，内容换成「蛋糕 烘焙」，块数不变）
缓存 doc[0] 编辑后: 文档0 火箭发动机 推力 参数   <- 没刷新
count() 不变: 5
```

docstring 自称「检索前先比 count，不一致说明索引被外部进程重建过，立即重建缓存，
防混合新旧结果」—— 但 count 相等恰恰是**编辑场景的常态**。

**放大因素**：GUI 是把 `index.py` 当**独立子进程**拉起的（`gui/worker.py:40`），
所以 server 进程里的 `reset_bm25_index()` 根本不会被调用。
长驻的 MCP server 会一直用旧文本做关键词召回，dense 侧却是新的 —— 两路错位。

**修复**：缓存指纹改为 meta 文件的 mtime，或 `count + 抽样 id 的文档 hash`。

---

### 🟠 F8 — 代码块里的 `#` 被当成标题，且**污染后续真实小节的 heading path**

`split_by_headings` 不识别 ``` 围栏。实测输入（外层用 ~~~ 包裹以免与内层围栏冲突）：

~~~markdown
# 真标题
正文一

```python
# 这是注释不是标题
def f(): pass
## 也不是标题
```

## 真的二级标题
正文二
~~~

切出来 4 节（应为 3），且：

```
heading_path='真标题'
heading_path='这是注释不是标题'                    <- 伪标题
heading_path='这是注释不是标题 / 也不是标题'          <- 伪标题嵌套
heading_path='这是注释不是标题 / 真的二级标题'         <- 真小节的路径被污染
```

**这才是关键**：`index.py:1018` 把 heading path 拼进**送去嵌入的文本**：

```python
new_texts.append((hp + "\n" if hp else "") + chunk_text)
```

所以一个 Python 注释会作为「父标题」混进真实小节的向量里。这不是计数问题，是**向量污染**。

在本仓库自带的 md 上实测：95 节中有 10 处围栏内伪标题（约 11% 膨胀，
`TASK_LOG.md` 4 处、`AI_GUIDE.md` 6 处）。
⚠️ 这是拿仓库文档做的代理测量，**真实 Obsidian vault 的比例未知**，需在目标机复测。

---

### 🟠 F9 — 裸 `[[wikilink]]` 被整个删除

`clean_wikilinks`（`index.py:644-658`）实测：

```
'见 [[火箭发动机]] 一节'         -> '见  一节'        <- 词没了
'见 [[火箭发动机|发动机]] 一节'    -> '见 发动机 一节'    <- 别名保留
'![[图片.png]]'                -> ''               <- 嵌入图片，删掉合理
```

Obsidian 里**裸链接是主流写法**，而链接目标恰恰是笔记里最高信号的概念词。
现在这些词从嵌入文本和 BM25 词表里被同时抹掉。

**修复**：`[[目标]]` → 保留 `目标`；`[[目标|别名]]` → 保留 `别名`（现状已对）；
仅 `![[...]]` 嵌入语法删除。

---

### 🟡 F10 — `fusion_dense_weight` / `fusion_bm25_weight` 是死键

AST 静态分析确认：

```
dense_weight: 赋值@390, 读取@389    <- 389 是 `if dense_weight is None:` 的守卫，在赋值之前
bm25_weight : 赋值@392, 读取@391    <- 同上
hybrid_search 中「赋值后从未被读」的变量: rerank_failures, dense_weight, bm25_weight
```

改成 RRF 后这两个值再没被用过（函数 docstring 也承认「保留仅为 API 兼容」）。
但它们仍然出现在 **GUI 设置页**、分组标题写着「检索与格式化（实时生效）」
（`gui/config_editor.py:37`）—— 用户调它们**完全没有任何效果**。

---

### 🟡 F11 — HyDE 整个特性没有任何调用方

`hybrid_search_hyde` / `hyde_generate` 在 `retriever.py` 之外**零引用**：
`server.py:27` 只 `from retriever import hybrid_search, reset_bm25_index`，
`gui/app.py:550` 也是 `hybrid_search`。

即使把 `hyde_enabled` 设成 `true` 也不会有任何事发生（何况这个键还不在 `CONFIG_TEMPLATE` 里，
见 F12）。TASK_LOG 问题19 记录的 HyDE 是**未接线的死代码**。

> 附注：真接上也有代价 —— `hybrid_search_hyde` 会先跑一次完整 `hybrid_search`，
> 再调 `_top1_confidence`（内部**又一次**完整 `hybrid_search`），触发时还有第三次。
> 即「开启 HyDE = 每次检索至少 2 倍开销」。

---

### 🟡 F12 — `CONFIG_TEMPLATE` 与 GUI 设置页各缺同样的 5 个键

`DEFAULTS` 有 36 个键，`CONFIG_TEMPLATE` 缺 5 个，`config_editor.ALL_KEYS`（31 个）缺**同样**5 个：

```
small_to_big, hyde_enabled, hyde_llm_url, hyde_llm_model, hyde_min_confidence
```

即 2026-08-13 新增的特性只改了 `DEFAULTS`，没同步模板和 GUI。
后果：用户既无法在 config.json 里看到这些键，也无法在 GUI 里调。

---

### 🟡 F13 — `index_vault` 吞掉 `incremental` / `full` 参数

```python
def index_vault(vault, incremental=True, full=False):
    return _index_core(vault, COLLECTION_NAME, INDEX_META, EXCLUDE_DIRS, STRUCTURE_FILES,
                       EXCLUDE_PATTERNS, ["md"], CFG["chunk_char_limit"],
                       CFG["short_doc_char_limit"], library_label="")
                       # incremental / full 两个参数都没往下传
```

对比 `index_library` 结尾正确地带了 `incremental=incremental, full=full)`。

所以 `python index.py --vault X --full` **静默降级成增量索引**。

**爆炸半径（已逐一追查调用方，比初判要小）**：
- GUI「全量重建」按钮 —— **安全**。`gui/worker.py:40` 拉的是子进程
  `python index.py --library <名> --full` → 走 `index_library`，转发正确。
- `export.py` —— **安全**。只 import 了 `index_library`。
- 仅 legacy `--vault` CLI 路径受影响。

TASK_LOG:928 记录了修 `index_library` 的转发，孪生的 `index_vault` 被漏掉了。

---

### 🟡 F14 — `kb_stale` 从不检查 `META_VERSION`，版本号提升不会触发重建

全仓库 `_version` 只出现三处：写入（`index.py:674`）、
校验（`index.py:925`，且在 `_index_core` 内部、`if not full` 分支）、注释。
`kb_stale` 里没有。

而 `server.ensure_fresh()` 是靠 `kb_stale` 决定要不要重建的。
于是：**改了 `META_VERSION` 之后，只要没有文件变动，索引就一直是旧版本切法**，
直到碰巧有文件改动才顺带触发全量重建。版本号的「强制重建」语义没有兑现。

（有自愈性质，所以列黄不列红：一旦任何文件变动就会被 `_index_core` 抓到并全量重建。）

---

### 🟡 F15 — 中文库名派生出的 collection 名会互撞

`collection_for()` 把非 `[a-zA-Z0-9._-]` 全替换成 `_`。实测：

```
'火箭笔记' -> 'kb_____'     '工程日志' -> 'kb_____'     COLLIDE=True
'笔记'    -> 'kb___'       '日志'    -> 'kb___'       COLLIDE=True
```

**任意两个等长的纯中文库名派生出完全相同的 collection 名。**

好消息：`add_library:163` 有显式撞名校验，会拒绝并提示，不会静默共用同一 collection。
所以后果是**注册第二个中文库时被硬性挡住**（需手动 `--set collection=`），不是数据混淆。

⚠️ **相关但未能验证**：这些名字（`kb_____`）以下划线结尾，
违反 Chroma 经典规则 `^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$`。
我从 chromadb 1.5.9 的 wheel 里确认了该规则存在于 `chromadb/api/segment.py:108`
（`check_index_name`），**但 1.5.9 的 `PersistentClient` 走的是 Rust 后端
（`chromadb.api.rust.RustBindingsAPI`），`check_index_name` 仅被 `segment.py` 自己调用。**
Rust 侧的校验规则读不到，因此**无法判定纯中文库名是否会被 Chroma 直接拒绝**。
需在目标机实测一次 `client.get_or_create_collection("kb_____")`。

> 当前注册的库 `Obsidian Vault` → `kb_obsidian_vault`，合法，不受影响。

---

### 🟡 F16 — `--create` 导入会造出「空目录」，下一次检索就把刚导入的索引清空

`import.py:248` 在 `--create` 路径上主动建了目标目录：

```python
lib_path = args.path or str((DATA_DIR / "imported" / lib_name).resolve())
Path(lib_path).mkdir(parents=True, exist_ok=True)
```

而 `kb_stale` 对「路径不存在」和「路径存在但空」的处理是**两回事**：

```
空目录       -> True {'changed':0,'added':0,'removed':1}          <- 无 missing 标志
路径不存在    -> True {'changed':0,'added':0,'removed':0,'missing':True}
```

`server.ensure_fresh()` 只在 `stats.get("missing")` 时跳过。
**空目录不带 missing → 判定「文件全被删了」→ 触发索引 → 清空刚导入的数据。**

`import.py:291` 自己印了警告（「否则下次检索前自动同步会判定'文件全删'并清空索引！」），
但 `--create` 恰恰**结构性地保证**了这个前提成立。

**修复**：`kb_stale` 对「目录存在但扫不到任何文件、而 meta 里有记录」也置 `missing`
（或新增 `emptied` 标志），由 `ensure_fresh` 一并跳过。

---

### 🟡 F17 — `split_sentences` 收不到 `chunk_max`，每库切块粒度覆盖失效

> **性质更正（修复时发现）**：最初写成"长无标点段落突破块上限"，不准确——
> 单句本身超限时"宁长勿断"是 `split_sentences` 的既定设计，不算 bug。
> 真正的问题是：不传 `max_len` 就回落到全局 `CFG["chunk_char_limit"]`，
> 于是**每库的 `chunk_char_limit` 覆盖传不到句子层**。已按此修。

`index.py` 两行相邻代码，一行传了一行没传：

```python
for sub in split_list_block(p, chunk_max):   # 1005: 传了
for s in split_sentences(p):                 # 1008: 没传
```

两个函数签名都是 `(text, max_len=None)`。实测：

```
输入 1040 字无标点段落 -> 1 片，最长 1040 字（chunk_char_limit=600）
```

---

### 🟢 F18 — `_expand_parent` 的「父节全文」既不全、顺序也乱、还带重复前缀

实测（命中块 = chunk 0，父节含 chunk 0/1）：

```
父节文本: '章/节\nB 段'
  - 仍带原始 'hp\n' 前缀        -> 每个兄弟块前都重复一遍标题路径
  - 排除了命中块本身            -> 标称「父节全文」，实际是「父节减去命中块」
```

再叠加输出顺序：最终给 LLM 的是「命中块 + 其余兄弟块」，
若命中的是第 3 块，输出就成了「块3, 块1, 块2」—— **叙述顺序被打乱**。

另外 `_format_results` 是**先拼父节再按 `CHUNK_LIMIT` 截断**，
所以命中块本身若已超限，`[父节全文]` 整段会被截掉，
但那次 `collection.get(where={"file": file})` 全表查询照样白跑了一遍（每条结果一次，N+1）。

---

### 🟢 F19 — frontmatter 丢失 YAML 列表型 tags

`extract_frontmatter` 用 `^([\w]+):\s*(.*)$` 逐行匹配，只认平铺标量。实测：

```yaml
tags:
  - 航天
  - CFD
aliases: [发动机, 引擎]
```
→ `{'title': '火箭发动机笔记', 'tags': '', 'aliases': '[发动机, 引擎]'}`

**Obsidian 最常见的多行 tags 写法解析结果为空串。**

严重度低，因为 —— 见下一条 —— tags 本来也进不了向量。

---

### 🟢 F20 — 文件名 / 标题 / tags 从不进入嵌入文本

`index.py:1018` 是全仓库唯一组装待嵌入文本的地方：

```python
new_texts.append((hp + "\n" if hp else "") + chunk_text)
```

即**嵌入文本 = heading path + 正文**。文件名、frontmatter title、tags 只在 1019–1026 行
写进 metadata，不参与向量，也不进 BM25 词表（BM25 建在 `documents` 上）。

对 Obsidian 这种「文件名即概念名」的库，这是一处明确的召回损失。
与 F9（裸 wikilink 被删）叠加后，笔记的**概念层信息基本没有进入索引**。

---

## 3. 尚未验证的线索（不要据此行动）

| 项 | 状态 |
|---|---|
| Chroma 1.5.9 Rust 后端是否拒绝 `kb_____` 这类名字 | 见 F15，wheel 里读不到 Rust 侧规则，需目标机实测 |
| 真实 vault 的块长分布 / 伪标题占比 | 只有仓库自带 md 的代理测量（11%），真实 vault 未知 |
| CUDA 降级状态机、`encode_safe` 是否会静默丢批 | 未审计，本机无 GPU 无法验证 |
| Windows `msvcrt` 字节范围锁的正确性 | 未审计，无法在 Linux 验证 |
| 心跳 / 进度文件的写入原子性 | 未审计 |
| `export.py` 完整数据完整性路径 | 只读了 60–139 行，未系统审计 |
| `gui/widgets.py`（1562 行）、`gui/app.py` 其余部分 | 未审计 |
| `import.py` `rebuild()` 先删 collection 再流式读 payload | 结构上是「先毁后建」，但前置 sha256 全量校验已挡住绝大部分损坏包；未判定为独立问题 |

---

## 4. 已排除（查过，不是问题）

- **MCP API 兼容性** —— `MCPServer` / `@server.tool()` / `server.run("stdio")`
  与 `mcp==2.0.0` 的实际符号一致（下载 wheel 核对过）。
- **PyPI 版本号** —— 4 个 pin 的版本都真实存在。
- **距离度量** —— 所有 collection **创建**点都显式带 `metadata={"hnsw:space": "cosine"}`：
  `index.py:717/920/1061`、`retriever.py:21`、`import.py:116/158`、`export.py:81`、
  `tests/verify_export_import.py:49`。
  （`library.py:260`、`server.py:87`、`retriever.py:410` 三处裸 `get_collection` 是**读**已存在的库，不是创建。）
- **`_merge_normalized` 与重排路径的元组结构** —— 两条路径都产出 `(name, collection, cid)`，一致。
- **`global rerank_failures` 的位置** —— 在 `except` 块内声明但该名字此前未被读，语法合法，模块可正常导入。

---

## 5. 索引质量改进清单（在约束内）

按「收益 / 代价」排序。**「需 --full」** 指改完必须全量重建才生效。

| # | 改动 | 收益 | 需 --full | 风险 |
|---|---|---|---|---|
| 1 | 修 F2（模板数值/注释对齐 DEFAULTS + 补齐缺键 + 一致性断言） | **最高**。v5 全部收益才真正生效；分发副本尤其 | 是 | 低 |
| 2 | 修 F9（保留裸 wikilink 目标词） | 高。找回概念层关键词 | 是 | 低 |
| 3 | 修 F8（切标题前跳过 ``` 围栏） | 高。消除向量污染 | 是 | 低 |
| 4 | 嵌入文本加入文件名 + title + tags（F20） | 高。Obsidian 场景召回明显改善 | 是 | 低，但会稍微稀释长块语义 |
| 5 | requirements 加 jieba + 降级（F1） | 高（可用性，不是质量） | 否 | 无 |
| 6 | 修 F6（置信度与展示顺序统一为重排分） | 中高。停止误导下游 LLM | 否 | 低 |
| 7 | 修 F7（BM25 缓存指纹换 meta mtime） | 中高。消除两路新旧错位 | 否 | 低 |
| 8 | 修 F17（`split_sentences` 传 `chunk_max`） | 中 | 是 | 低 |
| 9 | 修 F18（父节含命中块、按序、去重复前缀） | 中 | 否 | 低 |
| 10 | 删除或接线 HyDE（F11） | 中。接线则注意 2 倍开销 | 否 | 中 |
| 11 | 删除死键 `fusion_*_weight` 或恢复其语义（F10） | 低（避免误导） | 否 | 低 |
| 12 | 修 F19（YAML 列表 tags） | 低（依赖 #4 才有意义） | 是 | 低 |

**注意 #1–#4、#8、#12 都需要 `--full`**，建议合并成一次重建，并把 `META_VERSION` 提到 6。
同时要修 F14，否则版本号提升不会主动触发重建。

---

## 6. 建议的下一步（按优先级）

1. **人工确认（1 分钟，最高价值）**：在 Windows 机器上打开 `data/config.json`，
   读 `chunk_char_limit` 与 `rerank_candidates` 的实际值。
   这决定了现有索引到底是 v5 还是 v4，也决定了上面整张改进表的排序是否要调整。
2. **确认 F5 的三个 Windows 调用点**是否真的在生产里被触发过
   （查 `data/gui_index.log` 有无索引任务莫名中断、server 无故退出的记录）。
3. **搭 T1 验证层**（chromadb + 假向量，~200MB）：覆盖增量、孤儿块、删除、导入导出往返。
4. 补完未审计面：`export.py` 全量、`gui/widgets.py`、CUDA 状态机、心跳原子性。
5. 修复前先给 F2 / F6 / F7 / F8 / F9 各补一条回归测试 —— 这几条都能在纯逻辑层测，
   不需要模型也不需要 GPU。

---

## 7. 修复台账（2026-08-14 全部落地）

F1–F20 全部已修，`tests/audit_regression_test.py` 19/19 通过。

| # | 修复位置 | 做法 |
|---|---|---|
| F1 | `requirements.txt`, `retriever.tokenize` | 加 `jieba==0.42.1`；改模块级 try-import，缺失时降级纯 2-gram 并告警一次 |
| F2 | `config.CONFIG_TEMPLATE` | 模板值/注释对齐 DEFAULTS（600 / 50），**保留全部手写注释**；新增 `template_consistency_errors()` 断言出厂种子一致；`load_config` 对既有 config.json **补写缺键**（幂等、不动注释与既有值） |
| F3 | `config._strip_json_comments` | 字符串内显式跳过 `\\` 转义对，`\"` 不再被误判为字符串结束 |
| F4 | `config._coerce` | 按 DEFAULTS 同名值的类型逐项校验 + 正数范围检查；**非法值只回退该项**，合法值不受牵连 |
| F5 | `index._pid_alive`, `singleton.pid_alive` | Windows 改 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)` 只读探测；singleton 复用同一实现，不再各写一份 |
| F6 | `retriever.hybrid_search` | 置信度与最终排序**同源**：重排生效时用 `sigmoid(重排 logit)`，否则退回 RRF 一致度 |
| F7 | `retriever.get_bm25` | 缓存指纹改为 `(count, index_meta 的 mtime_ns)`，外部进程重建后立即失效 |
| F8 | `index.split_by_headings` | 跟踪 ` ``` ` / `~~~` 围栏状态，围栏内 `#` 一律当正文 |
| F9 | `index.clean_wikilinks` | 裸 `[[目标]]` 保留目标词；去路径前缀与 `#锚点`；仅 `![[...]]` 仍删除 |
| F10 | `retriever._rrf_combine` | 两路权重真正参与 `Σ w/(k+rank)`；DEFAULTS 改 1.0/1.0 保持等权，**现有排序不变** |
| F11 | `server.search_knowledge` | 接 `hybrid_search_hyde`（默认关＝零开销）；新增 `return_top_confidence` 让首轮直接带回置信度，**去掉原来 2 倍检索开销**；删除 `_top1_confidence`；对齐"取更高者"语义 |
| F12 | `config.CONFIG_TEMPLATE`, `gui/config_editor.GROUPS` | 补齐 `small_to_big` + 4 个 `hyde_*`；新增 `missing_keys()` 供回归 |
| F13 | `index.index_vault` | 补上 `incremental=incremental, full=full` 转发 |
| F14 | `index.kb_stale` | 增加 `_version` 比对，不一致返回 `version_upgrade`；`server.ensure_fresh` 据此触发重建 |
| F15 | `library.collection_for` | 退化名（含连续下划线／非字母数字结尾）追加库名 md5 前 8 位；**干净的 ASCII 名沿用旧派生结果，向后兼容** |
| F16 | `index.kb_stale`, `server.ensure_fresh` | "目录存在但扫不到文件"返回 `emptied`，自动同步跳过（优先于 `version_upgrade`——宁可不重建也不清空） |
| F17 | `index._index_core` | `split_sentences(p, chunk_max)`，每库 `chunk_char_limit` 覆盖能传到句子层 |
| F18 | `retriever._expand_parent` | 父节改为**完整、按 chunk 序、已剥锚点前缀**；`_format_results` 整体替换正文并标 `[已回填父节全文]`，命中块不再重复 |
| F19 | `index.extract_frontmatter` | 支持多行 YAML 列表与行内 `[a, b]`，统一规整成逗号分隔字符串 |
| F20 | `index._index_core` | 文件名 + title + tags 组成文件级锚点拼进待嵌入文本，逐段去重；完整前缀存 metadata `ctx` 供检索侧剥离（`_strip_ctx` 兼容旧索引的 `hp`） |

### 修复过程中的两个额外发现

1. **仓库自带测试 `test_config_editor.test_groups_cover_all_defaults` 在 HEAD 上就是红的**，
   报的正是 F12 那 5 个键。也就是说这条回归测试早就存在、早就失败，只是没人跑。
   （用 `git archive HEAD` 拉出原始代码复现确认。）
2. **F17 的性质要更正**：它不是"长段落突破块上限"（单句超限时"宁长勿断"是既定设计），
   而是**每库 `chunk_char_limit` 覆盖传不到句子层**，会回落到全局配置。已按此修。

### 上线前须知

- **必须全量重建**：`META_VERSION` 5 → 6。切块规则（F8/F9/F17）与嵌入文本（F20）都变了，
  旧向量与新查询不同分布。修好的 F14 会让 `ensure_fresh` 自动发现版本变化并触发重建，
  也可手动 `python index.py --library <名> --full`。
- **重建前先看一眼 `data/config.json` 的 `chunk_char_limit`**。若它现在是 1500，
  说明这台机器此前一直跑的是 v4 大块；建议改成 600 再重建，否则 600+small-to-big
  的配套设计仍然不生效。（补写逻辑不会改动已存在的键值。）
- **`fusion_*_weight` 会被补写为 1.0/1.0**。若你的 config.json 里已有 0.6/0.4，
  它**不会**被改动——但那两个值现在是真生效的了，等价于给 dense 更高权重。
  想保持与此前完全一致的排序，请手动改成 1.0/1.0。
- **中文库名的 collection 会改名**（F15）。当前唯一注册库 `Obsidian Vault` →
  `kb_obsidian_vault`，不受影响。若你另有中文名的库，其 collection 名会变，
  需要重建该库，或用 `python library.py config <名> --set collection=<旧名>` 钉住旧名。
- 未安装 `jieba` 时系统现在能跑但召回略降，请按新的 `requirements.txt` 补装。

---

## 附录：复现命令

```bash
# 1. 搭沙箱（不碰仓库的 data/，不装任何依赖）
S=<scratchpad>
mkdir -p "$S/harness" "$S/stubs/chromadb" "$S/stubs/mcp/server"
rsync -a --exclude='.git' --exclude='data' "<repo>/" "$S/harness/"
# 再写入 chromadb / mcp 的 stub（见 §1.2）

# 2. F2：首跑 vs 第二跑
cd "$S/harness" && rm -rf data
python3 -c "
import sys,os,importlib
sys.path[:0]=['$S/stubs','$S/harness']; os.chdir('$S/harness')
import config; a=dict(config.CFG)
importlib.reload(config); b=dict(config.CFG)
print({k:(a[k],b[k]) for k in a if a[k]!=b[k]})"
```

> 本次审计**未修改仓库任何既有文件**，未执行 `pip install`，未下载模型，
> 未写入仓库 `data/`，未做进程终止实验。唯一新增文件即本文。
