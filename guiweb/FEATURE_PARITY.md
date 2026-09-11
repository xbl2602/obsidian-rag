# FEATURE_PARITY — guiweb 相对 Flet GUI（gui/）的功能对齐清单

> 验收标准：Flet 版功能一项不少（✅=已实现且验证），demo 带来的增量单列。
> 原 `gui/` 保留不删，两套 GUI 可并存。
> 验证记录（2026-09-06）：mock 模式浏览器实测（图谱/检索/库/索引/设置视图渲染、
> 搜索流、全量红确认/移除双选项/导入逐字/库范围多选/云端同意门禁弹层）+
> Python 侧真实数据只读冒烟（快照/库清单/图谱 397 节点/失败明细/WEMM 探活）+
> 云端同意门禁探针复现（none→mineru-cloud 正确弹窗）。标注 ⬜ 的为已实现但
> 未逐项点击的次要交互（主题切换动画、试验台真实子进程全流程——真机首跑时顺手验证）。

## 1. 检索
| Flet 功能 | guiweb 落点 | 状态 |
|---|---|---|
| 问题输入 + 回车/按钮 | 检索视图 + 图谱搜索岛 | ⬜ 待前端集成验证 |
| Top-K（3/5/8/10/15） | 同 | ⬜ |
| 展开正文开关（include_body） | 同 | ⬜ |
| 库范围多选（含全部库/反选） | Header 库胶囊 + 检索页范围 | ⬜ |
| 结果解析（来源/标题/块号/置信度） | bridge.parse_search_text（结构化 JSON，不再前端正则） | ✅ 单测 |
| 置信度三档徽章（≥0.75/≥0.5/其余） | 前端渲染 | ⬜ |
| 片段展开/收起（收起 3 行预览） | 前端 | ⬜ |
| 命中正文默认渲染视图 + 渲染/源码切换 | 后端 rendered_html（_md_to_html）+ 前端 r-viewtgl | ✅ 单测 |
| 查看正文（GUI 内读全文，不跳外部） | read_document + 正文弹层（mDoc）| ✅ 单测 |
| 关联笔记内联展开（懒加载+缓存） | note_relations + 前端缓存 | ⬜ |
| 打开源文件（Obsidian URI/startfile 回退） | bridge.open_source（移植 _open_result） | ✅ 代码移植 |
| 首次检索 30–60s 提示、耗时显示 | 前端 loading + elapsed | ⬜ |
| 空态/错误 SnackBar | toast | ⬜ |

## 2. 索引
| Flet 功能 | guiweb 落点 | 状态 |
|---|---|---|
| 增量重建 | start_index(full=false) | ⬜ |
| 全量重建 + 红色确认（目标库/约 N 块/预计/可用性警告） | start_index(full=true) + 前端确认框 | ⬜ |
| 五阶段 stepper（扫描→转换→嵌入→写库→完成） | snapshot.progress.phase | ⬜ |
| 百分比/进度（嵌入按块、其他按文件） | progress.pct（store.progress_ratio 复用） | ✅ 复用 |
| 已用时间/ETA/计数行/库名 | progress.elapsed + 前端 ETA 估算 | ⬜ |
| 心跳五态（running/dead/stalled/done/idle）+ 呼吸 | progress.heartbeat（store.heartbeat_state 复用） | ✅ 复用 |
| 停滞宽限文案/转换豁免 | heartbeat_note（store 复用） | ✅ 复用 |
| DEAD 判定 + 告警 SnackBar | bridge alert 推送（每次劣化一次） | ✅ 代码 |
| 跨进程忙碌判定（MCP 触发感知） | progress.busy（store.index_busy 复用） | ✅ 复用 |
| 上次耗时 chip | last_elapsed | ✅ 代码 |
| **新增** 停止索引（demo 增量） | stop_index（taskkill 整树，仅限本应用拉起的任务） | ✅ 代码 |

## 3. 库管理
| Flet 功能 | guiweb 落点 | 状态 |
|---|---|---|
| 库清单（名称/路径/块数/最近索引/覆盖摘要） | list_libraries（library.list_summary 复用） | ✅ 复用+冒烟 |
| 添加库（路径+可空名） | add_library | ✅ 代码 |
| 每库配置：格式/门禁/排除/切块/collection 覆盖 | get_library_config + set/unset（空=恢复继承） | ✅ 代码 |
| Agent 门禁开关（批准/撤销）+ 警示条 | 库配置弹层内开关 | ⬜ |
| 移除双确认（仅注销 / 删数据+勾选） | remove_library(drop) + 前端门禁 | ⬜ |
| 打开文件夹 | open_path | ✅ 代码 |
| 提取试验台（单文件、后端覆盖、子进程隔离、双页签、取消/超时、缓存隔离） | preview_start/poll/cancel（_preview_job + 临时目录父进程清理） | ✅ 代码 |

## 4. 设置
| Flet 功能 | guiweb 落点 | 状态 |
|---|---|---|
| 12 分组全字段（GROUPS/FIELD_META 驱动） | get_settings 动态下发，前端不硬编码 | ✅ 代码 |
| choices 下拉 / suggest 芯片（✓使用中）/ secret 密码框 / bool 开关 / list 逗号 | kind 渲染 | ⬜ |
| rebuild ⟳ 标记 | field.rebuild | ⬜ |
| 注释保留写回 + 批量保存 + 错误就地显示 | apply_updates 复用 + save_settings | ✅ 复用 |
| 热读生效 | apply_updates 内 reload_cfg | ✅ 复用 |
| 云端同意门禁（切 mineru-cloud 先确认，取消回退） | 前端门禁 | ⬜ |
| missing_keys 静态断言 | get_settings.missing_keys | ✅ 复用 |

