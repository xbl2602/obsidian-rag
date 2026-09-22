## Security 评审（完整意见原文）

> 口径声明：按编排者裁决 — ①不设通过/打回，只做分级；②冷加载秒数引用历史实测不插桩；③真库基线9 collection为准；④审计口径=现有测试已钉住口径。咨询模式只评不改，全程零改工作树。

### 1. MinerU API Key 是否绝不进日志/异常消息（结论：成立，无硬伤）
- `extractors.py:1239` `api_key` 仅用于组 `headers={"Authorization": f"Bearer {api_key}"}`（`:1259`），`headers` 对象从未进入任何 `log`/`_warn_once`/`raise` 消息。
- `_mineru_submit:1179-1183` 提交失败只拼 `HTTP {status} code={code}`，不含响应体全文、不含请求体。
- `_mineru_poll_result:1103-1116` 非 JSON/404 按 `gone` 折叠，不打体；429/5xx 只退避续询，不打体。
- `_mineru_cloud_extract:1302-1308` 统一折叠，注释明确"只记类型与摘要"。
- `config.py:391-394` 模板注释"敏感信息：不会出现在任何日志中"；`gui/config_editor.py:159-160` `secret:true`，`gui/widgets.py:1098-1099` `password/can_reveal` 已遮显；`guiweb/ui/app.js:1523-1524` `type=password` 同步成立。
- `data/config.json` 明文存 Key 属单机桌面惯例，文件 ACL 即系统默认，无远程暴露面。
- 残留建议见 B1（预签名 URL，非 API Key 本身）。

### 2. selection_gate 两段式有无配置绕过（结论：无绕过，成立）
- `selection_gate.py:105-106` 提案号 `sel-+secrets.token_hex(4)`、确认码 6 位 `secrets.randbelow`，盘上只存 `sha256(code)`（`:110`），明文只走返回值（`:115`）。TTL `600.0`（`:19`），`consume_proposal:128-139` 校验提案号一致→库名一致→TTL（过期即删）→哈希比对→一次性 `unlink`。顺序正确，无短路分支，无任何配置开关。`server.py:393-416 apply_selection_changes` 直调 `consume_proposal`，失败只记 AUDIT 拒收日志。
- `make_proposal:70-90 normalize_changes` 事前拦截同位置打架，`set_selection:227-233` 落盘前二次兜底。双层一致。
- 诚实边界 `selection_gate.py:5-6` 已自述"MCP 通道信任边界"，靠审计日志（`server.py:383-384,403,407`）压风险 —— 属已披露设计，不计硬伤。
- 残留建议见 B4（非恒定时间比对+无尝试限次）、B5（单全局 pending 槽覆写）。

### 3. Agent 改勾选只走 MCP 门禁 vs GUI 直改是否越权（结论：隔离成立，非越权）
- Agent 可达面仅 MCP 三工具：`get_selection`（只读）、`propose_selection_changes`（只提案）、`apply_selection_changes`（凭提案号+码）。仓库内无第二条 MCP 写 `selection_in/out` 路径；`set_config` 的 `OVERRIDE_KEYS`（`library.py:34-36`）明确不含 `selection_in/out`，`tests/library_registry_test.py:137` 钉住非法键拒绝。
- GUI 直改 `guiweb/bridge.py:489-505 selection_update` 与 `gui/widgets.py` 经 `set_selection` 直写免确认码 —— 本地用户本人操作（pywebview 本地桥 / Flet 本地进程），不在 MCP 攻击面内。故"GUI 免码"不是门禁绕过，是"本人直操 vs 代理代操"的正确分权。`selection_resolve_conflict:507-545` 仅本库移除排除+纳入，符合问题47拍板。

### 4. set_config 非法键拒绝是否完备（结论：完备，无硬伤；一处宽松见 B3）
- `library.py:498-500` 白名单拒绝非 `OVERRIDE_KEYS` 键；`unset_config:559-561` 同谓词。`extensions` 白名单对 `SUPPORTED_EXTS`（`:517-525`）、`agent_formats` 仅二进制且须已在 `extensions`（`:527-540`）、`collection` 长度+字符集+跨库冲突（`:541-548`）。大小写归一+去重保序（`:510-515`）。
- 宽松点见 B3：`collection` 正则未要求字母数字开头结尾，`"___"`/`"..."` 能入库、到建 collection 才炸。

