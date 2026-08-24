# TODO — 多格式文档支持（Word + 文字层 PDF 优先，扫描件 OCR 末轮）

> 状态：方案已定稿（v2，经 council 三委员评审修订），分三轮交付。
> 评审记录：`.council-state/round-plan-1/`（8 个 blocker 已全部吸收进本清单）。
> 基线：HEAD 92af6e6，META_VERSION 将 8→9。
> 优先级修订（2026-08-24）：扫描件 OCR（MinerU 云端+本地）整体后移到最末轮；当前唯一焦点 = **R1：Word + 文字层 PDF**。

## 交付轮次

| 轮次 | 范围 | 出口标准 |
|---|---|---|
| **R1（当前焦点，最小可交付）** | CLI 全链路 + **DOCX + 文字层 PDF** | `library.py` 开 extensions → `index.py --library` 端到端索引 md/docx/文字层pdf；扫描件 pdf 干净跳过（终态防死循环）；六件套回归全绿 |
| **R2** | **GUI 适配** | 设置页/进度展示对多格式的完整体验 |
| **R3（末轮）** | **扫描件 OCR：MinerU 云端 API → 本地部署** | 云端路径可用后接本地 pipeline 联调，xsrc 自愈验证 |

## 核心思想

索引时把非 MD 文件转成 Markdown，复用既有整条切块管线；源文件零写入；
一切「不产块的文件」必须落持久化终态防重建死循环。

## 架构 v2 关键机制（实施时不得偏离）

### A. 统一终态机制（封堵五个死循环入口）
- `_load_text(fpath)` → `(text | None, 字节hash | "unreadable")`：
  - 空串/纯空白归一为 None（堵空产出入口）
  - OSError 不上抛，hash 用哨兵值 `"unreadable"`（锁定/OneDrive/AV 场景两轮哨兵等值 → 自动判稳；解锁后真实 hash ≠ 哨兵 → 自动重试）
  - 后缀路由一律 `suffix.lower()`（堵 `.PDF`/`.MD` 静默 xfail）
- `_index_core` 分支顺序：**None 判定严格先于 is_tbd_heavy**（否则 content=None 炸 AttributeError 废整轮）
- 非正常路径统一出口：
  ```python
  meta[rel] = {"hash", "chunks":0, "size", "mtime",
               "xfail":True,
               "reason":"unreadable|extract-failed|empty|tbd|scanned"}
               # scanned = 扫描件暂不支持（R1），R3 接入后端后自动重试转正
               # 且必须 current_rels.add(rel) 使条目持久化
  ```
- kb_stale 对二进制源：`extract=False` 拿字节哈希比对即可判「真没变」，绝不跑转换（GUI 每秒轮询的零成本边界）；entry 为 None 的失败新文件照常计 added 一次 → 触发一次重建落终态 → 此后稳定收敛

### B. 单一事实来源与谓词收拢
- extractors.py 导出 `TEXT_EXTS={md,txt}`、`BINARY_EXTS={pdf,docx}`、`SUPPORTED_EXTS`
- index 的 TEXT_SOURCE_EXTS 与 library 的白名单校验都引用它，禁止三处硬编码
- 单点谓词 `_skipped(info): bool(info.get("tbd") or info.get("xfail"))` 收拢四处判断

### C. 预留给 R3 的机制（R1 不实现，仅留口）
- 终态条目 `xsrc` 字段 + kb_stale 失配自动重试（换后端无需 --full）
- 缓存键 backend 维度：R1 用 `<md5>.v<EXTRACT_VERSION>`，R3 升级为 `<md5>.<backend>.v<N>`
- 子进程卫生全套（树杀/临时目录/UTF-8 解码/超时配置）

---

## R1 — 当前焦点：CLI 全链路 + DOCX + 文字层 PDF（无 OCR、零新增配置键）

> ✅ **已完成（2026-08-24）**：全部条目落地并通过六件套回归（test_extractors 16/16 新增；
> 其余五件全绿）。明细与实测数字见 TASK_LOG.md 问题 23。扫描件处理方式有微调：
> 提取入口返回 `(md, reason)` 二元组以支撑终态分类；空正文 md 一并落 empty 终态
> （顺带收敛了「明确不做」里的备案隐患）。

