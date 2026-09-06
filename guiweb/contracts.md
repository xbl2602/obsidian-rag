# guiweb 前后端契约（v1）

> 前端（`guiweb/ui/`）与 Python 桥（`guiweb/bridge.py`）的唯一接口文档。
> 前端开发时用 `ui/mock.js` 提供 `window.pywebview.api` 的假实现（`?mock=1` 或
> 检测到 `window.pywebview` 不存在时自动启用 mock 并显示"演示模式"角标）；
> 生产环境 pywebview 自动注入真实现。前端**不得**直接访问文件/网络。

## 通用约定

- 所有方法返回 JSON 可序列化对象（pywebview 自动在 JS 侧包成 Promise）。
- 所有列表/字段中文文案由后端给全，前端只做渲染，不自造业务文案。
- 后端主动推送：Python 侧周期性调用
  `window.__push(type, payloadJson)`，前端监听 `window.addEventListener(type, e => e.detail)`。
  推送类型：
  - `snapshot`：每 1 秒全量状态（结构同 `get_snapshot()` 返回值）
  - `log`：`{lines: [..], cursor: n}` 新日志行
  - `alert`：`{level:"dead", text:…}` 索引心跳停止告警（每次劣化只推一次）
  - `notice`：`{text}` 后端阶段提示（如语义边「正在加载嵌入模型…」）
  - `preview`：`{running, done}` 试验台状态变化提醒（详情仍走 `preview_poll()`）
- 主题：前端自带深/浅双主题（CSS 变量），不依赖后端。

## 方法

### get_snapshot() → 快照（前端每秒也会收到同名推送）
```json
{
  "libs": [{"name":"技术笔记","state":"ok|stale|none","files":1284,"chunks":5731,"path":"D:\\Vault\\技术笔记"}],
  "agg_state": "ok|stale|none",
  "files": 1894, "chunks": 8306, "vault_files": 1902,
  "progress": {"running":false,"phase":"idle|scanning|converting|embedding|writing|done",
               "files_done":0,"files_total":0,"chunks_done":0,"chunks_total":0,
               "pct":0.0,"elapsed":0,"library":"","busy":false,
               "heartbeat":"idle|running|dead|stalled|done","heartbeat_note":null},
  "last_elapsed": 80,
  "issues": [{"lib":"论文阅读","reason":"scanned","count":3,"label":"扫描件 PDF",
              "advice":"如已在设置中启用…"}],
  "wemm": {"backend":"on|off|local","url":"127.0.0.1:9101"},
  "device": {"model":"BAAI/bge-m3","rerank":"BAAI/bge-reranker-v2-m3","cuda":true}
}
```
- `device.cuda` 可能为 `null`（torch 尚未懒加载完成，稍后推送里会带上）。
- `progress.heartbeat_note`：停滞宽限/转换提示文案（null = 显示默认「心跳正常」）。

### list_libraries() → 库管理列表
```json
[{"name":"技术笔记","path":"…","collection":"tech_notes","blocks":5731,
  "last_indexed":1788000000.0|null,"overrides":"chunk_char_limit=600",
  "state":"ok","issues":{"scanned":3}}]
```

### get_library_config(name) → 库配置弹层数据
```json
{"effective": {"extensions":["md","pdf","docx"],"exclude_dirs":[…],"chunk_char_limit":600,…},
 "overrides": {"extensions":["md","pdf"],"chunk_char_limit":600},
 "all_keys": ["extensions","exclude_dirs","exclude_files","exclude_patterns",
              "chunk_char_limit","short_doc_char_limit","collection"]}
```
`overrides` 里有的键 = 该库显式覆盖；没有 = 继承全局（effective 里可见继承值）。
可覆盖键以 `all_keys` 为准。

### add_library(path, name) → {ok: true} | {ok:false, error:"…"}
### remove_library(name, drop) → {ok: true}（drop=false 仅注销 / true 连数据删除）
### set_library_config(name, updates:{key:value}) → {ok, errors:{key:msg}, cloud_confirm?:false}
- `extensions` 传字符串如 `"md,pdf"`；空字符串 = 恢复继承（内部转 unset）。
### unset_library_config(name, keys:[...]) → {ok}