### 5. 路径穿越（结论：均有守卫，无硬伤；一处纵深见 B2）
- 勾选路径：`norm_sel_path:54-73` 拒非 str、盘符/UNC/`~`/根斜杠、空段/`..`/`.`；`normalize_changes:78-80` 与 `set_selection:211-213` 双双 `(root/rel).resolve()` + `root in parents` containment（含 symlink 逃逸）。`bridge.selection_tree:368-373` 同谓词。`tests/test_gui_store.py:1345` 对 `../、..\、/etc/passwd、C:/Windows` 有用例钉住。
- 导出：`export.py:207-213` `rel` 由 `relative_to(vault)` 派生，`arcname=f"vault/{rel}"` 安全；包名经 `re.sub([^\w.-],_)` + sha8 防撞（`:220-222`）；`verify_package:148-157` 自产自验。
- 导入：`import.py:59-66 _validate_zip_members` 拒绝绝对路径、`..` 段、首段含 `:`（Win 盘符/ADS）与空首段；`place_vault:200-208` 目标由注册表库名派生（`validate_name:302-306`），`add_library:436-454` 三检。完整。
- 纵深缺口见 B2：`read_document` 对 meta 毒化场景无 containment 复检。

### 6. MCP 输入校验（结论：白名单完备，无注入执行面）
- 库选择 `resolve_entries:607-637` 白名单减法（未知库名抛错附清单；空集抛错），各工具全经此漏斗，无直拼接 SQL/shell。
- `folder`（`retriever.py:64-78`）纯内存前缀+边界过滤，无 FS 访问；`folder="../.."` 恒不命中（fail-closed）。
- 阈值有 `(0,1]` 校验（`server.py:682-683`）；`top_k` 下游按 `dense_candidate_factor` 展开，无拼接注入；advice 协议与两套 GUI 解析器一致，未改。
- `note_relations` 纯 meta 现算（`index.py:1314-1355`），`Path(rel).stem` 仅做字典键，无 FS 跟随。

### 7. exclude 规则能否被 crafted 路径绕过（结论：不能绕过；大小写一处建议见 B6）
- 漏斗唯一实现 `collect_md_files:1358-1415` + 裁决唯一实现 `decide_included`（`library.py:138-162`），显示与漏斗共用，无双写漂移。`verdict=="in"` 穿透是拍板语义（问题47），非漏洞；`verdict=="out"` 一票排除。
- 子串语义是过匹配 = fail-closed；同位置判定只认字符串相等 + 深度比较，crafted 路径无法洗白而不留 `tie` 痕迹（`tie=True` 时排除站住）。

## 分级发现（🔴 0 / 🟡 6 / 🟢 6）

- `B1 🟡 extractors.py:1302-1308 — PUT 预签名 URL 经 str(e) 进本地日志（临时上传凭证泄露面，建议只记类型+状态码）`
- `B2 🟡 server.py:658-660 + _read_source_text:599-609 — read_document 缺 vault containment 复检（meta 毒化可致任意本地文件读，建议 join 后 resolve 比对）`
- `B3 🟡 library.py:541-545 — collection 正则未锚定首尾字母数字（"..."/"___" 能入库到建库才炸，建议与 collection_for 同规则）`
- `B4 🟡 selection_gate.py:135-137 — 确认码比对非恒定时间且 10min 窗内无限次试（6 位码无限速，建议 hmac.compare_digest+失败计数）`
- `B5 🟡 selection_gate.py:107-113 — 单全局 pending 槽后提案覆写前提案（确认 DoS，建议按库分槽或拒绝覆写）`
- `B6 🟡 index.py:1394,1409 + library.py:126-135 — exclude 大小写敏感而 Windows FS 不敏感（SECRET 规避 secret，建议比对前 lower）`
- `B7 🟢 server.py:493-504 — allow_new_formats 真值判定（SDK 侧 bool 强类型，当前无实害）`
- `B8 🟢 全链路 — mineru_api_key 绝不进日志/异常/落盘（已逐路径核验，红线 8 成立）`
- `B9 🟢 selection_gate+server:380-405 — MCP 改勾选无配置绕过（硬编码门禁成立；GUI 直改系本地本人通道）`
- `B10 🟢 library.py:498-500 — set_config 白名单完备（selection_in/out 正确拒收）`
- `B11 🟢 import.py:59-66 — zip-slip 守卫覆盖四类绝对/逃逸形`
- `B12 🟢 index.py:1493-1499,2020-2026 + server.py:236-250 — agent_allowed 双路径 stat 前冻结（fail-closed 成立）`

### 验收复判指引（只读逻辑确认）
- `grep -n "api_key" extractors.py` → 仅取值组头，无 log/raise 拼接 Key。
- `python tests/run.py --suite test_selection`（`--suite test_gui_store` 含穿越用例、`audit_regression` 含 advice 12 条）→ 预期全绿；真库 collection 清单保持 9 个无新增。
- `propose→apply` 两次：第二次 `consume` 必拒（一一次性）；同位置 `in` 提案必拒。
```

（注：委员会话无 write 工具，原文以文本返回，由编排者代落盘。）
