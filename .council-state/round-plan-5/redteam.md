## RedTeam 评审（全文 · 只读静态审查，零改动、零落盘、零执行）

**审计对象**：maker 审计方案（D1~D7＋验收 C1..C12）vs obsidian-rag 工作树现状。

🔴 阻断（反例/证伪）：

**B1 `index.py:609-622` —— C8（“仅 OSError 回退联网”）的验收条件本身就是漏网之鱼：本地缓存损坏的非 OSError 路径永不回退，直接硬失败**
反例描述：`_load_pretrained` 只写了 `except OSError`（:620）。本地快照存在但损坏时——`config.json` 截断→`json.JSONDecodeError`（ValueError 系）、safetensors 截断→`SafetensorError`/pickle 系、`torch.load` 坏权重→`RuntimeError`——一律非 OSError，直接上抛，`local_files_only=False` 的联网自愈分支永不可达。
触发条件：任一本地模型快照内任一文件损坏（一条 `echo garbage` 即可构造）；此后 `get_model`（:733）与 `_get_reranker`（`retriever.py:253`，共用同一 helper）双双硬失败，索引与检索全挂，只能手工清缓存恢复。
为什么成立：C8 把“仅 OSError 回退”当作通过条件，但 OSError 只覆盖“缺文件”子集；“本地有文件但不可用”同属应回退集合却被 except 子句排除在外——代码检查即证（except 行只列一种异常），现有 `test__load_pretrained_*`（`tests/audit_regression_test.py:1197-1215`）只喂 OSError/成功两种 factory，从未喂过 ValueError/RuntimeError，口径缺口被测试真空掩盖。

**B2 `wemm_indexer.py:462-463` vs `index.py:1764-1767` —— C4（before_serve 让路机制）存在一条完全绕过它的正式调用路径：CLI 直跑页索引永不释放 bge**
反例描述：`_wemm_auto_phase` 传了 `before_serve=_release_for_wemm`，但 `wemm_indexer.main()`（:462-463）调 `index_wemm_library(..., agent_allowed=None)` **根本没传 `before_serve`**——同一函数两条生产路径，一条让路、一条不让。`_ensure_server_lazy`（:260-278）是 before_serve 的唯一触发点，不传回调＝该点静默 no-op。
触发条件：一次检索后（bge-m3＋reranker 常驻显存）→ 终端执行 `python wemm_indexer.py --backend on`（AI_GUIDE 官方指引的常规操作）→ WEMM 5.1GB 直接与常驻 bge 同驻，8GB 卡溢出进 WDDM 共享显存或 OOM。问题 58 的叙事是“纯 md 增量不白放”，CLI 路径下却变成“有页渲染也不放”——方向恰好反了。
为什么成立：逐行 grep 即证主调点共两处，一处传、一处不传；`main()` 还是 `--backend on` 的唯一 CLI 入口，不是死代码。

**B3 `index.py:1800-1810`＋`server.py:557-566` —— “开跑前主动 evict WEMM”与“BGE 实际加载”之间隔着数分钟的无锁窗口，并发一次 `navigate_knowledge` 即可把 WEMM 重新抬进显存，evict 形同点查而非持有**
反例描述：`index_library` 开跑前 evict（:1803-1808，仅当愉当时 `server_alive`），但 BGE 真正 `_load_model` 发生在扫描→转换→MinerU→编码的数分钟后（`get_model` :744-768）。窗口内 `navigate_knowledge`（:557-566）在**无任何 `_index_running` 守卫**的情况下执行 `release_model()`＋`ensure_server()`（把 WEMM 按需拉起），与后台索引线程同进程抢显存。`_vram_maybe_evict_wemm` 的首次读（:707）还用着 `max_age=5.0` 的缓存值（见 B6），stale 偏高即跳过二次 evict，直进 `_load_model`（:752）→ 双驻留→OOM→降级 CPU，整轮索引被拖慢一个数量级。
触发条件：后台索引（`_run_index`）跑大库期间，任意用户/Agent 调一次 `navigate_knowledge`——这正是 `ensure_fresh` 设计的常规并发（“搜索不等待”），非极端构造。
为什么成立：三处代码并读即证——evict 无锁、无“加载期持有”语义；navigate 路径无索引互斥；仲裁是瞬时点查不是租约。

