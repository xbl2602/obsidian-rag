# Architect 评审（方案门 第1轮）

## 总体判定：BLOCKER（阻断数 1）

### Blocker 清单

**B1 测试计划漏掉 R3 修复的锁定用例（违反 AGENTS.md 测试纪律）**
- 根因清单自评 R3 为「高：真停滞漏报」，改动第 4 条修它（index.py:1431 置 converting 后永不恢复；拟在失败分支 1439 与成功切块分支 1551 补 phase="scanning"），但测试计划没有任何用例覆盖这个修复。
- 一句话修复要求：补一条用例锁「转换完成后相位必须回到 scanning、此后宽限期外的停滞恢复告警」（最低限度配源码断言钉住 index.py:1439/1551 两处补丁），纳入六件套。

### 非阻塞建议
- S1 双判定侧镜像逻辑应抽共享纯函数（gui/store.py:11-18 已有 import index 先例）；本变更可不做，但应留注释钩子。
- S2 progress_start 应显式清 stall_grace_until（update_progress 是合并语义 index.py:275-277，progress_start 328-338 不清旧字段；server 后台背靠背多库循环会残留上一库 writing 宽限）。
- S3 R3 修复在空 body 防御分支（index.py:1444-1447）也有残留泄漏，顺手补第三处。
- S4 writing 宽限埋点建议提到 write_lock() 之前（最后一次嵌入更新 1571 到拿锁 1577 之间隔着 vstack+锁等待，最长 LOCK_TIMEOUT_SECONDS=60s > STALL_TIMEOUT=25s，仍会误报）。
- S5 换回 CUDA 的宽限时长未指明，应显式复用 STALL_GRACE_MODEL_LOAD=300。
- S6 _stall_grace 守卫用例放错文件：应合并进 audit_regression_test.py，不要塞 test_extractors.py。

### 对审查重点的正面回答
1. 宽限字段方案取舍合理，是最懒的正确解。
2. MinerU 轮询在 converting 相位窗口内受既有豁免覆盖，不需另埋点；真正的问题是 R3 泄漏。
3. 硬编码边界判断正确（内部上报启发式非用户调参面，沿用 SLOW_BATCH_SECONDS 先例）。
4. 无过度设计；欠设计即 B1 与 S2/S3，均为补丁级。

### 实测位置
index.py:183-192/267-292/295-338/393-416/460-495/540-585/650-709/1362-1379/1428-1447/1551-1611、gui/store.py:132-150、gui/app.py:292-304、server.py:66-80/346-353、TASK_LOG.md 问题13/24、extractors.py:45,448-530。
