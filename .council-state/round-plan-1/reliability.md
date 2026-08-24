# Reliability 评审 — round-plan-1

## 七问速答
Q1 异常分类学四类中三类闭环（extractors 内部兜底/subprocess 超时/meta 写盘自愈链），一类破防=read_bytes OSError 向上传播 × 主循环无逐文件隔离（B1）。Q2 xfail 三态机收敛成立，但有两个洞：空串绕过状态机（B2）、插入点照字面实现会先炸（S5b）。Q3 subprocess 层语义对但 Windows 树杀/临时目录/句柄收尸未指定（S2）。Q4 半写缓存由 os.replace 排除✅；孤儿缓存纯磁盘浪费低优 GC。Q5 并发互扰面为零✅；Windows replace 打开中的文件会 PermissionError 见 S5a。Q6 从头再来可接受✅，但 timeout 型 xfail 永久化唯一逃生通道是 --full 且无人知晓（S3）。Q7 gui/store.py:131-142 heartbeat_state 是相位盲的第二套看门狗，OCR 超 25s GUI 必报 HB_STALLED → 用户树杀 → 杀在提取段则该文件永远得不到 xfail 条目 → 下轮重试再被杀的谋杀循环（S1）。

## 🔴 阻断

**B1** read_bytes OSError 向上传播 × 主循环无逐文件隔离 = 单文件废掉整轮 + 无限崩溃重启环
- 场景：PDF 被 Acrobat/Word 独占打开、OneDrive 按需占位文件、AV 实时扫描锁 → PermissionError 冲出 for 循环 → 仅被 L1328 整体捕获 re-raise → 本轮全部已处理文件的切块嵌入作废（save_meta L1324 在循环后）。放大链：server.py:118-120 把同类异常折算成 stale=True → 后台索引 index.py __main__ L1356-1359 无逐库隔离再次崩溃 → 下次检索又判 stale → 每轮白付模型加载的无限环。MD 时代此路径罕见，pdf/docx 是锁竞争高发区。修法：_load_text 调用点逐文件 try/except OSError → 按 xfail/skip+log 处理；kb_stale 循环同理。

**B2** 空串提取结果绕过 xfail → 永久 added 抖动死循环
- 契约允许返回 ""（加密 PDF、覆盖率≥0.5 但 pymupdf4llm 产出空白的退化 PDF 是现实输入）→ 走过 xfail 分支 → 空正文守卫裸 continue 不落 meta → kb_stale 每轮计 added≥1 → ensure_fresh 每次搜索拉起后台索引。A6 只测了坏 pdf→None 形态没测空串。修法：_load_text/extractors 边界把 falsy 结果归一为 None + 补测试。

## 🟡 建议
S1 converting 豁免必须同时覆盖 gui/store.py:140 heartbeat_state（否则 server 说正常 GUI 弹卡死矛盾信号）；更稳做法是转换期发页级子进度 tick 保持看门狗真实检测力。S2 Windows Popen.kill 非树杀，孙进程存活占显存 → taskkill /T /F 兜底（gui/stop.py:68 先例）；临时目录删除 try/finally；ocr_cache 启动清扫按年龄；kill 后 proc.wait() 收尸防句柄泄漏。S3 xfail 条目加 reason 字段区分 timeout/corrupt，跑完汇总打印"N 个文件提取失败"；至少文档写明 --full 是逃生通道。S4 缓存键掺入 EXTRACT_VERSION+backend（与审计 F14 同构的坑）。S5a _cached 必须保证缓存写失败不影响返回值（Windows 上另一进程正读 <md5>.md 时 os.replace 抛 PermissionError，若被顶层兜底吞成 None 会把成功提取错打成 xfail）。S5b 执行序钉死：None 判定严格先于 tbd 判定。S6 index.py:1356-1359 多库循环无逐库 try/except（server 侧有 CLI/GUI 侧没有），一库连坐后续库，顺手补齐。

## ✅ 认可
meta 写盘失败中间态由双重自愈覆盖；心跳 daemon+_pid_alive 复核陈旧 running 不挡新任务；gui worker drain 管道无死锁；CREATE_NO_WINDOW/errors=replace 到位；写锁外提取缩短持锁时间；LockBusyError 语义完好。

## 结论
有 blocker（阻断数 2：B1 逐文件 OSError 隔离、B2 空串归一 None），两者修法很小但都直接击穿「xfail 防死循环」核心承诺，须修完再审。
