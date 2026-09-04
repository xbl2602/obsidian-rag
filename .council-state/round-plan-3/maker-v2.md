# Maker 方案修订版 v2（stall_grace_until 自过期宽限字段）

（本文件为 council 方案门第 1 轮修订的完整方案文本，供复审委员读取。）

## 1. 目标

给索引进度报告引入自过期、默认自清的宽限字段 `stall_grace_until`（绝对截止时间戳），让"模型加载 / 写锁等待 / 写库清理"这类合法长静默不再被判停滞误报，同时满足四条硬约束：
1. 宽限绝不掩盖心跳停止（DEAD 判定永远优先）；
2. 宽限绝不跨任务/跨进程残留（升级过渡期不劣于现状）；
3. 埋点侧绝不死锁、绝不复活死任务（两段式快照守卫）；
4. 宽限期内 UI 不是无声绿色（信息行 + 心跳胶囊 note 双补偿）。

不改切块/清洗逻辑、不改提取逻辑 → META_VERSION / EXTRACT_VERSION 均不动。

## 2. 根因清单（重编号，标注对应 G 项）

| # | 根因 | 对应 |
|---|---|---|
| R1 | update_progress 合并语义（index.py:276-277），不带 kwargs 字段原样保留 → 加载完宽限残留；server 长驻多库连跑跨任务残留 | G1 |
| R2 | 后写者胜：先写 300s 被后写 180s 反向缩短 | G2 |
| R3 | converting 置位（index.py:1431）后三个未还原出口：1434-1440 失败 / 1444-1448 空 body / 1548-1551 切块 → 白名单泄漏 | G7 |
| R4 | write_lock()（index.py:1261 排队上限 60s > STALL_TIMEOUT 25s）等锁必误报；无变更路径直达时 phase 还停在 scanning | G5 |
| R5 | 时间源不改（维持原判） | 已结案 |
| R6 | server._index_running 只看 updated_at 新鲜度；只要守卫不碰非本进程进度记录即无需独立改动 | G3 前提 |

## 3. 机制规格

### 3.1 字段定义
- JSON 键：`stall_grace_until`（float，epoch 秒绝对截止）
- 写入 kwarg：`stall_grace_s`（相对时长，永不落盘）
- 常量（index.py :187 后，硬编码不进 config）：
  - STALL_GRACE_MAX_S = 600.0（clamp 上限）
  - STALL_GRACE_MODEL_LOAD = 300.0
  - STALL_GRACE_WRITE = 180.0

### 3.2 状态表
| 事件 | 结果 |
|---|---|
| progress_start（328-338） | 显式清空：锁下 _progress.pop("stall_grace_until", None)，与 update_progress 默认 pop 双保险 |
| update_progress(**fields) | ① pop("stall_grace_s")；② prev=base.get(until)；③ base.pop(until)（默认移除）；④ grace_s 正数才写回 until=max(prev, now+min(float(grace_s), MAX))；⑤ 非数值/≤0 不写（fail-closed） |
| 心跳 tick / _report_device | 整表拷贝字段原样保留（宽限在静默窗口内存活靠它们） |
| progress_finish/error | 常规更新→字段移除，终态永无宽限 |
| 判定侧读取（双实现同一表达式） | 有效当且仅当 running 且 isinstance(v,(int,float)) 且 now<v；非法值视为无宽限；DEAD 先于宽限 |
| 强杀残留文件 | updated_at 冻结 → DEAD/stale 分支照常命中，宽限无复活通路 |

### 3.3 _stall_grace 助手（两段式，复刻 _report_device index.py:452-457 先例）
```python
def _stall_grace(seconds, **fields):
    with _progress_lock:
        snap = dict(_progress)
    if not snap.get("running"): return      # 无运行中任务不产生噪音
    if snap.get("pid") != os.getpid(): return  # 别的进程的进度绝不接手
    update_progress(stall_grace_s=seconds, **fields)  # 锁外调用
```

### 3.4 判定侧伪码（两侧互指注释，双实现一致）
- index.progress_text：DEAD 分支原文不动 → converting 豁免行原文不动 → elif in_grace 输出宽限信息行（含 PID+已安静秒数+剩余秒数，不含告警字样）→ stalled 告警原文 → 正常行。elapsed<300 启动 tip 与新机制收敛口径。
- gui/store.heartbeat_state：HB_DEAD 先于一切 → converting 白名单保留 → in_grace 返回 HB_RUNNING（色/呼吸不变）→ stalled。

## 4. 改动清单（逐文件）

### index.py（7 处）
1. :187 后新增 3 个 STALL_GRACE_* 常量
2. update_progress :267-292 按 §3.2 重写（pop kwarg / 默认清除 / max 合并 / clamp / fail-closed），docstring 注明一次性豁免语义
3. progress_start :328-338 锁下显式 pop（双保险）
4. progress_text :393-415 分支重排 + elapsed<300 tip 口径收敛
5. 新增 _stall_grace 助手（_report_device 附近）
6. 四个埋点：
   ① get_model() 缓存未命中实际加载分支内（cuda 路径 :570 后、cpu 路径 :582-583 同样处理）；禁止放函数入口
   ② _try_switch_back_cuda 入口（:543 old=_model 之前）
   ③ fallback_to_cpu 收尾（:709 之后）
   ④ :1576-1577 改为 update_progress(phase="waiting-lock", message="等待写锁...", stall_grace_s=WRITE) → with write_lock(): → update_progress(phase="writing", ..., stall_grace_s=WRITE)；waiting-lock 为新增 phase 值（已核对全部 phase 消费方安全）