**B4 `index.py:878`＋`:830-831` —— fail-open 铁律在此函数失效：`_auto_batch_size` 的无保护 CUDA 直调发生在 `_encode` 的 try 之外，CUDA 瞬态死亡的首个 `encode_safe` 直接上抛，降级链够不着**
反例描述：`encode_safe`（:867-897）在 :878 调 `_auto_batch_size`，而 `_encode` 的 `(RuntimeError, OSError, MemoryError)→fallback` 保护只从 :856 开始。`_auto_batch_size`（:830-831）调 `torch.cuda.mem_get_info()` **无任何 try**，仅靠 `_device != "cuda"` 字符串守卫。`_device` 是进程内普通字符串：CUDA 已死但 `fallback_to_cpu` 尚未执行之间，首个 `encode_safe` 在 :831 撞 `RuntimeError`，异常点在 try 之外→整批上抛→`_index_core` 当批中断。
触发条件：CUDA 瞬态失败（OOM 杀 context/驱动抖动）后的第一个编码批——恰恰是系统最需要“自动降级 CPU 重试一次”（:868-871 注释承诺）的时刻。
为什么成立：行号顺序即证（878 在 856 的 try 之前，831 无 except）；`_is_memory_error` 再完备也覆盖不到 try 之外的抛点。

**B5 `index.py:2326-2338` vs `:2307-2316`＋`:32` —— C9（“存活集完备”）为假：prune 的 `keep` 从不读入 legacy/base `index_meta.json`，却刻意保留该文件——旧单库入口的提取缓存会被当孤儿删掉**
反例描述：步骤 2 的 `keep` 只并入 `index_meta_<name>.json`（:2329）＋`_pending_load()` md5；步骤 1 明确保留 base `index_meta.json`（:2307 注释“保留 base index_meta.json”）但**没有任何一行读它**。`index_vault` 旧单库入口（:1720-1725）至今仍以 `INDEX_META`（:32）为指纹文件（GUI/export 兼容路径）。
触发条件：工作树存在 base `index_meta.json`（迁移残留/旧入口写入）且其 hash 对应提取缓存存在 → 任意一轮全成功 prune → 该批 md/sidecar 被删 → `read_document` 对这些文件报“未提取”、下轮索引被迫重提（MinerU 配额＋时间双重浪费）。
为什么成立：保留文件却不保留其指涉对象，同一函数内自相矛盾；“完备”断言被单文件反例推翻。附带：步骤 2 调 `_pending_load()` 无参读**全局**缓存目录，而 `cache_dir` 参数允许注入隔离目录——传参隔离时存活集与扫描目录错位（`tests/test_prune.py` 恰用注入目录，错位被测试形状掩盖）。

**B6 `index.py:733-768`（无锁）vs `retriever.py:226,248`（有锁）——`get_model` 双重加载竞态：2026-08-15 给 reranker 加了 `_reranker_lock` 修双份模型，embedding 的同一 bug 原样保留；`navigate_knowledge`（`server.py:559-561`）的无锁 `release_model()` 是现成的并发触发器**
反例描述：`get_model` 全程无锁：线程 A 在 `_load_model`（2.6s 冷加载窗口，CPU 回退时 10s＋）途中，线程 B 进 `get_model` 见 `_model is None` → 启动**第二份** `_load_model` → 双份 bge 并驻（fp16 约 2×1.2GB＋context），8GB 卡上这就是压垮 WDDM 的最后一根稻草；交错更差时 A 刚赋值的 `_model` 被 B 的 `release_model` 置空，`None.encode` 直接炸当批。`server.py:559-561` 的 navigate 路径在 MCP 线程调无锁释放，而 `_run_index` 后台线程（:166-198）同进程编码——触发器是产品内建的，不是外部杀手。
触发条件：后台索引加载模型窗口期内并发一次 navigate/search（CPU 盒子 10s 窗口极易命中；GPU 盒子 2.6s 窗口多试几次必现；日志出现两次“加载 embedding 模型”即实锤）。
为什么成立：同一文件内 reranker 有锁、embedding 无锁，对照即证这是已知 bug 类的漏修，不是新猜想。

