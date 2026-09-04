# Council 阶段1 · maker 方案（d3fbf5c 后，未改任何文件）

**目标**： 分辨并修复提取试验台三症状在 d3fbf5c 后的残留根因，并对 177ede6..d3fbf5c 全工作面做红线映射审计，产出最小修复集。

---

## 一、根因假设清单（按「可证伪性 × 概率」排序）

> 关键背景事实（本机 flet **0.86.5** 源码核实）：① `destroyed session` 报错文本来自 `Page.session` 属性——session 是 weakref，销毁后抛 `RuntimeError("An attempt to fetch destroyed session.")`；② 桌面模式 `send_message` 走 `dart_bridge.send_bytes`（C 层投递 Dart port，**跨线程机械上可行**）；③ `Session.patch_control`（diff 计算、`__index` 维护、mount/unmount）**无锁**，与 UI 事件循环并发无保护；④ `Page.run_task` = `asyncio.run_coroutine_threadsafe`，**官方支持从任意线程把协程 marshal 回 UI loop**——这是现成的合规更新通道；⑤ `pick_files` 内部超时为 **3600 秒**。

| # | 假设 | 概率 | 定位 | 判真伪方法 |
|---|------|------|------|-----------|
| **H1** | **子进程秒死无存活检测 → 最长 615 秒假忙锁死**（症状①②主嫌）。`_poll` 循环（widgets.py:1899–1931）只查 stop/deadline/queue 三件事，**从不查 `proc.is_alive()`**。spawn 子进程需重导入 gui/app.py 模块级依赖链（flet/widgets/config），pythonw/路径差异下可秒死且 `q.get` 不会报 BrokenPipe——UI 保持「提取中…」直至 deadline（auto/mineru-cloud 下 = max(30,600)+15 ≈ **615s**）。期间 `_btn_run`/`_btn_pick` 双双禁用 = 无法重新选择文件 | 高 | gui/widgets.py:1899–1931 | 无头可验：mp.Process(target=os._exit(1)) + 空 queue 对比实验 |
| **H2** | **destroyed-session 守卫缺口**（症状③残留实锤候选）。a) app.py:474–477 `_on_worker_exit` 由 worker.py `_waiter` daemon 线程直接回调 → `_snack` → `page.show_dialog` 无守卫；b) app.py:615 `_finish_search` 末尾 `self.page.update()` 在 try 之外；c) `_close_dialog`(app.py:546)、`_do_search`(app.py:583) 同类无守卫 | 高 | gui/app.py:474–477, 615, 546, 583; gui/worker.py:83–89 | 静态断言可证；fake page.session 单测复现 |
| **H3** | **跨线程 update 并发 patch race**。poll 线程每 0.5s patch `_live_text` 与刷新循环每 1s 整页 update 并发计算 diff/维护 __index 无锁——偶发错乱/RuntimeError；且 widgets.py:1920–1924 把心跳段一切 RuntimeError 当页面销毁直接 return——不 terminate 子进程、不复位状态机 | 中 | gui/widgets.py:1920–1924, 1890–1891 | 修复后对照验证 |
| **H4** | **僵尸 `_busy` 锁死**。poll 线程任何形式死亡 → `_busy=True` 永久；重开对话框走 `_reset_idle_ui`（:1826–1833）因 `not self._busy` 为假跳过全部复位 → 按钮永久禁用（点一次「关闭」即恢复是本假设指纹） | 中 | gui/widgets.py:1826–1833, 2012–2031 | 无头单测可证 |
| **H5** | **FilePicker 静默吞错**。widgets.py:1845–1846 `except Exception: return` 吞掉一切失败零反馈；service 注册失败时 `await pick_files` 挂起最长 3600s 无提示 | 中低 | gui/widgets.py:1835–1852 | 需有窗口脚本人工验证；代码层先改可见日志即可分辨 |
| **H6** | **大 Markdown 单次渲染卡顿**（体验项）。`_render` 全文一次赋 Markdown 控件再 update——Dart 侧解析大文档卡 UI，表现为结果出现瞬间整窗秒顿 | 中低 | gui/widgets.py:1964–1986 | 用户复测分辨时机 |
| **H7** | mp.Queue feeder × terminate 交互（读码核过 = 低风险，不改） | 低 | extractors.py:104–118, widgets.py:1925–1931 | 已排除 |
| — | S6 工作面其余部分：index/library/server/store/config 红线逐条读码核过（见第二节矩阵）；六件套基线实测全绿（19/15/5/config_editor/gui_store/extractors 26） | — | 各 diff 已通读 | ✓ |

**用户复测分辨手段**（修复前可做）：①损坏件跑提取看是否转圈 ~10 分钟→H1；②提取后不关对话框看能否再选文件→H4/H5；③开索引任务时关窗看控制台→H2；④记录卡顿时机：提取期间 vs 结果瞬间。

---

## 二、全工作面审计矩阵（八条红线 × 文件）