- [ ] **1. requirements.txt + 安装验证**：pymupdf / pymupdf4llm / python-docx（Py3.14 轮子验证，lxml 是唯一硬风险；失败即停回报，退路=docx 优雅降级）。版本号安装成功后回填锁定
- [ ] **2. extractors.py**（新文件，只依赖 config.CFG，无子进程、无后端分发）：
  - [ ] `extract_to_markdown(path) -> str|None`：唯一入口，绝不抛异常、绝不写源目录
  - [ ] DOCX 路：python-docx 按 body 子元素保序遍历；Heading N/标题 N 样式→#×N（>3 钳 ###，兜底 base_style）；表格→管道表（\| 转义、单元格换行→空格）
  - [ ] PDF 路：fitz 探测文字层覆盖率（≥0.5 页占比）→ pymupdf4llm.to_markdown()（list 返回值 join 归一）；**扫描件为主 → 返回 None + warn_once(说明 R3 前暂不支持)，由 index 记 reason="scanned" 终态**
  - [ ] 缓存层：键=`<md5>.v<EXTRACT_VERSION>`；原子写 tmp 带 pid + os.replace；**写失败仅跳过缓存照常返回结果**；None 不写缓存；启动清扫 >24h 孤儿 *.tmp；目录可注入参数覆盖（测试隔离）
  - [ ] 懒加载 import（pymupdf/python-docx 未装时对应格式优雅降级 None + 安装提示）
- [ ] **3. index.py**：
  - [ ] META_VERSION 8→9（v9 注释：多格式提取+原始字节指纹）
  - [ ] `_load_text()` 统一入口（终态机制 A 全部语义在此）替换 kb_stale/_index_core 两处裸 read_text
  - [ ] kb_stale：二进制源走字节哈希快速比对；removed/expected 排除口径改用 `_skipped()`
  - [ ] _index_core 主循环重排（None→tbd→哈希短路→终态落盘→正常切块）；frontmatter 仅对 TEXT_EXTS 生效
  - [ ] 提取前 update_progress(phase="converting")；progress_text 加 converting 豁免分支
  - [ ] `__main__` 多库循环逐库 try/except（一库失败不连坐）
  - [ ] 单点谓词 `_skipped(info)`
- [ ] **4. library.py**：set_config extensions 白名单校验引用 SUPPORTED_EXTS；小写归一去重保序
- [ ] **5. retriever.py**：零改动（已核实块元数据链路天然兼容）
- [ ] **6. tools/check_notes.py**：scan_library 对非 TEXT_EXTS 跳过正文分析（修二进制 UnicodeDecodeError 崩溃）
- [ ] **7. tests/test_extractors.py**（新文件，风格对齐 audit_regression_test.py 非 pytest）：
  - [ ] docx 回环（标题层级/管道表/正文）；pdf 文字层生成提取
  - [ ] 大写扩展名 `.PDF`/`.Docx` 路由正确（红队 B3 回归）
  - [ ] 伪造二进制→None 无堆栈；空 docx→None 走 xfail
  - [ ] 缓存命中计数不增；None 不写缓存；缓存写失败不影响返回值
  - [ ] xfail 防死循环全序列：坏 pdf→meta 有 chunks:0/xfail→第二遍零变更→修复+mtime 变化→第三遍出块；哨兵 unreadable 两轮判稳
  - [ ] TBD 重 pdf → 终态条目 reason=tbd → 不再触发重建（红队 B1 回归）
  - [ ] 扫描件 pdf（无文字层）→ 终态 reason=scanned → 不触发重复重建
  - [ ] 源目录零写入快照断言
  - [ ] 静态断言：kb_stale/_index_core 含 _load_text 不含裸 fpath.read_text
- [ ] **8. tests/verify_export_import.py L149-151**：rglob("*.md") 计数 → manifest vault_files[].rel 权威清单集合比对
- [ ] **9. R1 回归出口**：六件套全绿（audit 19/19 / library_registry 14/14 / server_singleton 5/5 / test_config_editor / test_gui_store / verify_export_import）
- [ ] **10. R1 文档**：TASK_LOG.md 追加条目；AI_GUIDE.md 注明「扫描件 pdf 暂不支持，计划末轮接入 MinerU」