🟡 风险 B7~B14：
- **B7 `tests/run.py:50-123` —— 统一回归快照表漏网 6 类全局**：未覆盖 `export.(CHROMA_DIR/DATA_DIR/EXPORT_DIR)`、`import.(CHROMA_DIR/DATA_DIR/IMPORT_WORK_DIR/VAULT_EXPORT_DIR/ARCHIVE_DIR)`、`extractors` 缓存目录（`set_cache_dir`）、`gpu_arbiter.ensure_mineru`、`index._model/_device`、`retriever._reranker/_reranker_failed`、`server._background/_gpu_activity`。`verify_export_import.IsoDirs`（:110-146）只管自家套件内还原，跨套件兜底全靠 `run.py`——兜底网眼比鱼大。后果实例：某套件 stub `ensure_mineru` 不还原→后续 MinerU 测试走假服务；真模型残留 `_model`→后续套件显存基线被污染；`export.EXPORT_DIR` 被改→直写真实 `data/export`。
- **B8 `gpu_arbiter.py:43-46`＋`index.py:707` —— 5 秒 stale 显存读驱动 evict 决策**：`vram_free_gb` 默认 `max_age=5.0`，`_vram_maybe_evict_wemm` 首查用缓存值。WEMM 刚加载/刚卸载的 5s 内启动 BGE 加载：stale 偏高→漏 evict（撞车，见 B3 后半）；stale 偏低→误 evict 正在编码的 WEMM。后批次下轮重试＝浪费整轮页编码。`max_age=0.0` 的强制刷新（:717）只在第一次判断失手后才发生，顺序反了。
- **B9 `server.py:72-101` —— 空闲卸载守护在跨进程场景下保护了错误的一方**：守护每 60s 读跨进程共享的进度文件 `running`（:79）。GUI 点索引走独立 `index.py` 子进程：其 WEMM 阶段同样写 `running=True` → server 守护全程跳过卸载，server 进程的 bge 常驻显存与子进程的 WEMM 对顶——该放的没放。反向：子进程纯文字索引时 server 的 bge 本可卸载省显存，却被迫保留。另 `_wemm_auto_phase` 每库收尾写 `running=False`（`index.py:1769-1775`），多库间隙毫秒级窗口若撞上 60s tick 即误卸载（概率低但机制性存在）。
- **B10 `index.py:634-647`＋`:858-864` —— fp16 安全叙事只查了一半**：`_param_dtype_mixed` 只扫 `parameters()`，不扫 buffers（BN running stats、rotary `inv_freq` 等）；混入 fp32 buffer 的模型通过加载检查，在 `encode` 才报 dtype 不一致——该 RuntimeError 非 memory-error，`:858-864` 直接 `raise`，无 CPU 重试、无终态记账，当批上抛。加载期检查≠运行期免疫。
- **B11 `server.py:622-667` —— `read_document` 全程无 `reload_config`**（同文件 :230/:376/:482/:684/:801 五处任务边界都有，唯独读路径没有）；`defaults=CFG.get("default_libraries", [])`（:633）读 import 快照。用户中途改默认库不重启 MCP 即调 `read_document` → 走旧默认，多解/错解。
- **B12 `gui/stop.py:38-65` —— “stop.py 唯一入口”只覆盖 GUI，不覆盖 detached 服务**：WMI 匹配仅 `gui/app.py`＋`flet.exe`；WEMM/MinerU 服务以 `DETACHED|NEW_GROUP` 独立拉起（`gpu_arbiter.py:227-228,526-527`），stop 杀不到。GUI 退出后 5.1GB 常驻至多半小时（`_IDLE_EXIT_S=1800`）；若配 `idle_exit=0`（常驻）则永久残留，且全仓无“一键全停”入口。
- **B13 `index.py:1697,2132` —— `mineru_concurrency=0`（最大吞吐）的实现与描述不符**：`workers=min(len(jobs),128)` 常驻 128 线程阻塞在长连接上，而实际节流是 45rpm 滑动窗口——线程数与限速无联动，`cloud_jobs>128` 的大库＝128 个长轮询连接空转等窗口，fd＋内存开销换零吞吐增益。bounded（128 上限）故列🟡。
- **B14 `server.py:112-129`＋`index.py:1384-1385` —— C11（“冻结分支零 I/O”）字面不成立**：`_pending_formats` 为统计未授权格式数量，对每个冻结格式调一次 `collect_md_files` 全库 `rglob＋is_file`（目录遍历＋stat 就是 I/O）；且 `ensure_fresh`（每次 `search_knowledge` 必经，`server.py:230`）都跑一遍。per-file 内容零读取成立，但“零 I/O”字面断言被计数路径证伪（大库每次搜索前多 N 次全库 walk，N＝冻结格式数）。