| 红线 \ 文件 | extractors.py | index.py | library.py | server.py | gui/store.py | gui/app.py | gui/widgets.py | config.py |
|---|---|---|---|---|---|---|---|---|
| 1. extractors 契约 | ✅折叠契约/仅写缓存目录 ✅26用例 ✅静态单一来源 | 引用集合✅ | 白名单引用✅ | 仅引用✅ | ISSUE_TEXT 对齐✅ | — | 子进程同管线✅ | — |
| 2. 统一终态+xsrc | sig供签✅ | ✅_terminal_entry xsrc :1337 全覆盖 ✅跑测 | — | — | meta_issues_for✅ | — | 试验台不写终态✅ | DEFAULTS✅ |
| 3. unreadable 先于 tbd + rels.add 统一 | — | ✅stat 后统一 add；冻结分支单独 add ✅跑测 | — | — | — | — | — | — |
| 4. 自愈判空基准 real_meta | — | ✅剔除哨兵与脏数据；0==0 不触发 ✅跑测 | — | _chroma_is_empty 传 allowed✅ | — | — | — | — |
| 5. META/EXTRACT_VERSION 纪律 | 不动算法✅ v2 | META 9✅静态断言 | — | — | — | — | 不改切块✅ | — |
| 6. 门禁语义不可回退 | — | ✅两处冻结分支 stat 前零I/O ✅跑测 | ✅交集+白名单校验 ✅跑测 | ✅均传 allowed ⚠建议补 server 用例 | — | — | 开关写入✅ | — |
| 7. GUI 零侵入/stop.py | — | — | — | — | — | IndexWorker 独立子进程✅ | ⚠事故面 H1–H5→方案主体；stop.py 未触碰✅ | — |
| 8. Key 不进日志 | ✅类型+摘要 | — | — | ✅ | — | — | chips 不含 key✅ | 实施时顺手核 |

---

## 三、改动清单

| # | 文件 · 位置 | 改动 | 预期行为变化 | 风险 |
|---|---|---|---|---|
| A1 | widgets.py `_poll`(:1899–1931) | 循环内加 proc.is_alive() 检测：死→排水队列一次(≈1s)，有 payload 按 done、无则 child-error 收场 chips「✗ 子进程提前退出」 | 秒死从假忙 615s 变秒级报错解锁；症状②收敛 | 低 |
| A2 | 同上心跳段(:1920–1924)/收尾段(:1936–1962) | a) 心跳 update 失败不再 return，降级继续轮询；b) 收尾 except 放宽 Exception，_busy=False+terminate 必达（finally 语义） | 消灭 poll 线程死亡→状态机不收敛族问题 | 低 |
| A3 | `_run`/`_poll`/新 `_finalize` 协程 | 结果渲染与控件复位经 page.run_task marshal 回 UI loop；调用前 session 活性检查；poll 线程零 UI 触碰 | 消除跨线程 patch race（H3）；心跳由 loop 侧绘制或值变化才更 | 中低 |
| A4 | `__init__`(:1668)/`_reset_idle_ui`(:1826) | 保存 _poll_thread；僵尸自检：_busy 且线程已结束→强制复位 | 重开对话框自愈锁死（H4） | 低 |
| A5 | `_run`(:1866–1893) | mp.Queue/Process 创建包 try/except，失败 chip+复位 | start() 异常不留脏状态 | 低 |
| B1 | app.py `_snack`(:693)/`_on_worker_exit`(:474)/`_finish_search`(:600–615)/`_close_dialog`(:546) | a) finish_search 末尾 update 入 try；b) on_worker_exit 经 run_task marshal+活性检查；c) _snack 兜底 RuntimeError | 关窗后不再刷堆栈（症状③） | 低 |
| C1 | widgets.py `_switch` 重复定义(:1783/:2006) | 删后者纯清理 | 无 | 零 |
| C2 | `_render`(:1964)/sanitize_render_md | （可选）预览 >100k 字符截断+chip 注明 | 缓解 H6 | 极低可裁 |
| C3 | `_pick`(:1845–1846) | 吞错改可见日志/chip | 可分辨选件失败 | 低 |

结构性改动论证：全部函数内局部修，A3 仅换更新通道不改执行模型。

---

## 四、验证计划

六件套回归全绿 + verify_export_import 最后跑。新增用例：
1. test_poll_dead_child_fast_fail（存活判定提为模块级纯函数 _classify_outcome）
2. test_extract_lab_zombie_busy_recovers（headless 构造+_busy 死线程→复位断言）
3. test_app_session_guards_statics（inspect.getsource 断言守卫）
4. test_poll_never_touches_ui_directly（静态断言）
5. （可选）test_server_pending_formats_report

用户复测清单：①提取期间拖动即时响应 ②完成后可直接再选 ③进行中关窗安静 ④损坏件秒级错误 chip。

---

## 五、明确不做

不动 META/EXTRACT_VERSION 与切块清洗逻辑；不动门禁语义；不碰真实 Vault/真库；不做 git 写操作；不引第三方依赖；不放宽既有断言；不扩大重构（不整体异步化、不换 mp.Queue、不动 stop.py、不统一全部对话框更新模式）。

---

## 待确认三项

1. C2 截断是否纳入本次；
2. H5 最终判定需有窗口脚本人工跑——实施阶段还是交用户复测；
3. 若复测表明只剩症状③，B1 独立先行、A 组降优先级。
