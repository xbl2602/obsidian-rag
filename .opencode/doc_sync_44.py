import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
base = r'D:/_STOREROOM/lol/Obsidian Vault/20-Projects/Obsidian RAG/'

# ---- 1. 操作手册：库管理章节后加勾选范围段 ----
p = base + 'Obsidian RAG 操作手册.md'
t = open(p, encoding='utf-8').read()
anchor = '## 多格式与扫描件 OCR（2026-08-24）'
assert anchor in t
sel_doc = '''## 库内勾选范围（2026-09-06，问题44）

每库可以按**文件/文件夹**粒度决定建不建向量库。GUI：库管理卡片上「勾选范围」→
右侧抽屉里勾选（勾 = 建库，不勾 = 排除；点文件夹名下钻，不越库根）。被排除的文件
**全流程不存在**：不扫描、不嵌入、不 OCR、不建页级导航、检索与读文档工具都不可见。

**优先级**（高→低）：排除名单 exclude_* > 用户显式选择（离文件最近的赢）> 格式开关。
所以：全局关 PDF 后单独勾选某个 PDF，它照样入库；全局再开，其余 PDF 回来。
**格式快捷按钮是批量操作**：关某格式会连带取消该格式文件的全部单独勾选
（文件夹级选择不受影响）。

AI Agent 经 MCP 只能「提议」勾选变更（propose_selection_changes），必须把变更清单
展示给用户、用户同意后带确认码调用 apply_selection_changes 才生效——硬编码门禁，
10 分钟过期，全程审计日志。

''' + anchor
t = t.replace(anchor, sel_doc, 1)
open(p, 'w', encoding='utf-8').write(t)
print('操作手册 done')

# ---- 2. 决策记录 ADR-19 ----
p = base + 'Obsidian RAG 决策记录.md'
t = open(p, encoding='utf-8').read()
adr19 = '''
## ADR-19 路径级勾选建模：排除=对管线不存在，格式开关=批量操作（2026-09-06）

- **状态**：已接受（设计经用户逐项拍板；问题44 已实施）。
- **背景**：用户要按文件/文件夹粒度决定建不建库，且被排除文件要全流程彻底不碰
  （BGE/WEMM/MinerU/pymupdf）；Agent 可经 MCP 提议但必须用户确认。
- **决策**：①排除的语义 = **对管线不存在**——过滤加在 collect_md_files 唯一漏斗
  （stat 之前零 I/O），条目按既有 removed/幽灵路径裁剪、块与 WEMM 页向量清理，
  不新增终态类型；②三态模型：显式勾选/显式排除/中性（跟随格式），**最近显式赢**
  （文件 > 父文件夹 > 格式开关），显式勾选可穿透扩展名白名单；③**无保护概念**——
  全局格式开关=对该格式文件的批量勾/取消（显式勾选跟着取消，用户为简化主动放弃
  保护语义），文件夹级选择不被批量触碰；④MCP 硬门禁两段式：propose 出提案
  （确认码只回显给 agent 转述、盘上只存哈希、10 分钟 TTL）→ apply 校验码才生效，
  诚实边界：agent 可不带用户直接带码 apply（MCP 信任边界），靠过期+一次性+审计
  压风险；⑤GUI 库管理出勾选抽屉（下钻/徽章/攒批保存），GUI=用户本人免确认码。
- **后果**：隐私文件（账单等）可彻底退出检索；新文件默认跟随格式（中性）无需登记；
  全量清单不落盘（只存例外），万级文件库轻量。已知取舍：文件改名/移动后选择丢失
  （按新文件处理），不做移动追踪。
'''
t = t.rstrip() + '\n' + adr19
t = t.replace('updated: 2026-09-06\n', 'updated: 2026-09-06\n')  # 日期已是 0906
open(p, 'w', encoding='utf-8').write(t)
print('决策记录 done')

# ---- 3. Roadmap：规划中 → 已完成 ----
p = base + 'Obsidian RAG Roadmap.md'
t = open(p, encoding='utf-8').read()
old = '## 规划中（2026-09-06）：库内文件/文件夹级勾选建模'
new = '## ✅ 已完成（2026-09-06）：库内文件/文件夹级勾选建模（问题44，见操作手册「库内勾选范围」）'
assert old in t
t = t.replace(old, new, 1)
open(p, 'w', encoding='utf-8').write(t)
print('Roadmap done')

# ---- 4. LOG ----
p = base + 'LOG.md'
t = open(p, encoding='utf-8').read()
t = t.rstrip() + '\n- [0906-Sel] 库内路径级勾选建模：文件/文件夹级排除全流程生效（扫描漏斗唯一过滤）；MCP 两段式确认门禁（提案+确认码+TTL）；GUI 勾选范围抽屉。ADR-19。录毕。\n'
open(p, 'w', encoding='utf-8').write(t)
print('LOG done')
