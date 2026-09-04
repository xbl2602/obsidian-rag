# 架构师评审 · 阶段1（round-plan-2）

## 总评
方案在试验台局部假设完备、改动克制、可证伪性好，A3 技术路线（poll 线程 + run_task marshal）是最简正确形态而非过度设计。但存在一处结构性盲区：症状①的根因空间完全没审 GUI 主循环自身的重活（每秒 4 次全库快照扫描 + 逐秒整页 update + 日志逐行整页 update），归因链不完整，若 H1 被其自己的对比实验证伪，A 组修完症状①大概率残留 → 二轮返工。

## 发现

[🔴blocker] B1: 方案盲区——症状①根因空间漏掉 GUI 主循环阻塞，改动集对目标不充分 — gui/app.py:244/267/288/342/361/386/316 + 方案§一 — _refresh_once 在 UI 事件循环内同步执行，每 tick 调 library_snapshot() 整整 4 次（267 index_state→store.py:126；342 _selected_rows；361 _update_kpi_subs；386 _update_status_card），每次快照=读注册表+每库 2 遍 meta JSON 解析+全库 rglob 目录树遍历+逐文件 stat；另有 meta_stats() 第三遍解析和 _issues_suffix 每库再读 meta；最后 :316 无条件整页 update。大 vault 下单 tick 可阻塞事件循环数百 ms～秒级，与提取是否运行无关——按钮点击排队正是症状①的独立机制。建议：①主循环阻塞补入 H 清单+零成本判别（_refresh_once 耗时打点或让用户不开试验台复测）；②坐实则每 tick 快照只算 1 次向下传参。

[🟡建议] B2: 同 tick 四重快照是模块边界模糊的症状 — app.py:342/361/386 + store.py:83-103 — _refresh_once 开头取一次 (agg, rows) 作参数传入三个 UI 组件；消掉 index_state 与 meta_stats 的重复解析。
[🟡建议] B3: 更新纪律自相矛盾 — widgets.py:1665 立约「不整页 page.update」但 app.py:316 每 tick 整页 update；LogView.append 每行整页 update 而 app.py:472 注释声称批量提交实际没有 — append 只改内存，由刷新循环节拍统一提交。
[🟡建议] B4: B1 守卫清单遗漏 app.py:597-598 add_done_callback(lambda f: page.run_task(...)) 销毁后仍触发 — lambda 包 try 或并入 B1b 统一模式。
[🟡建议] B5: C 系列拆 commit——C2 本次裁掉（H6 未实锤），C1/C3 单独提交。
[🟡建议] B6: A3 心跳双措辞定死为「标签串变化时经 run_task 提交赋值」，不新增第二个周期任务。

🟢 可忽略：A3 非过度设计（mp.Queue 无 async 接口、Proactor loop 不能 add_reader 管道，executor 包装更绕）；_classify_outcome 提纯层级恰当；A4/A5 有症状指纹支撑；审计矩阵与验证计划扎实。

结论：需返工（阻断数 1）——maker 补 B1 判别手段与快照合并即可，技术路线无需推翻。

## 对待确认三项
1. C2 本次不纳入（改预览语义且 H6 未实锤）；
2. H5 交用户复测清单顺带完成；
3. 同意 B1 先行，但前提是先做零成本判别拿数据，再定 A/B 优先级。
