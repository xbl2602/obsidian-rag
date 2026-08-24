# Architect 评审 — round-plan-1

对照源码逐条验证完毕（index.py / library.py / config.py / retriever.py / gui/config_editor.py / server.py / gui/store.py / 测试）。

## 🔴 Blocker

**B1 — config 新增三键漏掉第三处同步点（GUI 设置页），六件套回归必红**
- 定位：plan 只写了 DEFAULTS + CONFIG_TEMPLATE 两处。
- 证据：本项目对"默认值"有三处同步的铁律（审计 F12 教训）。tests/test_config_editor.py:74-75 有静态断言 `missing = set(DEFAULTS) - set(ce.ALL_KEYS); assert not missing`，而 gui/config_editor.py:56 的 ALL_KEYS 由 GROUPS 派生。方案加三键不动 GROUPS ⇒ test_config_editor 必失败，与验收标准 A12 自相矛盾。
- 修法：GROUPS 增加一组（如「PDF/OCR 提取」三键均 str），一行成本。

**B2 — xfail 防死循环存在漏洞：「提取成功但产出空文本」不落终态条目**
- 定位：失败分支条件仅 content None × index.py:1163-1167 空正文守卫 continue 不写 meta 不进 current_rels × index.py:926-933 entry None 时每轮 added+=1。
- 推演：图片型 PDF（pymupdf4llm 返回空白）、空 docx、MinerU 返回空——本功能主动引入的高频输入走 content="" → 空守卫 → meta 无条目 → 每轮 kb_stale 判 added≥1→stale→ensure_fresh 每次搜索触发后台重建循环；文件未变快速路径永远命中不了终止状态。缓存压得低成本压不掉"每轮判脏+重建调度"。
- 边界澄清：不要求顺手修既有 md 空文件隐患（另立任务没问题）；要求新增代码引入的新路径（非 TEXT_SOURCE_EXTS）必须全部到达终态。修法一行：非原生文本源"提取失败或产出无可索引文本"统一走 xfail 条目（可加 reason 区分 empty/tbd）。tbd-heavy 判定对转换文本同样会产生"跳过但不落盘"的非终态（index.py:1150-1153），建议同一终态机制收口。

## 🟡 建议
1. 后端升级不自愈无信号：先索引后装 MinerU，已 xfail 且文件未变的 PDF 走快速路径永不重试、warn_once 也不再触发，用户侧完全静默。建议 xfail 记 backend 标识（xsrc 字段），与当前配置不一致视为待重试；最低限度文档写明"装机后需 --full"。2. 提取缓存无版本戳：key 纯内容字节，升级提取逻辑后旧缓存仍命中且 META_VERSION 全量重建也不重新提取（重建只重建切块层）。建议缓存文件名带 EXTRACTOR_VERSION 或明文规定改逻辑必清 ocr_cache。3. 格式知识三处硬编码（library 白名单 / TEXT_SOURCE_EXTS / extractors 分支）：建议 extractors 导出 SUPPORTED_EXTS 单一事实来源，set_config 引用之并断言并集=白名单全集。4. "非正常索引"谓词分散四处（index.py:914/937-938/942-943/1143-1144）：方案只改中间两处；tbd 与 xfail 双标记可共存但必须抽单点谓词 `def _skipped(info): return bool(info.get("tbd") or info.get("xfail"))` 供四处调用+注释声明互斥性。5. 缓存目录建议可注入参数覆盖口利测试隔离。6. 默认值诚实性：本机未装 MinerU，默认 "mineru-local" 意味着默认路径就是探测失败降级；默认 "none"、装机者显式开启更符合"默认值=当前实际行为"惯例（轻微可辩）。

## ✅ 认可
extractors.py 边界干净依赖单向无环；_load_text 放 index.py 正确（原始字节 md5+免转换模式是指纹基础设施语义）；缓存放 extractors 内部正确；kb_stale 走 extract=False 保住 GUI 每秒轮询零转换成本的关键边界；retriever.py 零改动声明核实成立；META_VERSION 8→9 借全量重建统一新旧哈希语义时机正确；frontmatter 后缀门控方向正确。

## 结论
有 blocker（2）——均为方案文本层面遗漏（GUI 第三处同步一行、xfail 终态条件一行级修补），架构骨架经源码验证全部成立，补齐后可直接进入实施。
