# 笔记命名规范与扫描工具 — 设计

- 日期：2026-08-14
- 状态：已批准
- 关联：Obsidian RAG 检索质量（v6 起文件名/title/tags 是检索锚点）

## 背景与动机

v6（F20）起，笔记**文件名 + frontmatter title + tags** 与正文标题链一起拼进每个块的嵌入/BM25 文本，
成为检索锚点。当前 vault 只有零散规则（"标题写清主题""子标题带上下文"），
**没有文件名命名规范、没有标题层级体系、没有校验手段**，导致：

- 纯英文/纯数字文件名（如 `HEBAT3_ORK_Design_Parameters.md`）中英混合查询命中弱（Roadmap 已列）
- `## 2.1` 纯编号标题链无语义
- 无标题文件整篇成块，无锚点
- 存量治理无入口

## 目标

1. 建立**约定**：根 AGENTS.md + 使用指南 + 相关模板，AI 日常写入遵守
2. 建立**扫描**：只读脚本，输出不合规清单 + 改名映射表（含引用关系），只报不改
3. 治理存量：扫描提供真实清单，逐个人工/AI 确认后再改名

## 硬性约束（来自需求）

1. **不碰正文**：扫描只读；报告不内嵌正文，只输出路径 + 标题行上下文（每处 ≤1 行）
2. **元数据保持最新**：改名/落地时必须同步 frontmatter `updated`、`目录.md`、区 `LOG.md`、`01-Meta/LOG.md`
3. **检查引用关系**：扫描前建全库 wikilink 反向索引；改名映射表每条附入链清单与风险标注
4. **先 git 基线**：执行前两仓库 git status + 提交未提交改动（已做）

## 检测规则

### 硬规则（扫描必报）

| # | 规则 | 判定 | 依据 |
|---|---|---|---|
| R1 | 文件名须含中文主题词 | 文件名无任何 CJK 字符 | v6 文件名是锚点；纯英文文件名中文查询命中弱 |
| R2 | 文件名自然短语 | 含 `_`、双冒号 `::`、连续空格、`:` 等 | 无连字符噪声，语义检索/BM25 最佳 |
| R3 | 正文须有标题 | 文件正文无任何 `#` 标题 | 无标题=整篇成块，无标题链可锚定 |
| R4 | H2/H3 不得纯编号 | 标题全为 `^#+ \d+[.\d]*$` | 标题链拼进块文本，`2.1` 无语义 |

### 软规则（单独分组，不算违规）

- H1 与文件名不一致（v6 有去重，不伤质量，统一更干净）
- frontmatter 无 `title`（仅短文件整篇成块时用得上）

### 豁免集（命中即跳过，不报）

- 结构文件：`MOC-*`、`LOG.md`、`目录.md`、`Home.md`、`AGENTS.md`、`templates/` 目录
- 代码/数值命名：`_` + 大写缩写/数字特征（如 `HEBAT3_ORK_Design_Parameters`）
- 纯日期命名、`00-Inbox/` 未处理区

## 扫描产物

```
=== 不合规清单（需人工处理） ===
[R1] 10-Areas/.../Thrust Equation.md  (created 2026-07-02, updated 2026-08-01)
      建议新名: 推力方程 Thrust Equation.md
      入链: [[Thrust Equation]] ← 3 处 (MOC-Aerospace.md, 概念/.., ..)

=== 软规则建议 ===
[H1] 30-Resources/Ideas/x.md  ← H1 "My Note" 与文件名不符

=== 改名映射表（需人工确认） ===
旧名 | 建议新名 | 入链数 | 风险
```

- 建议新名只从文件内已有中文上下文（H1 / frontmatter `title` / tags / 正文首词）提取，脚本无 LLM 不做翻译
- 被大量引用的文件（入链数 ≥3）标注 ⚠️ 高风险

## 落地路径

1. `tools/check_notes.py` 实现（只读 + wikilink 反向索引 + 报告）
2. 跑主 vault 扫描，验证规则与报告格式
3. 规范写入根 AGENTS.md（新增"笔记命名规范"小节）+ 使用指南"写笔记规范"表
4. Roadmap 关联：顺带提供"全英文笔记标题中文化"存量治理入口

## 技术细节

- 脚本放 `C:\Users\xbl26\projects\obsidian-rag\tools\check_notes.py`
- 复用 `library.py` 的库配置与排除名单：`python tools/check_notes.py [库名]`（默认全部注册库）
- CJK 判定：`any('\u4e00' <= ch <= '\u9fff' for ch in name)`
- 标题提取：复用 `index.py` 的 `split_by_headings` 逻辑（或独立轻量实现）
- wikilink 反向索引：扫描所有 md 的 `[[...]]`，解析目标为文件名（去 `|别名`、去 `#锚点`、去 `folder/` 前缀），建 目标文件名 → 引用来源 映射
- 命令行：`--json` 可选输出机器可读 JSON

## 2026-08-14 修订（覆盖范围 / 豁免 / 判断原则）

- **默认只扫 Obsidian Vault 主库**：agents/skills/test 是 opencode agent 定义 / skill / 测试文件，
  非笔记，不按笔记命名规则评判。`--all` 才扫全部注册库。
- **入链反向索引扫整个 vault**（含被索引排除的 目录/LOG/MOC 等）：改名影响所有引用者，
  不只可索引文件。示例：Kalman-Filter 入链从 0 修正为 4 处。
- **豁免增强**：归档区 `90-Archive/`（含 `*-AGENTS.md`/`*-LOG.md` 等加前缀结构变体，
  归档区命名规范明确支持）、`TODO/`、`任务节点/`、`Clippings/`、`graph-ignore-*`、
  `README.md` 及变体、`_` 前缀工作流文件。
- **扫描结果只是候选清单，不是指令**：AI 必须逐条判断文件真实角色再决定是否改名，
  结构性/功能性文件即使命中规则通常不改。此原则写入 vault 根 AGENTS.md。
- **CODE_NAME_RE 补丁**：AGENTS.md 豁免清单示例 `HEBAT3_ORK_*`（字母+数字+下划线的代码/数值命名）
  未被原正则匹配而误报；补一个「纯 ASCII + 含数字 + 含下划线」分支修复（不可含 CJK，避免误伤
  `A1_认知模型` 这类中文名）。补丁后硬违规 55 → 49。

## 2026-08-14 改名落地（39 项）

- 逐条判断后执行 **39 项改名 + 10 项不动**（功能性/结构性：Roadmap 台账、OfficeCLI-SKILL、
  report_checklist/questions 清单、ansys/y+ 附录引用集、空文件 Ideas、user-profile B2-B5 代码命名）。
- 全部 `git mv` + 全库 wikilink/`related` 精确基名替换（含 90-Archive/目录/LOG/MOC），正文零改动，
  frontmatter `updated` 同步为 2026-08-14，区 LOG + 01-Meta/LOG 记录。
- 两个 `AI Agent.md`（AI Dev Workflow / AI Knowledge System）语义接近但分属两项目，
  保留同名新名 `AI Agent 智能体`，与改名前的裸链接解析行为一致。
- 落地后重扫：硬违规 49 → **10**，剩余全部为上述判定不动的功能性文件（预期内）。
