# Product 评审（方案门 第1轮）

## 总体判定：BLOCKER（2 条规格漏洞）

### Blocker 清单

**B1 旧进度文件缺 stall_grace_until 字段时判定侧行为未定义**
- 若缺失即视为"无宽限"，代码升级后未重启的旧索引进程跑 converting 大文件时会被新 GUI/MCP 判 stalled（比现状糟）；若保留 converting 白名单则需显式约定。
- 要求：两侧对字段缺失/非法值显式约定回退口径（建议：字段缺失 → converting 维持现行豁免、其余相位走原 STALL_TIMEOUT，并注明"升级须重启全部进程"），配进回归用例。

**B2 MinerU 云端 OCR 单文件预算 600s（config.py:83、extractors.py:486-528 轮询期完全安静）远超拟议 300s 档**
- 若 converting 相位套用默认宽限，大扫描件 OCR 必然再次误报。
- 要求：converting（尤其云端 OCR 路径）单独设 ≥ mineru_timeout_seconds 的宽限，或转换启动时按预算动态写入宽限，加边界用例。

### 非阻塞建议
- S1 三态呈现靠文案区分不够，建议宽限态附量化信息（如"首次加载嵌入模型… 已安静 45s"），HeartbeatPill.set_state 的 note 参数现成支持（gui/widgets.py:179）。
- S2 双看门狗一致性用例扩展到宽限场景（同一快照下 HB_RUNNING 且 progress_text 无停滞字样）；现有 p3 断言（scanning 停滞必 stalled）语义会变需同步更新并注明理由。
- S3 index.py:400-401 elapsed<300 启发式 tip 与新机制口径重复，建议收敛为一处。
- S4 gui/config_editor.py:206-208 stall_timeout hint 应补"特定阶段有内置宽限"，否则用户调阈值后发现行为不符更困惑。
- S5 MCP 文字变化无契约风险，前提 GUI 与 progress_text 同步改。

### 审查点逐条结论
1. 300s 内真 hang 显示绿色可接受：心跳线程死 → dead 通道 15s 照常告警；心跳活主线程死锁 → 用户处置手段相同，5 分钟延迟换消除每轮冷加载必误报，trade-off 正确。
2. 见 S1/S2。
3. 不加配置键成立（YAGNI）。
4. server._index_running 只看 updated_at 不受宽限影响，兼容性好；剩余风险即 B1/B2。
5. 更省替代方案不够：纯文案不给确定性判断；一刀切调大 STALL_TIMEOUT 会钝化真检出且盖不住 converting 600s。分相位自过期宽限是最小正确解。

### 实测位置
gui/store.py:132-150、gui/widgets.py:141-213,222-226、gui/app.py:292-304、index.py:173-190/267-316/356-416、server.py:66-80、config.py:43-44,83、extractors.py:486-530、TASK_LOG 问题24、tests/test_gui_store.py:318-330。