🟢 备注：
- 本次全部为静态只读审查＋触发条件推导，未插桩、未压测、未落盘；真库 collection 基线未触碰。
- 未列入阻断的刻意保留：WEMM 双直调（`wemm_server.py:137,141`）因 `_resolve_model_path`（:94-116）命中快照目录时传本地路径、不经过 hub 核对，危害有缓解且 C2 既有钉子明示只管两条路径，故降🟡观察项，不硬列🔴。
- 口径漂移（非缺陷）：`server.py:53-56` 注释“默认 1800s” vs `config.py:37` DEFAULTS 300 vs `AGENTS.md:80`“默认 300s”——实际以 config 为准，server 注释 stale；`TASK_LOG` 另有 1800→300 的变更记录，三处未对齐。

结论: 被推翻（阻断数 6）

发现清单：
- [🔴] B1 `index.py:620` — 仅捕获 OSError，损坏缓存的非 OSError 逃逸且永不联网回退
- [🔴] B2 `wemm_indexer.py:462` — CLI 入口不传 before_serve，页渲染时 bge 永不让路
- [🔴] B3 `index.py:1803`/`server.py:562` — evict 与 BGE 加载间无锁窗口可被 navigate 重填 WEMM
- [🔴] B4 `index.py:878`/`index.py:831` — try 之外的无保护 CUDA 直调，首批失败绕过降级链
- [🔴] B5 `index.py:2329` — prune 存活集漏 base 指纹，旧入口缓存被误删
- [🔴] B6 `index.py:733` — get_model 无锁双重加载，navigate 无锁释放为内建触发器
- [🟡] B7 `tests/run.py:50` — 快照表漏 export/import 常量等 6 类全局，跨套件污染无兜底
- [🟡] B8 `gpu_arbiter.py:45`/`index.py:707` — 5s 缓存显存读驱动 evict，可漏可误
- [🟡] B9 `server.py:79` — 守护读跨进程 running 标志，跨进程场景保错边
- [🟡] B10 `index.py:634` — fp16 混搭检查只扫参数不扫 buffer，运行期 dtype 炸无重试
- [🟡] B11 `server.py:622` — read_document 无 reload_config，读旧默认库快照
- [🟡] B12 `gui/stop.py:38` — stop 覆盖不到 DETACHED 服务，无一键全停
- [🟡] B13 `index.py:1697` — 128 常驻线程与 45rpm 限速脱钩，大库空转连接
- [🟡] B14 `server.py:112` — 冻结格式计数做全库 walk，违 C11“零 I/O”字面
```

（注：委员会话无 write 工具，原文以文本返回，由编排者代落盘。）
