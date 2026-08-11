# 多库 RAG（Multi-Library）设计文档

> 日期：2026-08-11 · 状态：草案 · 前置：2026-08-11-gui-design.md（已批准，不受影响）

## 1. 目标

将"单 vault → 单索引"升级为"**多库注册表**"：任意 md 文件夹可单独注册为一个 RAG 库，
每次检索可指定单库 / 多库并查 / 全部库（默认）。AI agent 通过 MCP 可无歧义地发现与选择库。

**非目标（明确延期）**：GUI（保持 MCP/CLI 优先）、PDF/docx/xlsx/pptx 等非 md 格式
（只预留扩展位）、每库独立 embedding 模型（union 检索要求同一向量空间，模型保持全局）。

## 2. 方案选型：单 Chroma 实例多 collection（方案 A）

| 方案 | 做法 | 结论 |
|---|---|---|
| **A（选）** | 一个 `data/chroma/` 实例，每库一个 collection + 按库 BM25 缓存 | 改动最小，Chroma 官方标准用法 |
| B | 每库独立数据目录 `data/libraries/<名>/chroma` | 跨库检索需开 N 个 PersistentClient，开销/复杂度翻倍，物理隔离本机无用 |

A 中库之间的"隔离"由 collection 提供：独立 count/删除/重建/指纹，互不可见。
个人本机场景不需要物理隔离（备份走 export/import，已按库打包）。

## 3. 架构总览

```
data/
├─ config.json        ← 全局默认值（手写注释，不变动结构，机器不重写）
├─ libraries.json     ← 库注册表（机器维护，CLI 增删改查）
├─ index_meta_<名>.json  ← 每库独立指纹
└─ chroma/            ← 单实例，每库一个 collection（派生名 kb_<name>，可覆盖）
```

### 3.1 注册表 `data/libraries.json`

```json
{ "libraries": [{
  "name": "Obsidian Vault",
  "path": "D:/_STOREROOM/lol/Obsidian Vault",
  "collection": "obsidian_kb",
  "exclude_dirs": null,
  "exclude_files": null,
  "exclude_patterns": null,
  "chunk_char_limit": null,
  "short_doc_char_limit": null,
  "extensions": ["md"]
}]}
```

- `name`：库唯一标识，AI 调用参数；默认 = 文件夹名（可 `--name` 覆盖）。校验：
  非空、全局唯一、不得重复注册同一路径。
- 覆盖字段为 `null` 时继承 `config.json` 全局值；不覆盖项一律取全局。
- `collection`：默认派生 `kb_<name>`（name 中的非法字符替换为 `_`）；显式设置则用设置值。
- `extensions`：可索引扩展名列表，现仅 `["md"]`；日后加 pdf/docx 在此列 + 加载器（本迭代不做）。
- 独立文件的原因：CLI 会整文件重写，而 `config.json` 含手写注释，不能被机器重写破坏。

## 4. CLI

### 4.1 新增 `library.py`（库管理，AI 可调）

```
python library.py list|ls                  # 全部库：名/路径/块数/最近索引/配置摘要
python library.py add <路径> [--name 名]    # 注册新库（默认名=文件夹名）
python library.py remove <名> [--drop]      # 注销（默认仅注销保留数据；--drop 连 collection 一起删）
python library.py config <名> --set key=val # 改独立配置（合法键见 3.1）
```

- `add` 校验：路径存在、不重复；写入后提示下一步 `index.py --library <名>`。
- `remove` 非 `--drop`：仅从注册表移除（数据保留，重新 add 同路径可恢复，collection 继续沿用）；
  `--drop` 需二次确认（非交互场景加 `--yes` 跳过），删除 collection 与指纹文件。
- `config --set` 合法键：exclude_dirs/exclude_files/exclude_patterns/
  chunk_char_limit/short_doc_char_limit/extensions/collection；`--unset` 恢复继承全局。
- 日志走 stderr（与 index.py 的 log 约定一致）；数据型输出（list 表格）走 stdout。

### 4.2 `index.py` 扩展

```
python index.py --library <名|all> [--full]
```

- 默认 `all`：顺序处理全部注册库；单库场景等价旧行为。
- 无注册表时（迁移前）行为见 §7。

## 5. 索引（index.py 参数化）

- `index_library(lib, incremental, full)`：collection、指纹文件、排除规则、切块粒度、
  扩展名全部取自该库的合并配置。
- 指纹文件 `index_meta_<名>.json`；迁移时旧 `index_meta.json` 改名随行（见 §7）。
- 进度文件 `index_progress.json` 增加 `library` 字段；`index_status` 显示当前库。
- 锁/心跳/显存/降级等机制原样复用，锁仍全局单文件（一次只跑一个索引任务）。

## 6. 检索（retriever.py 多库融合）

### 6.1 内部改动

- `get_collection(lib)`：按库取 collection。
- **BM25 缓存全局单例 → 按库字典** `_bm25_cache = {name: (bm25, ids, files)}`，
  每库懒加载；`reset_bm25_index()` 全量失效（可加参数按库）。
- 块 id 维持 `rel::i`（各 collection 物理隔离，无撞车）；内部去重键与统计用 `(库, rel)`。

### 6.2 跨库合并排序

1. 每个选定库独立跑 dense+BM25 融合，取各库融合 top `rerank_candidates` 进**跨库重排池**；
2. 重排器（cross-encoder，纯文本打分，不依赖向量空间）对整个池全局精排 → 最终 top_k；
3. 重排器不可用/关闭时降级：各库融合分按库内最高分归一化（0-1）后合并排序取 top_k。

