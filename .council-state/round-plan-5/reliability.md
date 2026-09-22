## Reliability 评审（可靠工 · 只读审计 · 咨询模式不改代码）

```
🔴 硬伤:
B1 mineru_server.py:229-236 + 425-433 + 334-380 — 空闲卸载/_check_idle_unload 与 POST /evict 只持 _INNER_LOCK，
   长解析的网络等待期（do_parse 持 _API_LOCK 做 urllib 等待，不持 _INNER_LOCK）可被中途停内服务，
   在途 parse 失败落 extract-failed 终态（非 deferred），与注释“等当前一份走完再停”不符；
   且 _last_use 在长解析期不刷新（仅 _ensure_inner 入口与成功返回处刷新），默认 300s 空闲必误杀超长文件
   （单文件超时公式 300+30×页数，200 页可达约 105min）。对比 wemm_server.py:320-322 + 387-388
  （/evict 与 encode 同持 _ENGINE_LOCK，真等待当前编码）即为正确样板。

🟡 建议:
B2 extractors.py:1296-1299 + index.py:2037-2043 + index.py:2135-2137 — 云端超时/download 保留 pending
   意图“下轮续接”，但 index 对同 xsrc 终态 O(1) 快速收敛（size+mtime 未变即 unchanged 跳过），
   续接分支（_mineru_cloud_extract 内 _pending_match）下一轮根本到不了；且 pending 修剪以本轮
   cloud_jobs 为 alive 集，超时文件不在其中时条目反被当孤儿清掉。 transient 超时于是变成
   准永久终态（需文件 mtime 变化或后端签名变化才解）。建议与本地 deferred 对齐：超时/download
   走“不落终态”（或索引侧 pending 存在时穿透快速路径），否则“保留条目下轮续接”注释是空承诺。
B3 index.py:2027-2031 + wemm_indexer.py:303-307 — stat OSError 分支 continue 时未先 current_rels.add(rel)，
   瞬态 stat 失败（AV/索引占用）会导致 meta 条目被结尾裁剪、其块被当 stale 删除（下轮回填自愈，
   但造成无谓删建抖动）。对比同文件 _load_text 不可读路径（先 add 再落 unreadable 终态）即为正确样板；
   建议 stat 失败也先 add（无条目则不建条目但保留旧条目）。
B4 index.py:2326-2329 — prune 存活集只收 index_meta_<name>.json 与 pending md5，未收 legacy 基线
   index_meta.json（index_vault 旧单库入口仍写它）的 hash；legacy 路径在用时其提取缓存会被当孤儿误删
   （可重提自愈，仅浪费）。建议 keep 并入基线 meta 或在注释中明确“legacy 路径 prune 前先迁移”。
B5 wemm_indexer.py:166-173 — page_count() 的 finally 内 doc.close() 未包 try/except，
   违反 AGENTS.md 红线 1 同款要求（extractors.py:708-712 与 classify finally 已做对）；
   目前无调用方（grep 仅定义），属 latent，建议补齐或删死代码。

🟢 备注:
G1 extractors.py:708-712 + 1039-1043 + 702-707 — _extract_pdf 外层兜底折叠 + finally 包裹、
   classify finally 包裹，红线 1 成立；_load_pretrained（index.py:609-623）仅捕 OSError 回退联网，
   其余异常上抛由调用方正确处理（CUDA→CPU / reranker 降级），无旁路（retriever 共用同一入口）。
G2 index.py:2102-2109 + tests 钉住 — 本地 deferred（服务拉不起/中途死亡）不落终态不动 meta，
   云端 Token 失效取消任务不落终态（2163-2167），fail-open 成立；_wemm_auto_phase（1776-1779）
   全捕捉只记日志，文字索引不被页库波及，成立。
G3 index.py:372-386（wemm）upsert 全成功才 meta.update(pending_ok) — “写库成功才落 meta”成立；
   部分失败当轮 valid 集不含新页使其自清、下轮幂等重做（upsert 同 id 覆盖），仅多耗一轮编码，接受。
G4 server.py:72-100 空闲卸载护栏 — 检查 read_progress running 并 touch 跳过（索引期间不误杀），
   limit<=0 常驻语义成立；检索并发与卸载竞态 benign（全局置 None 不杀在用局部引用，下次懒加载）。
   gpu_arbiter fail-open 全链成立：vram_free_gb 探测失败返 None（37-66），wait_for_vram 见 None 即放行
   （81-83），_vram_maybe_evict_wemm 见 None/足够即返（694-730 全 try 包裹），evict_* 失败返 False。
G5 extractors 日志不含 Key：_mineru_cloud_extract:1225/1302-1308 只记类型摘要；wemm/mineru 服务日志
   仅文件名与类型（wemm_server.py:38-39，mineru_server.py:31），成立。
G6 单轮单次懒拉起（wemm_indexer.py:260-278 tried/ready）与 before_serve 仅真渲染触发（268-272），
   成立；prune 双触发点 had_error 跳过一致（server.py:188-192，index.py:2450-2456 failed 即 exit 不回收），成立；
   _pending 跨进程仅线程锁（last-writer-wins 丢条目→下轮重复提交烧配额，不影响正确性），接受。
G7 真库基线/冷加载秒数按编排者裁决仅做逻辑确认：_load_pretrained 离线优先仍在（index.py:609-623，
   retriever.py:251-255 经同一入口），0=常驻语义三处一致（config 37 / server 85-86 / 两 server idle-exit 0），
   不插桩、不新增口径。

结论: 咨询模式，仅分级记录，不设通过/打回。硬伤 1 项（B1），建议 4 项（B2–B5），备注 7 项。

摘要：
- 审计面：fail-open、终态完备、extractors 不抛、prune 存活集/had_error、upsert→meta 顺序、空闲卸载护栏、MinerU 断点续接，7 项全读完。
- 总体：fail-open 链条与终态机制基本成立；双触发点回收门禁一致；加载全穿 _load_pretrained。
- 唯一硬伤是 MinerU 壳服务的卸载/抢占锁粒度：空闲 300s 会杀死长解析在途任务并落错终态。
- 另有云端超时续接意图与索引收敛冲突、stat 瞬败裁剪、prune 漏 legacy 基线、page_count finally 漏包四项建议。

发现清单：
- [🔴] mineru_server.py:229 — 空闲卸载/evict 不等在途解析，瞬态失败误落永久终态（对比 wemm_server.py:320）
- [🟡] extractors.py:1296 — 云端超时保留 pending 但索引收敛致续接不可达（配 index.py:2037）
- [🟡] index.py:2027 — stat 失败未先 add 进 current_rels，瞬态失败致条目裁剪块删（同 wemm_indexer.py:303）
- [🟡] index.py:2328 — prune 存活集漏 legacy 基线 index_meta.json 的 hash
- [🟡] wemm_indexer.py:166 — page_count finally 内 close 未包 try（红线 1 样板违反，暂无调用方）
```

（注：委员会话无 write 工具，原文以文本返回，由编排者代落盘。）
