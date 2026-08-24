# TODO — 多格式文档支持（PDF / DOCX / 扫描件 OCR）

> 状态：方案已定稿（v2，经 council 三委员评审修订），分三轮交付。
> 评审记录：`.council-state/round-plan-1/`（8 个 blocker 已全部吸收进本清单）。
> 基线：HEAD 92af6e6，META_VERSION 将 8→9。

## 交付轮次

| 轮次 | 范围 | 出口标准 |
|---|---|---|
| **R1（最小可交付）** | CLI 全链路 + **MinerU 云端 API 后端** | `library.py` 开 extensions → `index.py --library` 端到端索引 md/pdf/docx（扫描件走云端转换）；六件套回归全绿 |
| **R2** | **MinerU 本地部署**及联调 | 本机装好 pipeline 后端，local 路径实测通过，xsrc 自愈（cloud↔local 切换自动重试）验证 |
| **R3** | **GUI 适配** | 设置页/进度展示对多格式的完整体验 |

## 核心思想

索引时把非 MD 文件转成 Markdown，复用既有整条切块管线；源文件零写入；
检索引用指向原始相对路径；一切「不产块的文件」必须落持久化终态防重建死循环。

## 架构 v2 关键机制（三轮通用，实施时不得偏离）

### A. 统一终态机制（封堵五个死循环入口）
- `_load_text(fpath)` → `(text | None, 字节hash | "unreadable")`：
  - 空串/纯空白归一为 None（堵空产出入口）
  - OSError 不上抛，hash 用哨兵值 `"unreadable"`（锁定/OneDrive/AV 场景两轮哨兵等值 → 自动判稳；解锁后真实 hash ≠ 哨兵 → 自动重试）
  - 后缀路由一律 `suffix.lower()`（堵 `.PDF`/`.MD` 静默 xfail）
- `_index_core` 分支顺序：**None 判定严格先于 is_tbd_heavy**（否则 content=None 炸 AttributeError 废整轮）
- 非正常路径统一出口：
  ```python
  meta[rel] = {"hash", "chunks":0, "size", "mtime",
               "xfail":True, "reason":"unreadable|extract-failed|empty|tbd",
               "xsrc":当前后端名}   # 且必须 current_rels.add(rel) 使条目持久化
  ```
- kb_stale 对二进制源：`extract=False` 拿字节哈希比对即可判「真没变」，绝不跑转换（GUI 每秒轮询的零成本边界）；entry 为 None 的失败新文件照常计 added 一次 → 触发一次重建落终态 → 此后稳定收敛

### B. 后端自愈
- 终态条目记 `xsrc`；kb_stale 对 xfail 条目 O(1) 比较 `xsrc ≠ 当前配置后端` → 视为待重试（换后端无需 --full）
- 缓存键 `<md5>.<backend>.v<EXTRACT_VERSION>`：换后端/升提取器旧缓存天然失效

### C. 单一事实来源
- extractors.py 导出 `TEXT_EXTS={md,txt}`、`BINARY_EXTS={pdf,docx}`、`SUPPORTED_EXTS`
- index 的 TEXT_SOURCE_EXTS 与 library 的白名单校验都引用它，禁止三处硬编码
- 单点谓词 `_skipped(info): bool(info.get("tbd") or info.get("xfail"))` 收拢四处判断

---

## R1 — 最小可交付：CLI 全链路 + MinerU 云端后端

- [ ] **1. requirements.txt + 安装验证**：pymupdf / pymupdf4llm / python-docx（Py3.14 轮子验证，lxml 是唯一硬风险；失败即停回报，退路=docx 优雅降级）。版本号安装成功后回填锁定
- [ ] **2. extractors.py**（新文件，只依赖 config.CFG）：
  - [ ] `extract_to_markdown(path) -> str|None`：唯一入口，绝不抛异常、绝不写源目录
  - [ ] DOCX 路：python-docx 按 body 子元素保序遍历；Heading N/标题 N 样式→#×N（>3 钳 ###，兜底 base_style）；表格→管道表（\| 转义、单元格换行→空格）
  - [ ] PDF 路：fitz 探测文字层覆盖率（≥0.5 页占比）→ pymupdf4llm.to_markdown()（list 返回值 join 归一）；扫描件路 → OCR 后端
  - [ ] OCR 后端分发框架：`mineru-cloud`（R1 主路径，完整实现）/ `mineru-local`（代码路径按同构预留，R2 联调验证）/ `none` 或命令探测不到 → warn_once(含安装指引)+None
  - [ ] 子进程卫生：列表参数禁 shell=True；UTF-8 显式解码；超时读 `mineru_timeout_seconds`；超时后 taskkill /T /F 树杀+proc.wait() 收尸；CREATE_NO_WINDOW
  - [ ] 输出目录=data/ocr_cache/tmp-<pid>-<ts>/，rglob("*.md") 取最大者读取，try/finally 整体删除临时目录
  - [ ] 缓存层：键=`<md5>.<backend>.v<N>`；原子写 tmp 带 pid + os.replace；**写失败仅跳过缓存照常返回结果**；None 不写缓存；启动清扫 >24h 孤儿 *.tmp；目录可注入参数覆盖
- [ ] **3. config.py**：DEFAULTS + CONFIG_TEMPLATE 同步四键（template_consistency_errors 必须保持 []）：
  `"pdf_scan_backend": "mineru-cloud"`（R1 默认=实际可用路径；R2 联调后再议是否翻转推荐）、
  `"mineru_cloud_cmd": ""`、`"mineru_local_cmd": ""`、`"mineru_timeout_seconds": 600`（进 _POSITIVE_KEYS）
