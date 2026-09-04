# Architect 复审（方案门 第2轮）

## 总体判定：BLOCKER（阻断数 1，纯测试计划补充）

## 原 blocker 复核
- B1 → RESOLVED：v2 补 R3 锁定用例（spy 快照序列 + 结构断言），单点还原比第 1 轮要求的钉两处更优；放 test_extractors.py 合规（_IsoEnv 基建在彼处）。
- S2 → RESOLVED（progress_start 显式 pop 双保险）；S4 → RESOLVED（waiting-lock 宽限 180 > 锁超时 60 全覆盖）；S6 → RESOLVED（守卫四分支归位 audit 文件）。
- S5 → NOT RESOLVED（部分）：埋点②③仍未指明宽限常数，应显式 STALL_GRACE_MODEL_LOAD=300。

## 编排者补充项核实
- A（C3 用例缺口）→ **确认存在**：v2 §5 无任何 CUDA 用例，而改动触碰 index.py:543/570/582-583/709 四处冷却切换路径。要求恢复六条进 audit_regression_test.py：①冷却内零探测②过期探测失败重武装③过期成功不武装④fallback_to_cpu 状态⑤换回双向含异常回滚⑥running 中降级联动写宽限。
- B（waiting-lock 消费方）→ 全部安全降级无崩溃：widgets.set_phase idx=-1 兜底、PHASE_COLOR.get 默认值、progress_ratio 走 else、server 无 phase 消费。瑕疵：app.py:443-444 KPI 裸显英文「阶段：waiting-lock」，建议补中文映射。

## 建议（不阻断）
- progress_start 的 pop 不得持锁调 update_progress（死锁），建议结构断言。
- 埋点②旁注释说明覆盖切回失败回滚全程，防后人误删。
- heartbeat_note 放 store.py 职责正确；app.py 单行替换符合零侵入红线。

## 结论
机制设计全部达标，唯一 blocker 为 C3 六用例恢复；补齐后编排者核对用例清单即可放行实现，无需再走全轮复审。