### start_index(full, libraries) → {ok, already_running?}
- `libraries`: `""`=全部注册库，否则逗号分隔。
- 全量确认弹窗所需数据由前端用 snapshot 拼（目标库、总块数 8306、警告文案）。
### stop_index() → {ok, stopped, reason?}
- 只能停**本 GUI 拉起**的索引子进程；跨进程（MCP 触发）返回
  `{ok:false, stopped:false, reason:"该任务不是本应用启动的，请用 python gui/stop.py 或等待完成"}`。

### search(query, top_k, libraries, include_body) → 结构化结果（阻塞，首次 30–60s）
```json
{"results":[
  {"lib":"技术笔记","rel":"20-Projects/Obsidian RAG/WEMM 设计.md",
   "heading":"WEMM 页级向量","chunk_idx":1,"chunk_total":3,
   "confidence":0.87,"body":"…块全文…"},
  {"lib":"","rel":"","confidence":null,"body":"（本次查询整体置信度偏低…）","notice":true}],
 "elapsed": 12.3, "error": null}
```
- `notice:true` 的条目是整体提示横幅（低置信提示/截断提示），渲染成横幅不算结果。
- 双链不在此处：单条结果按需调 `note_relations`。

### note_relations(lib, rel) → {resolved, file, outlinks:[rel], inlinks:[rel]}

### open_source(lib, rel, heading) → {ok}
- Obsidian vault → `obsidian://open?vault=…&file=…#heading`；普通目录 → `os.startfile`。

### open_path(path) → {ok}（打开文件夹/日志目录）

### pick_path(mode, start) → {path, error}（原生选择弹窗；mode: "dir"|"file"；start 为输入框现值用于定位起始目录；取消 → {path:null}）

### selection_tree(lib, sub) → 该目录一层的勾选态 + 整棵目录树（问题44；sub 空=库根；越界/非法 → {error}）
```json
{"lib":"…","sub":"…","root":"…",
 "folders":[{"path":"","depth":0,"name":"库名","state":"root","state_text":"库根","explicit":null},
            {"path":"20-Projects/课件","depth":2,"name":"课件","state":"auto_in","state_text":"入库（跟随子内容）","explicit":null}],
 "dirs":[{"name":"课件","dir":true,"path":"课件","explicit":null,"state":"auto_in","state_text":"入库（跟随子内容）","n_children":5}],
 "files":[{"name":"a.pdf","dir":false,"path":"课件/a.pdf","explicit":"in","state":"in","state_text":"已入库（显式勾选）","ext":"pdf"}],
 "selection_in":["…"],"selection_out":["…"],"extensions":["md","pdf"],"default":"follow","error":null}
```
state：in/out（显式）| auto_in/auto_out（中性，按格式开关与 selection_new_files 判定）|
root（树根）。folders = 全库目录树（扁平、depth 缩进、跳过隐藏目录，≤4000 项）——
前端左栏目录树一次拉取，右栏按 sub 懒加载文件。

### selection_update(lib, changes:[{path, action:"in"|"out"|"neutral"}]) → {ok, error?, selection_in, selection_out}
GUI=用户本人，直接生效（无需 MCP 那套确认码）；下一轮索引自动应用。

### selection_format_bulk(lib, ext, include) → {ok, changed, error?}
格式快捷批量：include=false 把该格式文件的显式勾选移入排除（青苹果菜单跟着取消）；true 反向。文件夹级条目不动。

### get_settings() → 设置页全量
```json
{"groups":[{"title":"PDF 与云端 OCR","level":"basic|advanced","desc":"…",
   "fields":[{"key":"pdf_scan_backend","label":"扫描件 OCR 后端","kind":"str|int|float|bool|list",
              "hint":"…","rebuild":false,"secret":false,
              "choices":[["none","不做 OCR…"],["mineru-cloud","…"]],
              "suggest":[["BAAI/bge-m3","推荐 · 中英多语"]],
              "value":"none"}]}],
 "missing_keys": []}
```
- `value` 一律字符串（bool → "true"/"false"，list → "a,b"）。
- **云端同意门禁在前端**：`pdf_scan_backend` / `pdf_text_backend` 新值含
  `mineru-cloud` 且旧值不含时，先弹确认框再调 save。

### save_settings(updates:{key:"value_str"}) → {errors:{key:msg}}（空 errors = 成功，已热读）