## 5. 观察 / 治理 / 全局
| Flet 功能 | guiweb 落点 | 状态 |
|---|---|---|
| KPI（文件/块/耗时/状态聚合） | snapshot | ✅ 代码 |
| 日志区（历史尾 300 行、着色、计数、清空） | log_tail + worker 复用 | ✅ 代码 |
| 打开 Vault / 打开日志目录 | open_source/open_path/get_static_path | ✅ 代码 |
| 设备信息行（device·模型·库·块数） | snapshot.device（torch 懒探测线程） | ✅ 代码 |
| 占用行：显存/GPU/功耗/CPU/看图模型状态（问题47） | snapshot.gpu/cpu/wemm_live（只读，fail-open） | ✅ 代码 |
| 深/浅主题 | 前端 CSS 变量反转 | ⬜ |
| GUI 单例守卫 | app.py 文件字节锁（独立锁文件，可与 Flet 版并存） | ✅ 代码 |
| xfail 汇总文案（扫描件×N…） | snapshot.issues（ISSUE_TEXT 复用） | ✅ 复用 |

## 6. demo 增量（Flet 没有的新功能）
| 功能 | 落点 | 状态 |
|---|---|---|
| 全库图谱（双链+归属+可选语义边、图层、管线四态着色、库边界、检索轨道） | graph()/semantic_edges + 前端画布 | ✅ 后端/⬜ 前端 |
| 逐文件失败明细（file_index_rows_for 接线） | failures() | ✅ 代码 |
| WEMM 状态面板 + 服务探活 | wemm_status/wemm_probe | ✅ 代码 |
| 近似去重报告 | dedup_run | ✅ 代码 |
| 导出/导入（子进程启动器；导入需逐字确认） | export_run/import_run | ✅ 代码 |
| 动态岛全局状态 | snapshot 推送 | ⬜ |
| 结构化检索接口（替换文本协议解析耦合） | parse_search_text 后端集中 | ✅ 单测 |

## 真实 bug 修复记录（只收可复现、确认属实的）
1. **检索整体提示行被渲染成幽灵结果**（guiweb 与 Flet **都已修**）：
   「（多条高置信命中…把 top_k 调大）」「（本次查询整体置信度偏低…）」
   「（同一文件最多展示 N 块…）」这类游离提示行会被当成来源行渲染成畸形结果卡。
   guiweb.parse_search_text 将其归为 `notice:true` 提示条目；Flet 的块切分
   （gui/widgets._render_results，2026-09-11 问题55）改为同规则并渲染成提示横幅——
   此前 Flet 会把第一个非 `[来源]` 行当成某块的 src、把后续真实结果全吞进它的正文。
   复现：`SearchCard().show_results(带 2 行建议 + 2 条结果的文本)`，旧实现顶层只有
   1 张畸形卡（文件名位置显示整句提示、正文是后续结果），现为 2 横幅 + 2 结果卡。
2. **设置页整页变成空下拉**（guiweb 真机实测，2026-09-06）：fieldRow 用
   `if (f.choices)` 分支，bridge 对无选项字段返回空数组 `[]`，JS 空数组是
   truthy → 几乎全部字段（含 bool 开关）渲染成零选项的空 select。修复：
   `if (f.choices && f.choices.length)`。回归：test_guiweb.py
   test_settings_choices_guard。
3. **图谱检索无加载反馈 + 置信度角标残留**：冷启动 30-60s 零反馈、重复点击被
   G.searching 守卫静默吞掉；clearGLit 不清 `.g-conf` 旧角标，两轮连续检索
   显示互相污染。修复：按钮 busy 态 + toast；clearGLit 同步清角标。浏览器
   两轮连续检索实测干净。
4. **生产版推送总线缺失（2026-09-08，问题47 附记）**：bridge 每秒
   evaluate_js 调 `window.__push`，但生产版 app.js 从未定义它（只在
   mock.js 里有）——守卫 `&&` 把全部快照/日志推送静默吞掉，KPI 与进度
   永远停在启动那一刻，只有直接 API 调用（toast/按钮）有反应，用户体感
   "点了显示启动、底下毫无反应"。修复：app.js 顶层定义同语义分发器。
   回归：test_guiweb.py test_snapshot_sys_fields_contract。
## 问题44 增量（2026-09-06）：路径级勾选建模（guiweb 独有，Flet 备用版不同步）
| 功能 | 落点 | 状态 |
|---|---|---|
| 库管理「勾选范围」右侧抽屉（下钻/生效态徽章/跟随恢复/格式快捷批量/攒批保存） | selection_tree/selection_update/selection_format_bulk | ✅ 代码 |
| 排除=对管线不存在（扫描漏斗唯一过滤，块与 WEMM 页自动清理） | collect_md_files selection 参数 | ✅ 代码 |
| MCP 两段式确认门禁（提案号+确认码+TTL+一次性+审计） | selection_gate.py + server 三工具 | ✅ 代码 |