7. :1433 extract_to_markdown 返回后单点还原 phase="scanning"（覆盖三出口）

### gui/store.py（2 处）
1. heartbeat_state 插宽限分支 + 升级过渡期 docstring + 互指注释
2. 新增纯函数 heartbeat_note(progress, running)：converting→转换文案；宽限有效→"模型加载/写库中（宽限内）"；否则 None

### gui/app.py（1 处）
- :292-297 内联三元换 hb_note = heartbeat_note(...)；widgets.py 零改动

### gui/config_editor.py（1 处）
- stall_timeout hint 追加"；特定阶段（转换/模型加载/写库）有内置宽限"

### server.py / extractors.py / config.py / widgets.py
零改动。

## 5. 测试计划

### tests/audit_regression_test.py
- test_stall_grace_cleared_by_normal_update（G1①）
- test_stall_grace_no_cross_task_residue（G1②）
- test_stall_grace_merge_takes_max（G2）
- test_stall_grace_guard_running_and_pid（四分支：空内存不写/异 pid 不写且 PROGRESS_FILE 字节不变/同 pid 写入≈now+s/残留文件+空内存→原样）（G3）
- test_stall_grace_helper_no_reentrant_deadlock（行为断言 update_progress 调用时锁已释放 + 结构断言不在 with _progress_lock 块内）
- test_stall_grace_type_defense_and_clamp（写侧 "300"/-5 不写、10000 clamp≤600；判侧 "abc"/None 照常告警）
- test_stall_grace_kwarg_never_persisted
- test_progress_text_grace_states（三断言：宽限内信息行含 PID+安静秒数无告警字样 / 过期恢复告警 / 宽限内但心跳冻结仍 DEAD）
- test_progress_text_converting_whitelist_without_grace_field（G8 升级过渡边界）

### tests/test_extractors.py
- test_converting_phase_restored_after_extract（R3/G7 锁定：spy 包装 update_progress 记录快照序列，断言 converting 后紧跟 scanning、embedding/writing 无 converting 残留 + 结构断言还原语句位置）

### tests/test_gui_store.py
- test_heartbeat_grace_running_then_expired_then_dead（宽限内 RUNNING / 过期 STALLED / 宽限内心跳停 30s DEAD）
- test_heartbeat_grace_invalid_types_fail_closed
- 扩展既有 test_heartbeat_converting_stall_is_not_stalled（p3 断言保留补注释 + converting+缺宽限字段仍 RUNNING）
- test_heartbeat_note_grace_vs_converting
- 双看门狗一致性：GUI 用例与 audit 的 grace_states 用同一组样本断言两侧结论一致

### 明确不加
MinerU 真实云端调用（人工冒烟纪律）、embedding 单批超时（范围外）、test_config_editor 新用例（hint 变更对既有断言透明）。

## 6. 步骤顺序与风险
七步：常量+update_progress 改造 → 助手 → progress_text 重排 → 四埋点+G7 还原 → store 判定+note → app/config_editor → 六件套+文档。
风险：默认 pop 是全局语义变化（docstring+防呆用例兜底）；waiting-lock 新值外部脚本可见（仓内消费方全核对）；双实现漂移（互指注释同样本用例压住）；检测延迟 25s→300s 由 G9 双补偿对价；embedding 单批>25s 仍暴露（现状如此，批次间有事件）。

## 7. 规格澄清附录（G8）

### 7.1 升级过渡期矩阵
| 场景 | 旧代码读新文件 | 新代码读旧文件（缺字段） |
|---|---|---|
| converting 大文件/云端 OCR | 豁免（既有白名单） | 豁免（白名单无条件，不依赖字段） |
| 模型加载 120s | ⚠误报（现状 bug） | ✅宽限覆盖 |
| 写锁排队 60s | ⚠误报 | ✅宽限覆盖（waiting-lock 可见） |
| 其余相位真停滞 | 告警 | 告警（or-0 回退旧行为） |
| 心跳停止 | DEAD | DEAD（先于一切豁免） |

结论：任意方向版本混跑都不比现状糟。

### 7.2 MinerU 600s 覆盖链声明
600s 级安静期完全在 extract_to_markdown 内部，phase 必为 converting → 白名单无条件豁免 → G7 单点还原保证一返回立即退出 converting，豁免窗口严格闭合。MinerU 不需要也不得加宽限埋点。锁定用例：converting_whitelist_without_grace_field + GUI 侧扩展断言。

## 待确认
① C2/C3 编号映射（maker 推断 C2=机制正确性 C3=GUI 区分，若与验收标准有出入请编排者校正）；② waiting-lock 新 phase 值已采纳，product 若认为可省可退化为仅写宽限。
