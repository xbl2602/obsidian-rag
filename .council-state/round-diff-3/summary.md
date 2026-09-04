# round-diff-3：对本轮修复本身的纯盲审（不告知委员之前发现过什么）

范围：工作区未提交改动中，第一批 6 处修复引入的新代码（extractors.py/gui/store.py/gui/widgets.py/index.py + 测试）。6 位委员全新实例、互不可见、不知道 round-diff-1/2 的任何结论。

## 结果

| 委员 | blocker | 主要发现 |
|---|---|---|
| 架构师 | 0 | note：SettingsDialog/ExtractLabDialog 两处云端确认对话框代码几乎逐行重复，未抽公共 helper，文案已开始不一致 |
| 可靠工 | 0（但发现的问题和红队B2同根因） | note：确认框弹出期间无_busy锁，理论可重复触发；`q.put`写在`with tempfile...`块外，临时目录清理失败会把成功结果误报为失败 |
| 安全官 | 0 | note：既有行为提醒，非本轮新增风险 |
| 产品代表 | 0 | note：配置键名`pdf_scan_backend`未译成中文；试验台连续测试云端后端每次都要重新确认；AI_GUIDE.md教的"手改config.json"路径完全绕开新加的确认机制 |
| **红队** | **2** | **B1（已修）**：`_poll`对子进程`proc.terminate()`是Windows无条件硬杀，子进程`with TemporaryDirectory`的清理不会执行，云端OCR产物残留在系统%TEMP%下无回收机制。**B2（已修）**：云端上传确认框"确认"按钮无防重入锁，`_start_extraction`未检查`self._busy`，双击/重复事件派发会导致同一文件被重复上传给第三方 |
| 性能师 | 0 | note：预览隔离后失去缓存复用是正确性代价，可接受 |

## 处理结果

红队 2 个 blocker + 可靠工发现的 q.put 顺序问题（同一段代码，一并修）已全部修复：
1. 临时目录创建/清理责任转移到父进程（GUI），`_poll` 无论 outcome 是什么都无条件 `shutil.rmtree` 清理；额外加了 `_sweep_orphan_preview_dirs()` 清扫 >24h 孤儿目录（对齐 extractors.py 已有的 `_sweep_orphan_tmp` 风格），覆盖父进程自己崩溃的极端情况。
2. `_start_extraction()` 内部最开头加 `if self._busy: return` + 立即置位，防重入检查下沉到唯一的启动入口，不再依赖调用方（`_run()`/确认框回调）各自保证只调一次。
3. `_preview_job` 重构为"提取成功立刻 `q.put`，收尾清理放 `finally`"，清理失败不会覆盖已送出的成功结果。

七件套回归全绿（20/20、15/15、5/5、6/6、37+/37+、32/32、39/39）。新增 7 个测试用例覆盖这三处修复。改动范围：`extractors.py`、`gui/widgets.py`、`tests/test_extractors.py`、`tests/test_gui_store.py`。

架构师发现的"两处确认框重复"note 未处理（用户未要求本轮一并修）。
