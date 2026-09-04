# 架构评审报告 — 多格式文档支持（R1/R2/R3a）

审查范围：`git diff 92af6e6 HEAD` 涉及的 `extractors.py`（新）、`index.py`、`library.py`、`config.py`、`server.py`、`gui/widgets.py`、`gui/store.py`、`gui/app.py`、`gui/config_editor.py`。仅从设计合理性、模块边界、过度设计三个维度评审。

---

## B1 🔴 blocker — 终态判定逻辑在 `kb_stale` 与 `_index_core` 中重复实现，而非单一事实来源

**位置**：`index.py:1018-1048`（`kb_stale`）与 `index.py:1294-1349`（`_index_core`）

AGENTS.md 红线 2 明确要求"统一终态机制"，但代码里实际存在两套几乎并行的终态判定状态机：`kb_stale` 里针对 `unreadable`/`empty`/`tbd`/二进制哈希比对各写了一遍"是否已收敛"的判断，`_index_core` 主循环里又针对同样四类分支各写了一遍"落 `_terminal_entry`"的处理。两处的判定条件（`entry.get("reason") == "empty"` 等字符串字面量）各自手写、没有共享的分类函数或查表结构。

这不是功能 bug，而是架构问题：未来加一种新 reason（例如 R3b 本地 OCR）时，只要漏改其中一侧，就会退化成"每轮误判 stale 死循环"——而这正是该机制本应封堵的问题。

## B2 🟡 note — GUI 提取试验台自建一套子进程治理，与"GUI 零侵入观察者"红线产生擦边
`gui/widgets.py:1650-2032`（`ExtractLabDialog`）自行维护超时轮询/terminate/join，与 `gui/stop.py` 的治理经验没有复用关系。

## B3 🟡 note — `extractors.py` 混入完整的远程 HTTP 客户端实现，模块职责边界被拉宽
`extractors.py:398-482`（`_mineru_cloud_extract`）把云服务 SDK 混进提取层文件。

## B4 🟡 note — `gui/widgets.py` 中 `_switch` 方法被定义两次（1783/2006），后者静默覆盖前者（死代码）。

## B5 🟡 note — 为未实现的 `mineru-local` 后端预留了完整分支和缓存路由，构成对不存在场景的过度设计（`extractors.py:35,181,368-371`）。

## B6 🟡 note — `agent_formats` 与 `extensions` 是两份需手动保持从属关系的状态（`library.py:151-172,248-283`），一致性靠运行时校验而非数据结构保证。

---

## 总体结论
架构总体站得住：extractors.py 契约基本落实，SUPPORTED_EXTS 作为单一事实来源被老实引用，GUI 未直接碰 Chroma/模型。但"统一终态机制"是用两套手写并行逻辑模拟出来的而非真正收拢（B1），是本轮最值得修的结构性问题。