### 6.3 结果格式

```
[来源] <库名>/<相对路径> (## <标题>) [块 k/N] [置信度 x.xx]
```

- 库名前缀保证同名文件跨库不歧义（AI 可直接据库名继续操作）。
- `folder` 参数语义不变：作用于每个库的库内相对路径（`_in_folder` 复用）。
- `include_body=False` 两阶段模式、单文件封顶、截断标记等行为不变（封顶/计数键改用 (库, rel)）。

## 7. 迁移（零重建升级）

首次运行新代码且 `libraries.json` 不存在：
1. 自动合成第一个库：`name` = 现 vault 文件夹名，`path` = 现 vault（或
   `OBSIDIAN_VAULT` 环境变量，环境变量仍只作用于该库）；
2. `collection` 沿用 `obsidian_kb`（不派生，避免与旧数据断裂）；
3. `index_meta.json` 改名为 `index_meta_<名>.json`（失败则置空走自动全量重建）；
4. 打印迁移日志。

老提示词兼容：`search_knowledge` 不带 `libraries`/`exclude` = 全部库；单库时代等价旧行为。

## 8. MCP 工具（server.py）

```
list_libraries()                # 新增：枚举库（名/路径/块数/最近索引），先查再搜
search_knowledge(query, top_k, libraries="", exclude="", folder="", include_body=True)
reindex_knowledge(library="")   # 空 = 全部
index_status()                  # 原样，进度含 library 字段
```

### 8.1 库选择语义（四种模式全覆盖）

| 意图 | 调用 |
|---|---|
| 只搜指定库 | `libraries="Rocketry"` |
| 多库并查 | `libraries="Rocketry,Books"` |
| 全选（默认） | 两个参数都留空，或 `libraries="all"` |
| 反选（全选排除若干库） | `exclude="Books"` → 最终 = 全部库 − Books |
| 白名单内再排除 | `libraries="A,B", exclude="B"` → 最终 = A |

规则：
- **最终范围 = (libraries 非空 ? libraries : 全部库) − exclude**；两参数均为逗号分隔的库名列表。
- 两个参数都做库名校验：出现陌生库名 → 明确报错并**列出可用库名**（取自注册表，
  与 `list_libraries` 输出一致），杜绝 AI 猜名。
- 重复名自动去重；`libraries` 与 `exclude` 同库名时按减法语义自然消解。
- 计算后范围为**空集** → 明确报错说明（"所选范围为空：exclude 覆盖了全部指定库"），
  绝不静默返回空结果（否则 AI 会误判"库中无内容"）。
- AI 使用流程：先 `list_libraries()` 看有哪些库 → 按上表选择参数。

- `ensure_fresh`：逐库检查指纹，有变化的库后台增量重建；搜索用现有索引结果先行返回。
- 库名校验失败的错误信息必须**列出可用库名**（list_libraries 的输出），杜绝 AI 猜名。

## 9. 导出 / 导入

- `export.py --library <名>`：manifest 记录库名与 collection；默认导出全部库（逐库打包或合并包，本迭代选逐库独立打包，路径 `data/export/`）。
- `import.py --library <名> [--create]`：导入到指定库；库不存在且带 `--create` 时自动注册
  （路径参数化，`--path` 指定）。包内无库名或未指定时按包内 manifest 处理。
- AI_GUIDE.md 同步：导入命令示例加 `--library` / `--create` 分支。

## 10. 测试

1. `tests/library_registry_test.py`（新增，纯逻辑无模型）：
   注册表读写往返、默认值继承合并、CRUD 校验（重名/重复路径/非法键/路径不存在）。
2. 双库检索冒烟（手动脚本）：两个微型夹具库 `A/B`，验证跨库排序、
   `[来源] <库>/<rel>` 标注、`libraries="A,B"` 与 `libraries=""`（全库）路径。
3. `tests/eval_retrieval.py` 回归：改为显式传默认库名，基线不变
   （top1=3/5、top3=5/5、top5=5/5）。

## 11. 文档同步（收尾）

- 项目 AGENTS.md（20-Projects/Obsidian RAG）§检索协议：加多库参数与 list_libraries。
- vault 根 AGENTS.md §2 检索协议参考：同步多库用法。
- `config.py` 模板注释：删除"各配置档一库"过时说法，改为指向 libraries.json。
- AI_GUIDE.md：导入命令加 `--library` / `--create` 分支。

## 12. 改动文件清单

| 文件 | 动作 |
|---|---|
| `library.py` | 新增（注册表 CRUD，~150 行） |
| `config.py` | 新增 LIBRARIES 加载/合并（注册表缺失时迁移合成） |
| `index.py` | 按库参数化：collection/指纹/排除/切块/进度 library 字段/CLI |
| `retriever.py` | 按库 collection + BM25 缓存字典 + 跨库重排池/归一化降级 + 结果库名前缀 |
| `server.py` | list_libraries 新增 + 三个工具参数化 |
| `export.py` / `import.py` | `--library` 参数化 |
| `tests/library_registry_test.py` | 新增 |
| `tests/eval_retrieval.py` | 显式默认库名 |
| 项目 AGENTS.md / vault 根 AGENTS.md / AI_GUIDE.md / config 模板注释 | 同步 |
