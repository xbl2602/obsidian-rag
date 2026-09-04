# RedTeam 复审（方案门 第2轮）

## 总体判定：PASS（可进实现）——唯一红线：C3 六条 CUDA 边界用例必须原文恢复

## 原 Blocker 复核（B1-B5 全部 RESOLVED）
- B1 → 正向覆盖成立；异常路径残留实测核清：LockBusyError → except Exception → progress_error(running=False)，三重净化（默认 pop / phase=error / running=False 使残值惰性化），无残留通路。
- B2 → 守卫读内存快照且内存从不从文件水合（_progress={} 进程初始态）；pid 由 update_progress setdefault 写入、progress_start 必经 → 不存在"同进程 running=True 但 pid 异值"可达状态；历史残留文件场景不可达且方向安全。
- B3 → 入口写入先于 _load_model("cuda")，fp16→fp32 双加载全程在宽限内。
- B4 → max 合并明文化 + 专测。
- B5 → 两段式无重入，行为+结构双断言。

## 编排者补充项
C3 用例缺口 → 🔴 确认成立（阻断级缺漏，静默丢失源于编号映射错位）。恢复最低集：
1. 埋点② 结构断言（入口、先于 _load_model）
2. 埋点③ fallback 收尾标记存在性与顺序
3. 埋点① 双分支结构断言（在缓存未命中实际加载分支内、不在函数入口）
4. 冷却期内重复降级/切回序列下 clamp≤600 与 max 不回退
5. 无运行中任务时全部 CUDA 埋点零写入
6. waiting-lock → write_lock → writing 埋点顺序断言

## 新问题扫描
- 慢批降级链（埋点③→:1571 pop→埋点① 重写）实测不裸奔但依赖两埋点跨函数协作，脆弱无专测 → 建议埋点序列断言或严格化 C3-3 结构断言。
- 阈值漂移声明：lock_timeout_seconds 用户调到 >180s 或模型冷加载 >300s 时等锁窗/加载窗回退现状误报（非恶化），§7.1 应补一行声明。
- progress_text 宽限信息行 p.get("pid") 为 None 时格式化容错。
- 其余机制证伪未推翻：默认 pop 无回归面、waiting-lock 消费方安全实测、TOCTOU 最坏损失少量宽限、float 非数值 fail-closed 覆盖。
