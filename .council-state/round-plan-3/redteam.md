# RedTeam 评审（方案门 第1轮）

## 总体判定：BLOCKER（阻断 5 条）

### Blocker 清单

**B1 写锁等待窗口零覆盖——方案声称修"误报"却留下必现的合法安静期**
- 时序：P1 持写锁大批量 upsert 数分钟；P2（GUI 子进程索引）跑到嵌入尾段，最后 update_progress 在 index.py:1571，随后 1577 调 write_lock() 排队。lock_timeout_seconds=60 > stall_timeout=25。P2 等锁 ≥25s：心跳照跳但 last_advance_at 停留 → GUI 判"进度停滞（假活）"。
- 关键证据：phase="writing" 与宽限埋点都在获锁之后；且无变更路径（new_ids 空）直达 1577 时 phase 还停在 "scanning"——连 writing 宽限都够不着。
- 修复要求：write_lock() 调用前写宽限/置 phase（或在锁轮询循环内周期 touch）。

**B2 _stall_grace 的 running 守卫数据源未定义——按文件判定会复活陈旧进度文件并拖垮 _index_running 放行**
- 时序：GUI 子进程被 gui/stop.py taskkill /T /F 强杀（正规停止路径！）→ 进度文件残留 running=True、updated_at 冻结。T+20s server 进程检索触发 fallback_to_cpu → 埋点若按文件判定（read_progress().running 为 True）→ update_progress 无条件刷新 updated_at/last_advance_at=now → 死任务显示健康运行长达 300s；server.py:74-79 _index_running 的"心跳停 15s 放行新任务"被推翻 → 新任务被多挡约 285s。
- 修复要求：守卫钉死 = 内存 _progress.running 且 pid == os.getpid()（本进程拥有当前任务才写）。

**B3 _try_switch_back_cuda 宽限写在"收尾"，盖不住它自己的加载过程——冷却到期自动切回场景 25s 必误报**
- 卸载 CPU 模型 → _load_model("cuda") fp16 尝试+dtype 回退 fp32 是串行双次加载（index.py:523-537），60-120s+ 安静。宽限在收尾 = 加载期间照判 stalled。
- 修复要求：进入函数至少在 _load_model("cuda") 之前先写宽限，收尾再续。

**B4 stall_grace 合并语义未指定——"先 300 后 180"会缩短已有宽限（回退攻击）**
- base.update(fields) 是后写者胜。反例：嵌入尾段慢批二连降级 → fallback 写宽限至 T+300；紧接着 writing 写 180s 至 T′+180 < T+300 → 宽限反向缩短，writing 真卡死提前误报。
- 修复要求：合并取 max(existing_stall_grace_until, now + s)，方案明文写出。

**B5 _stall_grace 按字面实现会自死锁**
- _progress_lock 不可重入（index.py:184），update_progress 在锁内再取锁（index.py:275）→ 主线程冻结、心跳线程阻塞在 acquire → updated_at 冻结 → 15s 后全线判 DEAD。把偶发误报升级成必然全线误报。
- 修复要求：助手不得持锁调 update_progress，复刻 _report_device 快照模式（index.py:452-457）或换 RLock。

### 攻击面结论（A1-A7）
- A1 成立可接受但方案隐瞒展示层后果：宽限期 stall 是该窗口唯一探测器，检测延迟 25s→300s（12×），UI 完全静默失去感知 → 要求宽限期内显示"宽限中（模型加载/写库）"。
- A2 部分成立：MinerU 轮询在 converting 相位受既有豁免覆盖 ✔；未覆盖窗口=①锁等待(→B1) ②switch-back 加载期(→B3) ③单巨文件切块/首建 client+count（既有缺口非方案引入，应声明范围）。
- A3 成立（条件依 B2）；按内存实现则闭合。
- A4 成立（→B4）。
- A5 部分证伪：每文件处理完都有 update_progress 推进，扫描流不误报；R3 未制造新窗口。
- A6 C3 担忧证伪（torch 全懒加载注入有效 index.py:466,578,608,702）；C2 纯函数测试绕过真实渲染链路属有限信心，可接受但点名。判定逻辑是双实现必须两处同步修改两处测试。
- A7 证伪"server 侧无独立盲区"：B2 场景中 grace 写入刷新 updated_at 会把残留放行窗口从 15s 拖到 ~300s。

### 非阻塞建议
1. 300/180 维持硬编码则收敛为 index.py 单一常量并由 store.py import。
2. 进度文件存绝对时间戳 stall_grace_until 而非秒数（方案已如此，确认正确）。
3. 补防呆断言：stall_grace_s 必须 pop 干净不许漏进 JSON。
4. 顺手补 index.py:1444-1448 防御分支 phase 重置。
5. B1 若采用提前置 phase 建议独立值 waiting-lock 更诚实方便 GUI 区分。
6. _IsoEnv 端到端用例：grace 内不判 stalled → 过期恢复判定 → DEAD 全程不受 grace 影响（三断言一条用例）。
