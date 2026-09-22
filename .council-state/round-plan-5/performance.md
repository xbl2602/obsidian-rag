## Performance 评审（完整意见原文 · 只读咨询，未改一行代码）

🔴 阻断: 无（0）。按"只报影响明确性能需求的真实瓶颈"纪律：B1单批upsert、B2每查多枚举均为真实开销，但在个人库量级下未达不可用/数据错误级别，故不升🔴，记🟡限风险。

🟡 建议:
- B1 index.py:2237 — 文字索引写库 `collection.upsert(ids=new_ids, embeddings=emb.tolist(), ...)` 单次全量，无分批。触发条件：全量重建且新块数万级（10k块×1024维≈40MB embedding + texts/metas单次tolist）。证据：同库WEMM分批1000（wemm_indexer.py:45,373）、import分批500（import.py:32,173）皆有分批注释"Chroma单批有上限，大库必炸"，唯文字路径一把梭。量级：个人库百~千块无感；万块以上峰值内存翻倍且有炸批风险。建议：复用500/1000分批（逻辑确认，不新增口径）。
- B2 server.py:112-129 — `_pending_formats` 对每个二进制格式各调一次 `collect_md_files`（line 120），而`ensure_fresh`（server.py:228-309）每次检索必经此+`kb_stale`（index.py:1447）内部又一次`collect_md_files`。触发条件：每次search_knowledge调用（无论有无变更）。量级：每查询2~4次全树rglob+stat+`decide_included`字符串运算，万文件库约0.5~2s固定附加（SSD/HDD各异）。建议：合并为单次枚举或mtime节流。
- B3 retriever.py:150-155 — `BM25.__init__` 先`[len(tokenize(d)) for d in docs]`再`Counter(tokenize(d))`，每doc分词两次（含jieba lcut+2-gram双通道）。触发条件：索引变更后首次查询（`get_bm25`缓存失效，retriever.py:183-207）。量级：N=10k×600字时2N次分词，可省一半。建议：一次分词同时产长度与词频。
- B4 retriever.py:296-331 — `_expand_parent` 对每唯一（库,文件,hp）执行一次`collection.get(where={"file": file})`（line 312）；`parent_cache`（line 409）只去同查询内重复，次数仍与命中节数成正比；候选窗`top_k×4`（line 31,721）放大候选即放大此开销。触发条件：正文模式+small_to_big命中多节时；advice规则6正鼓励top_k调大到15~20，开销同比放大。量级：每节一次全文件docs拉取。建议：维持现状+记录上限（个人库可接受）。
- B5 index.py:302-371 — `update_progress`每次调用即`tmp写+json dumps(indent=2)+os.replace`（`_write_progress_file` line 277），扫描段每文件一次（lines 2042,2066,2089等）、嵌入段每批一次（line 2212），另有5s心跳线程。触发条件：文件数/批次数线性。量级：每次1~5ms+Defender实时扫描开销（代码注释自认一轮撞500+次WinError5），5000文件≈10~30s累计I/O。建议：节流合并（1s或每N文件一次）。
- B6 export.py:79-93 — `read_all_chunks`把全量embeddings转`float64` list（line 91，存储是float32/cosine归一，翻倍）+payload常驻BytesIO（line 230-231）。触发条件：导出大库。量级：10k×1024时约80MB×多份常驻，随库线性。建议：流式写出/保持float32。
- B7 wemm_indexer.py:250-383 — `page_batches`全库累积向量后再分批upsert（373）；render+encode逐页串行（339-342，DPI60约2.5s/页）。触发条件：PDF页数千级。量级：千页≈40min单线程，且向量常驻内存（20k页×512维≈40MB+）。建议：每满1000即写即清；encode并行受服务端串行锁限制，维持串行。