### R1 用户侧启用（零额外安装）

```powershell
python library.py config "Obsidian Vault" --set extensions=md,pdf,docx
python index.py --library "Obsidian Vault"
```

---

## R2 — GUI 适配

- [ ] gui/widgets.py 库设置页：extensions 字段旁标注支持的格式（md/txt/pdf/docx）与扫描件暂不支持的提示
- [ ] 索引进度：converting 相位在 GUI 进度区的展示；heartbeat_state 豁免与 server 侧 progress_text 对齐（双看门狗一致，防一边正常一边弹卡死）
- [ ] 提取失败文件的 GUI 可见性：列表/统计中区分 xfail 条目（含 reason），给出处置指引
- [ ] TASK_LOG 追加 R2 记录

---

## R3（末轮）— 扫描件 OCR：MinerU 云端 API → 本地部署

### 3a. 云端 API（先做）
- [ ] MinerU-Open-CLI 接入：flash-extract 免注册（≤10MB/20页）；extract 免费注册 Token（200MB/200页）
- [ ] config.py 四键（DEFAULTS+CONFIG_TEMPLATE 同步）：`pdf_scan_backend`（默认 mineru-cloud）/ `mineru_cloud_cmd` / `mineru_local_cmd` / `mineru_timeout_seconds`（进 _POSITIVE_KEYS）
- [ ] gui/config_editor.py GROUPS 加「PDF/OCR 提取」组收编四键（缺这步 test_config_editor 必红）
- [ ] 后端分发框架 + 子进程卫生全套：列表参数禁 shell=True；UTF-8 显式解码；超时可配；超时 taskkill /T /F 树杀+proc.wait() 收尸；CREATE_NO_WINDOW；输出目录=data/ocr_cache/tmp-<pid>-<ts>/，rglob("*.md") 取最大者，try/finally 清理
- [ ] 终态条目引入 xsrc 字段 + kb_stale 失配自动重试；缓存键升级 `<md5>.<backend>.v<N>`
- [ ] AI_GUIDE 补「MinerU-Open-CLI 安装」段
- [ ] 测试：reason=scanned 的存量终态条目在接入后端后自动重试转正的端到端用例

### 3b. 本地部署联调
- [ ] 本机安装：Python 3.12 独立 venv + `uv pip install "mineru[pipeline]"`（~2-3GB，纯 CPU 不占显存；模型首跑下载 ~870MB）
- [ ] mineru-local 实测：真实扫描件跑通；核对输出目录旗标、退出码语义、树杀有效性、UTF-8 输出
- [ ] timeout 实测校准；性能记录（页级耗时、内存峰值、与 bge-m3 编码并发的资源互扰观察）
- [ ] xsrc 自愈端到端验证：cloud 索引 → 切 local → 自动重试且缓存键正确失效
- [ ] TASK_LOG 追加 R3 记录；评估是否翻转默认 backend 为 mineru-local（隐私优先）

---

## 明确不做（备案）

- 既有 md 空正文守卫不落 meta 的隐患（另立任务）
- GUI/server tbd_ratio 口径不一致（gui/store.py:69 不传 ratio，既有偏差）
- OCR 结果页级进度 tick（个人库规模下 converting 相位豁免双看门狗已够）
- pymupdf4llm 对恶意 pdf 的解析隔离（单用户本地威胁模型，风险可接受）

## Backlog（按需触发，当前不排期）

- **图片内容打标**：现管线只索引文字/表格/公式/图注，图片本身的内容（示意图/曲线/照片）不入索引。
  触发条件：实际使用中发现「已知某图的信息检索不到」。
  届时方案：extractors 增加可选增强阶段——抽取 `![](images/*)` 占位对应图片 → 本地 VLM
  （LM Studio 多模态模型，复用 hyde_llm_url 配置模式）生成中文描述 → 以「【图表内容：…】」
  注入原位置进切块管线。架构口子已预留，无需改动既有设计。
- MinerU VLM/hybrid 后端（需 ~7.7GB 显存，8GB 卡与 bge-m3 冲突，除非换卡否则不可行）
