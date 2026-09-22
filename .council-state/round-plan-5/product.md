## Product 评审（只读咨询，未改一行代码；read/glob/grep 取证）

> 口径：遵循编排者裁决——不设通过/打回，只做分级；冷加载秒数引用历史实测不插桩；真库基线 9 collection；只用现有测试已钉住口径。

🔴 硬伤：
- `B1 [🔴] server.py:556（同根因第二处 server.py:575）— navigate_knowledge 两处面向 agent 的文案仍写“reindex_knowledge 只重建文字索引，不建 WEMM 页库”，与现行 index_library(wemm_sync=True 默认)+_wemm_auto_phase 自动同步（index.py:1782-1822）事实矛盾 → 影响对象：用户/agent/运维（agent 按错剧本指路手跑 CLI，真实链路其实已自动建页库）`
- `B2 [🔴] AI_GUIDE.md:168-177 + FEATURE_PARITY.md:112-117 — AI_GUIDE§8 只教 Flet 版（gui\app.py）启动/关闭，对 guiweb 零提及；但路径级勾选抽屉等能力 guiweb 独有、Flet 不同步 → 影响对象：用户（跟文档走只能拿到旧 GUI，新能力不可发现；Flet 用户在界面里管不了勾选，形成事实分裂）`

🟡 建议：
- `B3 [🟡] server.py:54-69 — _GPU_IDLE_UNLOAD_DEFAULT_S=1800.0 及注释“默认1800s”过期，config.py:37/225 三处（DEFAULTS/模板/设置页）已是 300；C3 四处同步缺第 4 处 → 影响对象：运维（回退分支计时差 6 倍；读注释排障会被误导；触发概率低，因缺键自动补写）`
- `B4 [🟡] guiweb/FEATURE_PARITY.md:11-26 — 约 20 项 ⬜（含 Top-K/include_body/库范围多选/徽章/片段展开等检索主路径）标“已实现但未逐项点击”，TODO 却记 ✅已完成 → 影响对象：用户（parity 名实部分相符，日常用 guiweb 主路径风险被低估）`
- `B5 [🟡] AI_GUIDE.md:203-204 — 新增工具速查只列 5 个 WEMM 系工具，index_status/note_relations 剧本/selection 三件套门禁无人类可读手册，全靠运行时 MCP 工具列表自发现 → 影响对象：用户/agent（人类运维无单页手册）`
- `B6 [🟡] HANDOFF.md:94-95 — Vault 用户文档组（20-Projects/Obsidian RAG/）问题 39 起更新跨工作区跳过待补；56/57/58（idle 1800→300、fp16、交接收紧）仅 TODO/TASK_LOG 有，Vault 侧待补 → 影响对象：用户（三处同步缺用户手册侧）`

🟢 备注（已验证为好/可忽略）：
- `G1 [🟢] advice 提示协议两端一致 — guiweb/bridge.py:81-85 与 gui/widgets.py:592-595 同规则（非[来源]开头→提示横幅），contracts.md:96-98 与 search_knowledge 说明书同步；阈值同尺度（advice 0.75/0.30 vs retriever.py:349 CONF_TIER_STRONG=0.75 / config 0.30）；retriever.py:405 回退 0.35 仅缺键时生效，可忽略`
- `G2 [🟢] gpu_idle_unload_seconds 可发现可理解 — DEFAULTS+模板+设置页“性能与硬件”+AI_GUIDE§9 四处可达，label/hint 讲清 0=常驻/默认 300/任务期不卸载（见 B3 尾巴注释过期除外）`
- `G3 [🟢] sidecar 双轨回收安全 — prune 覆盖 sidecar（index.py:2277/2340），运维无孤儿风险；仅“本文件走精确轨还是启发式”无诊断面，属 nicety`
- `G4 [🟢] HANDOFF.md 系 2026-09-04 快照，其 META 9/EXTRACT 4/空闲 10 分钟等过期数字属预期，接手勿引为现状`

结论：不设通过/打回（编排者裁决）；分级记录 🔴×2 / 🟡×4 / 🟢×4。最值得修的是 B1（两行文案改字即消）与 B2（AI_GUIDE 补 guiweb 入口与“用哪个 GUI”一句话）。

摘要：
1. 只读审计完成，零代码改动，取证 server/config/bridge/widgets/advice/parity/contracts/AI_GUIDE/TODO/TASK_LOG/HANDOFF。
2. 🔴 B1：navigate_knowledge 两处文案称 reindex 不建页库，与自动同步事实矛盾，agent 剧本错误。
3. 🔴 B2：AI_GUIDE 只字不提 guiweb，而勾选等能力 guiweb 独有，文档与 GUI 分裂。
4. 🟡 B3：server 空闲卸载回退常量/注释 1800s 过期，配置三处已是 300。
5. 🟡 B4：parity 约 20 项 ⬜ 含检索主路径，TODO 却标完成，名实部分相符。
6. 🟡 B5：8 个 MCP 工具无人类手册页；B6：Vault 文档组 39 起待补。
7. 🟢 advice 双端协议/阈值一致；idle 键可发现；sidecar 回收安全。
8. 建议修序：B1 改字 > B2 补文档 > B3 同步常量注释 > B4 补验证或改标注。

发现清单：
- B1 [🔴] server.py:556 — 文案与自动建页库事实矛盾（用户/agent）
- B2 [🔴] AI_GUIDE.md:171 + FEATURE_PARITY.md:112 — 双 GUI 分裂且文档缺席（用户）
- B3 [🟡] server.py:56 — 空闲回退 1800s 与配置 300 脱节（运维）
- B4 [🟡] guiweb/FEATURE_PARITY.md:14 — 主路径 ⬜ 却记完成（用户）
- B5 [🟡] AI_GUIDE.md:203 — MCP 手册缺 8 工具（用户）
- B6 [🟡] HANDOFF.md:94 — Vault 文档待补（用户）
- G1-G4 [🟢] 见上（协议一致/可发现/回收安全/快照预期）。
```

（注：委员会话无 write 工具，原文以文本返回，由编排者代落盘。）