### graph(libraries) → 图谱数据（阻塞，首次构建后内存缓存）
```json
{"nodes":[
  {"id":"技术笔记|20-Projects/Obsidian RAG/WEMM 设计.md","lib":"技术笔记",
   "rel":"…","type":"md|txt|docx|pdf","chunks":12,"updated":1788000000.0|null,
   "pipeline":{"mineru":"none|queued|done|failed","wemm":"none|done"},
   "pages":14,"fail_reason":null,"theme":"wemm|mineru|chunk|daily|config|general",
   "big":true},
  {"id":"技术笔记|wemm|<pdfRel>|p3","lib":"技术笔记","rel":"…","type":"page",
   "page":3,"pipeline":{"mineru":"done","wemm":"done"}},
  …],
 "edges":[{"a":"id1","b":"id2","kind":"link|own|page"}],
 "libs":["技术笔记","论文阅读","会议记录"],
 "stats":{"nodes":63,"edges":77}}
```
- `pipeline.mineru`：meta 有该 PDF 条目且非 xfail=done；xfail scanned 且后端未
  变更=queued；xfail 其他=failed；meta 无条目=none。
- `theme`：按路径关键词归族（wemm/mineru/chunk/daily/config/general），用于语义
  聚落的确定性别名（前端可显示主题图例）。
- `big`：连接数前 5 的节点（hub）。
- 语义边不在 graph() 里（避免默认加载模型）：单独调 `semantic_edges()`。

### semantic_edges(libraries, threshold) → {edges:[{a,b,sim}]}（阻塞，首次加载嵌入模型 30–60s）
- 以「标题+相对路径」为文本用当前嵌入模型编码，余弦 ≥ threshold 输出边。
- 纯读路径（不碰 Chroma、不写任何文件）；失败返回 {edges:[], error:"…"}。

### dedup_run(threshold) → {clusters:[{lib,a,b,sim}], stats:{scanned,skipped,pairs,groups}, error?}（阻塞，读全库文件）
### failures(lib) → {total, rows:[{rel, reason, will_retry}]}
### wemm_status(lib) → {exists, total_pages, rows:[{rel, pages|null, failed, reason}]}
### wemm_probe() → {alive, detail}

### preview_start(path, backend) → {ok, error?}（backend: null=跟随全局 / "local" / "mineru-cloud"）
### preview_poll() → {running, done, result:null|{ok, error?, markdown, rendered_html}}
### preview_cancel() → {ok}

### log_tail(cursor) → {lines:[...], cursor}（cursor=null 从头；含历史 data/gui_index.log 尾部）

### export_run() → {ok}（子进程跑 export.py，输出进日志流；写 exports 目录，不碰库）
### import_run(confirm_text) → {ok, error?}
- confirm_text 必须逐字等于「我确认导入」才执行；子进程跑 import.py，输出进日志。

### get_static_path(name) → {path}（前端要打开的本地资源绝对路径，如日志目录）

## 前端视图与功能映射（Flet 全功能对齐 + demo 增量）

| 视图 | 必须有 |
|---|---|
| 图谱（默认） | graph 数据渲染、图层开关（双链/归属/WEMM页/缓存/PDF原件/库着色/库边界）、检索涟漪+轨道、Inspector（管线区块/双链/语义近邻定位）、库过滤 |
| 检索 | 输入+TopK+展开正文+库范围、结构化结果、置信度徽章、片段展开、关联笔记（懒加载+缓存）、打开源文件、耗时 |
| 库 | 列表（块数/最近索引/覆盖摘要/状态）、添加、每库配置（格式/门禁/排除/切块/覆盖与继承）、移除双确认、打开文件夹、库范围多选（影响检索与重建目标） |
| 索引 | 增量/全量（红确认：目标库+总块数+预计+检索可用性警告）、五阶段 stepper、pct/ETA/耗时、心跳五态+note、DEAD 告警、停止按钮、上次耗时 |
| 诊断 | 失败明细表（含 will_retry）、WEMM 状态+探测、近似去重、导出/导入（确认门禁） |
| 设置 | 12 分组全部字段、rebuild ⟳ 标记、choices 下拉、suggest 芯片（✓使用中）、secret 密码框、保存热读、云端同意门禁 |
| 全局 | 动态岛（心跳五态+索引进度形变）、日志区（着色/计数/清空）、深浅主题、提取试验台（选文件/后端覆盖/双页签/取消/超时）、演示模式角标（仅 mock）、单实例守卫（后端） |