- [ ] **4. gui/config_editor.py（仅最小同步）**：GROUPS 加「PDF/OCR 提取」组收编四键——缺这步 test_config_editor 必红（DEFAULTS⊆ALL_KEYS 静态断言）；完整 GUI 体验留 R3
- [ ] **5. index.py**：
  - [ ] META_VERSION 8→9（v9 注释：多格式提取+原始字节指纹）
  - [ ] `_load_text()` 统一入口（终态机制 A 全部语义在此）替换 kb_stale/_index_core 两处裸 read_text
  - [ ] kb_stale：二进制源走字节哈希快速比对；xfail 条目 xsrc 失配视为待重试；removed/expected 排除口径改用 `_skipped()`
  - [ ] _index_core 主循环重排（None→tbd→哈希短路→终态落盘→正常切块）；frontmatter 仅对 TEXT_EXTS 生效
  - [ ] 提取前 update_progress(phase="converting")；progress_text 加 converting 豁免分支
  - [ ] `__main__` 多库循环逐库 try/except（一库失败不连坐）
  - [ ] 单点谓词 `_skipped(info)`
- [ ] **6. library.py**：set_config extensions 白名单校验引用 SUPPORTED_EXTS；小写归一去重保序
- [ ] **7. retriever.py**：零改动（已核实块元数据链路天然兼容）
- [ ] **8. tools/check_notes.py**：scan_library 对非 TEXT_EXTS 跳过正文分析（修二进制 UnicodeDecodeError 崩溃）
- [ ] **9. tests/test_extractors.py**（新文件，风格对齐 audit_regression_test.py 非 pytest）：
  - [ ] docx 回环（标题层级/管道表/正文）；pdf 文字层生成提取
  - [ ] 大写扩展名 `.PDF`/`.Docx` 路由正确（红队 B3 回归）
  - [ ] 伪造二进制→None 无堆栈；空 docx→None 走 xfail
  - [ ] 缓存命中计数不增；None 不写缓存；缓存写失败不影响返回值
  - [ ] xfail 防死循环全序列：坏 pdf→meta 有 chunks:0/xfail→第二遍零变更→修复+mtime 变化→第三遍出块；哨兵 unreadable 两轮判稳
  - [ ] TBD 重 pdf → 终态条目 reason=tbd → 不再触发重建（红队 B1 回归）
  - [ ] 源目录零写入快照断言
  - [ ] MinerU 双后端未装自动 SKIP
  - [ ] 静态断言：kb_stale/_index_core 含 _load_text 不含裸 fpath.read_text
- [ ] **10. tests/verify_export_import.py L149-151**：rglob("*.md") 计数 → manifest vault_files[].rel 权威清单集合比对
- [ ] **11. R1 回归出口**：六件套全绿（audit 19/19 / library_registry 14/14 / server_singleton 5/5 / test_config_editor / test_gui_store / verify_export_import）
- [ ] **12. R1 文档**：TASK_LOG.md 追加条目；AI_GUIDE.md 补「MinerU-Open-CLI 安装」段（flash-extract 免注册 ≤10MB/20页；extract 免费 Token 200MB/200页）

### R1 用户侧启用

```powershell
# 装 MinerU-Open-CLI（一次性，见 AI_GUIDE）
python library.py config "Obsidian Vault" --set extensions=md,pdf,docx
python index.py --library "Obsidian Vault"
```

---

## R2 — MinerU 本地部署及联调

- [ ] 本机安装：Python 3.12 独立 venv + `uv pip install "mineru[pipeline]"`（~2-3GB，纯 CPU 不占显存；模型首跑下载 ~870MB）
- [ ] mineru-local 后端实测联调：真实扫描件跑通；核对输出目录旗标、退出码语义、树杀有效性、UTF-8 输出
- [ ] timeout 实测校准（默认 600s 是否合理，按本机页耗时调整）
- [ ] xsrc 自愈端到端验证：pdf 先以 cloud 索引 → 配置切 local → 自动同步触发重试且缓存键正确失效
- [ ] 性能记录：页级耗时、内存峰值、与 bge-m3 编码并发的资源互扰观察
- [ ] TASK_LOG 追加 R2 记录；评估是否把默认 backend 翻转为 mineru-local（隐私优先）

---

## R3 — GUI 适配

- [ ] config_editor：「PDF/OCR 提取」组注释文案完善；backend 下拉候选说明
- [ ] gui/widgets.py 库设置页：extensions 字段旁标注支持的格式与各格式当前可用性（如 mineru 未装时提示）
- [ ] 索引进度：converting 相位在 GUI 进度区的展示（含长 OCR 的预期管理文案）；heartbeat_state 豁免与 server 侧 progress_text 对齐（双看门狗一致，防一边正常一边弹卡死）
- [ ] 提取失败文件的 GUI 可见性：列表/统计中区分 xfail 条目，给出处置指引
- [ ] TASK_LOG 追加 R3 记录

---

## 明确不做（备案）

- 既有 md 空正文守卫不落 meta 的隐患（另立任务）
- GUI/server tbd_ratio 口径不一致（gui/store.py:69 不传 ratio，既有偏差）
- OCR 结果页级进度 tick（个人库规模下 converting 相位豁免双看门狗已够）
- pymupdf4llm 对恶意 pdf 的解析隔离（单用户本地威胁模型，风险可接受）