🟢 可忽略/备注:
- B8 WEMM改档：`_wemm_sig(model:dim:dpi)`（wemm_indexer.py:77）任变即全量重渲染——不过宽。DPI变必须重渲；model/dim变理论可复用PNG，但当前render+encode耦合（339-342），一并重渲是可接受trade-off。
- B9 冷加载：`_load_pretrained`离线优先（index.py:609，重排器复用同一入口retriever.py:253）已钉住；剩余~2.6s是权重反序列化+torch初始化，fp16（index.py:629-630, retriever.py:255）仅省读盘/显存——0.08s差恰证明读盘已非瓶颈。import固定~5.2s（chromadb顶层index.py:12、jieba retriever.py:100-103等）是首查主导项；lazy-import重构收益/风险比低，不做。
- B10 MinerU参数：默认3并发（config.py:99）+45rpm滑动窗口（extractors.py:787-834，持锁串行，占重试槽位）+单文件600s预算+poll 3s——匹配。云端单文件数十秒级→3 workers提交率3~6文件/min≪45/min，限速仅防burst/重试风暴；官方50/min留10%余量合理；0=128线程（index.py:1697）由闸门节流安全。维持。
- B11 空闲卸载：server daemon 60s轮询读progress，running即`_touch`跳过（server.py:79-81），"索引运行期间跳过卸载"已钉住；WEMM unload300s/exit1800s（wemm_server.py:409-410）、MinerU同档（mineru_server.py:478-479）只管各自服务。长索引不会被半路抽bge。`gpu_idle_unload_seconds=300/0=常驻`（config.py:37）三端语义一致。
- B12 重复释放：历史背靠背重复`release_reranker`已消除；现存`release_reranker`（retriever.py:230，仅置None，轻量幂等）与`release_model`（index.py:771，gc+empty_cache）分属开跑前（index.py:1794）、before_serve条件回调（1745,仅真渲染时）、导航前（server.py:560-561）、空闲daemon（95-96）四条互斥路径，无同类残留。`before_serve`先于`ensure_server`时序已由用例钉住。
- B13 `_vram_maybe_evict_wemm`（index.py:694-730）固定`sleep(2.0)`×2段：低显存CUDA加载最多+4s；可轮询替代，收益小，维持。fail-open正确。
- B14 0=常驻禁用后daemon仍以1s间隔空转（`min(30,max(1,0))=1`，wemm_server.py:180/201、mineru_server.py:250）：唤醒开销可忽略。另server.py:52-56注释"默认1800s"与config实际300s漂移，顺手改注释即可（非性能）。
- B15 `read_document`（server.py:640-641）、`note_relations`每次全量读meta JSON；`resolve_note_relations`（index.py:1314）每次O(F·L)现算；`advice_for`纯规则零I/O（advice.py:1,73）——个人库KB~MB级，全部可忽略。另`_auto_batch_size`每批一次`mem_get_info`（index.py:831）为µs级，忽略；`dense_k=max(top_k×8,200)`（retriever.py:636）HNSW取200候选廉价，忽略；`reranker.predict batch_size=16`（retriever.py:694，池50→4批）合理，忽略。

结论: 分级记录（🔴×0 / 🟡×7 / 🟢×8），不设通过/打回。审计口径=现有测试已钉住口径，未新增口径；真库基线9 collection口径下prune存活集逻辑正确。

摘要：
- 按编排者定案：只做分级记录；秒数引用历史实测，只做逻辑确认；口径=现有测试已钉住口径。
- 结论：🔴×0，🟡×7，🟢×8。D2/D3/D6/D7逻辑闭环完好；开销集中在D4写库分批不对称、D5查询前置枚举、B4检索N+1三处。
- 冷加载：离线优先后剩余~2.6s主因是权重反序列化+torch初始化；import固定~5.2s才是首查延迟主导项，动它收益/风险比低。
- MinerU默认3并发+45rpm匹配：限速只是防burst/重试风暴；0=128线程由闸门节流，安全。
- 文字索引写库是唯一单批一把梭，大库全量重建是唯一潜在炸批点。
- 每次search_knowledge触发2~4次全库rglob枚举，是查询路径最大固定附加开销。
- 空闲卸载300s与长索引交互正确；历史重复release已消除，现存各release路径互斥且幂等。

发现清单：
- B1 [🟡] index.py:2237 — 文字索引写库单次upsert全量，无分批
- B2 [🟡] server.py:112-129 — 每次检索逐二进制格式各做一次全树rglob，每次查询2~4次全库枚举
- B3 [🟡] retriever.py:150-155 — BM25构造每doc调tokenize两次
- B4 [🟡] retriever.py:296-331 — _expand_parent每唯一节一次按文件全量get（N+1）
- B5 [🟡] index.py:302-371 — progress每文件一次tmp写+replace落盘
- B6 [🟡] export.py:79-93 — 全量embedding转float64 list+BytesIO常驻
- B7 [🟡] wemm_indexer.py:250-383 — 页向量全库累积后才分批upsert；逐页串行约2.5s/页
- B8-B15 [🟢] 见上。
```

（注：委员会话无 write 工具，原文以文本返回，由编排者代落盘。）
