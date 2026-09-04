# Reliability 评审（方案门 第1轮）

## 总体判定：BLOCKER（3 项阻断）
根因分析 R1–R6 全部实测核实成立，方向正确；但三个可靠性细节按现状落地会产生新问题。

### Blocker 清单

**B1 宽限字段没有任何清除语义 → 豁免泄漏跨相位/跨任务**
- update_progress 是合并语义（index.py:276-277），不带 kwargs 原样保留。模型 10s 加载完后，后续每批更新都不清宽限 → 之后真实假活最长 300s 完全豁免；server 长驻进程多库连跑（server.py:100-111），上一轮 writing 宽限残留进下一轮。
- 修复要求：progress_start 显式清空宽限字段，且常规 update_progress 不携带该 kwarg 时默认删除该键（天然把豁免限定在"埋点→下一个正常进度事件"区间）；补两条测试（常规更新清除宽限、跨任务无残留）。

**B2 _stall_grace 助手的锁序未钉死 → 高概率自死锁**
- _progress_lock 是不可重入 threading.Lock（index.py:184），update_progress 自己在锁内（index.py:275）。若实现成 with _progress_lock: if running: update_progress(...) 就是同线程重入死锁，心跳线程随之永久阻塞，所有看门狗通道瘫痪。
- 修复要求：两段式——锁内读快照判 running（参照 index.py:452-454 _report_device 读法）、锁外再调 update_progress(stall_grace_s=...)；测试覆盖 running=False 时不写进度文件。

**B3 _try_switch_back_cuda 埋点位置错误：「收尾」盖不住真正的静默窗口**
- 数十秒静默发生在函数体内 _load_model("cuda")（index.py:546），收尾才写宽限 = 加载期间照样误报，加载完成后反而凭空多 300s 豁免（与 B1 叠加）。对照 fallback_to_cpu 收尾写是对的（其静默重载发生在调用方 658-660）。
- 修复要求：_try_switch_back_cuda 埋点移到函数入口（_load_model("cuda") 之前）；两个函数不能共用"收尾"一个说法。

### 非阻塞建议
- S1 R3 修漏一个出口：converting 置位后有**三个**退出点（1439 失败 / 1444-1448 空 body 防御 / 1551 切块）。更稳做法：extract_to_markdown 返回后单点还原 phase="scanning"，一处覆盖全部出口。
- S2 300/180 建议进 config.py（与 cooldown=300 对齐是依据；180s 低配机可能偶发超出，失败良性）。
- S3 宽限信息行建议附 PID+已宽限 Xs；GUI 宽限期胶囊只显示普通 running 无解释文案，可选补齐。
- S4 时间回拨拉长宽限（有界）、休眠恢复朝安全方向，记录备注即可，不必 monotonic。
- S5 index.py 侧 progress_text 读宽限也要 or 0 兼容，两侧表达式逐字镜像。
- S6 跨模块快进时钟要分别 patch time；fake torch 注入可行性已核实（torch 全懒加载 index.py:466,578,608,627,645,702）。
- S7 既有盲区备案：CPU 慢机单批 >25s 的批次内停滞误报仍存在（问题13 已知取舍），勿在本方案扩大战场。

### 实测位置
index.py:183-191/248-264/267-292/295-325/356-416/437-457/523-585/650-709/1316-1326/1362/1428-1551/1561-1614、gui/store.py:132-150、gui/app.py:292-296、gui/widgets.py:147,222、server.py:66-80、config.py:35/42-44/83/399-405、tests/test_gui_store.py:27-60,318-330、TASK_LOG 问题13/23/24、extractors.py:486-530。
