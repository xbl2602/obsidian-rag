# Reliability 复审（方案门 第2轮）

## 总体判定：PASS（可进实现）

## 原 blocker 复核
- B1 清除语义 → RESOLVED：双保险落位 + 四条埋点链完整时序推演（实读 index.py），无提前打断点；跨任务残留封死。
- B2 锁序死锁 → RESOLVED：两段式锁外调 update_progress 无重入，测试双断言钉死。
- B3 switch-back 埋点位置 → RESOLVED：入口写入盖住双次串行加载；与 fallback 收尾措辞分离，两静默窗各有归属。
- S1/S5/S6 → 全部 RESOLVED（单点还原三出口；isinstance fail-closed 双侧同一表达式；测试绕开跨模块时钟 patch）。

## 专项核查
1. 合法静默窗不被中途打断：四条链上 update_progress 只出现在静默窗边界之外 ✔
2. waiting-lock 心跳照常 tick（gap ~5-10s ≪ 15s），60s 等锁无 DEAD 误判；新 phase 消费方逐一核对安全 ✔
3. heartbeat_note 纯函数方向正确

## 新问题（均非阻断）
- N1 heartbeat_note 缺 DEAD 优先短路：心跳冻结但宽限未过期时会红 DEAD 胶囊配"宽限内"文案自相矛盾。建议 note 内部 DEAD-first 返回 None 或调用方短路。一行成本。
- N2 同进程并发污染（server 后台索引中检索线程触发设备切换写宽限进索引记录）：有界、fail-open、下次进度事件即清除，备案即可。
- N3 测试缺口两条：waiting-lock 埋点接线无用例；心跳 tick 保留宽限字段无显式用例锁定。
