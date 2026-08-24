# RedTeam 评审 — round-plan-1

## 🔴 Blocker

**B1｜TBD 占位重的 PDF 造成「每次搜索都判新增 → 永久重建」死循环**
- 序列：config tbd_exclude_ratio 默认 0.1 开启；库 extensions=md,pdf 且 MinerU 已装；放入 TBD 行占比 ≥10% 的 报告.pdf → _index_core tbd 分支 continue 不进 current_rels → L1274 裁剪 → meta 永不落条目；下次 kb_stale：entry=None → 方案把 is_tbd_heavy 移入 content 非 None 分支，而 pdf 走 extract=False 返回 None → TBD 检查被跳过 → added+=1 → stale=True → 后台增量索引重演 → **每次 search_knowledge 都返回"新增 1 个文件"，永不收敛**。
- 为什么成立：md 无此问题（现行 kb_stale 在 added 计数之前做 TBD 判定并 continue）；方案把判定挪进 content 非 None 分支后恰恰只对非文本源拆掉了这道闸。
- 修法：_index_core 的 tbd 分支对非文本源也落 chunks:0 条目并加入 current_rels（与 xfail 同构）；或 kb_stale 对 entry 为 None 的非文本文件不计 added。

**B2｜方案指定的分支顺序让首个提取失败的文件炸掉整库索引（AttributeError）**
- plan.md「失败分支在 tbd 分支后」字面实现：content=None 先流经 is_tbd_heavy(content) → index.py:55 content.splitlines() 抛 AttributeError → L1328 progress_error + raise → 整个 _index_core 中止，xfail 机制根本执行不到。
- 可复现：库里放一个打不开的 pdf + 任意 md，跑 python index.py --library X。
- 修法：xfail 分支先于 tbd 分支，或 tbd 判定加 content is not None 守卫。

**B3｜大小写扩展名在三处路由点未规定 .lower()，.PDF/.MD 被静默打成永久 xfail**
- collect_md_files 做了小写归一所以大写扩展名一定进 files 列表；但 _load_text 路由、extractors 分发、frontmatter 门控三处都没写大小写规则 → Scan_2026.PDF 落入"其他后缀→None"、Note.MD 被送进 extractors 返回 None → 写成 xfail 条目后字节不变快速路径永远命中 → 永不重试静默不可检索。测试无用例覆盖大写扩展名。
- 修法：方案明文三处一律 suffix.lower()，测试补大写用例。

**B4｜「check-notes 已透传自动受益」是错的：extensions 加 pdf 后 tools/check_notes.py 直接崩溃**
- scan_library 对收集集逐个 p.read_text(encoding="utf-8")（tools/check_notes.py:217 errors 默认 strict）→ 真实二进制 pdf 必抛 UnicodeDecodeError，L218 只捕 OSError → 工具带栈崩溃。L207 collect 确实透传了 extensions，pdf 一定被送进这条文本分析路径。
- 修法：scan_library 对非 TEXT_SOURCE_EXTS 跳过正文分析，或改回「需适配」清单。

## 🟡 建议
R1 缓存键不含提取器版本/backend 名：换 backend 或升级 pymupdf 后 --full 全部命中旧缓存零效果；R2 空串提取是 B1 第二扇门（pymupdf4llm 对退化 pdf 可能返回 ""）；R3 timeout=600 写死，大部头数百页 OCR 超 600s 永久 xfail；R4 缓存目录无清理（孤儿 tmp 文件）；R5 converting 相位只换标签，停滞看门狗看 last_advance_at 未触及，需推进 advance 时间戳才真防误报；R6 GUI 与 server staleness 口径本就不一致（gui/store.py:69-77 不传 tbd_ratio），B1 循环会放大成双双永动；R7 mineru 子进程抢显存窗口有 CPU 降级兜底可接受。

## ✅ 认可（证伪未遂）
Q2 同名不同扩展名 rel 天然隔离无碰撞；Q6 死代码判断正确且 xfail 条目 tbd:False 自然旁路 L914 不会被误跳过；Q5 export/import 往返 manifest 原始字节 sha256 自洽、集合比对修法必要；META_VERSION 8→9 覆盖哈希语义迁移且既有测试动态取值不受影响；缓存原子性与源目录零写入成立。

## 结论
有 blocker（4 项）：B1 永久重建循环 / B2 指定顺序崩溃 / B3 大小写静默 xfail / B4 check-notes 崩溃+豁免论据失实。均可在方案层修复，须修订后再进实现。
