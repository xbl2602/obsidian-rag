# 可靠工评审报告 — 多格式文档支持（R1/R2/R3a）

范围：`92af6e6..HEAD`，聚焦异常处理/超时/重试/降级/状态一致性。

---

**B1** 🔴 blocker — `extractors.py:350-380`（`_extract_pdf`）

页级文字层扫描循环（359-361 行）位于 `doc = pymupdf.open(...)` 之后的第二个 `try` 块内，但该 `try` 只有 `finally: doc.close()`，没有 `except`——只有紧邻的 `pymupdf4llm.to_markdown(doc)` 调用单独包了 try/except。

触发场景：能被 `pymupdf.open()` 成功打开、但某一页内部结构损坏的 PDF，`get_text()` 时抛异常。异常从 `_extract_pdf` 直接冒出，穿透 `_extract_full`、`extract_to_markdown`——违反 extractors 契约"绝不抛异常"（AGENTS.md 红线 1）。

后果：`index.py:1335` 调用点无 try/except 包裹，异常一路冒到 `_index_core` 顶层 `except Exception as e: progress_error(e); raise`，导致整库本轮索引中止（尚未提交的写入全部作废）。更严重：异常发生在 meta 写入之前，这个"毒药 PDF"永远不会落入终态，下一次索引会在同一文件上重复崩溃——统一终态机制本该封堵的死循环，此路径完全绕过。

`tests/test_extractors.py:209` 只覆盖了"`pymupdf.open()` 打开失败"场景，未覆盖"能打开但扫描页文字层时抛异常"。

---

**B2** 🔴 blocker — `gui/widgets.py:1866-1962`（`ExtractLabDialog._run` / `_poll` / `_close`）

`self._proc`、`self._busy`、`self._stop` 跨线程共享，无"运行代号"标记本次调用属于哪一次 run。

触发时序：用户点开始提取（run1）→ 关闭对话框（`_close` 立即置 `_stop.set()` 且 `_busy=False`，但不等待/join proc1，清理丢给下一次轮询）→ 在轮询线程真正 terminate proc1 之前，用户重新打开对话框再次点开始提取（run2，因为 `_busy` 已被提前清空，防重入判断挡不住）→ `_run` 覆盖 `self._proc = proc2` → 轮询线程读到 proc2，把刚启动的 run2 强行 terminate；真正该清理的 proc1 失去引用成为孤儿进程。

后果：子进程泄漏 + 正在运行的新任务被旧线程误杀 + `_busy`/UI 状态交叉污染，命中条件仅需"关闭对话框后较快重新开始"，正常使用节奏下不罕见。

---

**B3** 🟡 note — `gui/widgets.py:1920-1924` 会话销毁异常直接 return，未 terminate 子进程，MinerU 请求可能无人监视跑满超时（默认 600s）。

**B4** 🟡 note — `index.py:1344-1349` 防御分支对二进制源落 "empty" 终态未传 `xsrc`，隐性偏离红线2字面要求（当前不构成功能 bug）。

**B5** 🟡 note — `extractors.py:400-482` MinerU 云端失败一律折叠为 extract-failed，瞬时性故障（限流/网络抖动）会被永久冻结，缺少退避重试机制。

---

## 总体结论
统一终态机制、一致性自愈、xsrc 签名重试在主链路基本到位，但存在一处真正会导致死循环/整库索引中止的异常处理漏洞（B1）和一处会导致子进程泄漏+状态错乱的 GUI 并发竞态（B2），建议在合入下一轮前修复。
