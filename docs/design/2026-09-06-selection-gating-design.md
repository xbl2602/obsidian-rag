# 设计文档：库内文件/文件夹级勾选建模（selection gating）

日期：2026-09-06 · 状态：已批准（用户逐项拍板，见"决策"标注） · 实施：问题44

## 目标

用户与 AI Agent 可以按**文件/文件夹粒度**决定库内哪些内容进向量库。被排除的文件
**全流程彻底不碰**：扫描即过滤，BGE / WEMM / MinerU / pymupdf 等一切下游在物理上
触达不了它。AI Agent 经 MCP 提议变更，**硬编码确认门禁**，未经用户确认不生效。

## 数据模型（libraries.json 每库新增）

```json
"selection_in":  ["20-Projects/课件/青苹果菜单.pdf"],   // 用户显式勾选（文件或文件夹）
"selection_out": ["私人/账单/"]                          // 用户显式排除（文件或文件夹）
```

- 路径 = 库内相对路径，`/` 分隔，统一 `normslash` 规范化
- 不在两表 = **中性**，按格式规则（`extensions`，库级覆盖 or 全局）判定
- 新文件天然中性 → 跟随全局格式（用户拍板②）；config 新键
  `selection_new_files: "follow"|"include"|"exclude"`（默认 follow）供调节
- 新建库无两表 = 全部按格式纳入（"默认全部勾选"）
- 文件夹级选择不被格式批量操作触碰（只动文件级条目）

## 生效判定 resolve(lib, rel)（优先级从高到低）

1. **离文件最近的显式选择赢**（用户拍板①）：文件自身 > 父文件夹 > … > 库根。
   例：显式勾选 `课件/青苹果菜单.pdf` + 显式排除 `课件/` → 青苹果入库，其余排除
2. 无显式 → 格式规则：扩展名 ∈ extensions → 纳入，否则排除
3. 既有 `exclude_dirs/files/patterns` 硬排除不变（在此之上，最先执行）
4. 执行位置 `collect_md_files()` 扫描漏斗，stat 之前（零 I/O；agent 门禁分支
   照旧最早期，红线 6 不回退）

## 全局格式开关 = 批量勾选/取消（用户拍板③：无保护概念、无跟随开关）

- 格式 X 关：`selection_in` 中该格式的**文件**条目移入 `selection_out`
  （"青苹果菜单"跟着取消）；中性 X 文件直接变排除
- 格式 X 开：`selection_out` 中该格式的文件条目移除（中性 X 文件变纳入）
- 任何时候可单独勾回/取消单个文件（写 sel_in / sel_out）
- 文件夹级条目永不被批量操作触碰

## 生效与清理（统一终态红线兼容）

勾选变更原子写 libraries.json；**下一轮增量索引**应用：

- 新排除（include→exclude）：条目转 `_terminal_entry(reason="excluded")`
  （防 stale 死循环，红线 2）；其块从 Chroma 删除；WEMM 页向量按 rel 清理
- 新纳入（exclude→include）：正常索引，旧终态自然被成功条目覆盖
- 检索天然生效（块已删）；MinerU 攒批/WEMM 页同步都在扫描漏斗下游，天然隔离

## MCP 硬门禁（用户拍板④：agent 展示并询问，用户不一定开着 GUI）

两段式 + 确认码，硬编码无配置绕过：

- `propose_selection_changes(library, changes)` → 校验（路径在库内/规范化/
  变更非空）→ 写 `data/selection_pending.json`（proposal_id + 随机 6 位确认码 +
  人类可读 diff + 创建时间）→ 返回 diff 与确认码；**绝不直接改库**
- `apply_selection_changes(library, proposal_id, confirmation_code)` →
  码正确 + 10 分钟内 + 未使用 → 原子写 libraries.json + 删 pending + 审计日志
- 如实披露：agent 理论上可不真问用户直接带码 apply——MCP 通道的信任边界；
  两段式 + 过期 + 审计把误操作风险压到最低且全程可追责

## GUI（guiweb 库管理；Flet 备用版不同步，ADR-18）

每库卡新按钮「勾选范围」→ 右侧滑出面板：

- 面包屑下钻/上钻，不越库根（bridge 强制 + UI 无 ..入口）
- 文件夹三态框（全/半/无，半态=子内有分歧）；文件勾选框
- 每行生效态徽章：✅ 入库 / ⛔ 已排除 / ➖ 中性跟随格式；显式选择加点标记
- 目录懒加载（bridge 按目录返回，万级文件不卡）
- 格式快捷批量行（md/pdf/docx…）带"将清除该格式单独勾选"提示
- 改动攒批 + 「保存」统一提交（GUI 操作=用户本人，无需确认码）

bridge 新方法：`selection_tree(lib, dir)`、`selection_update(lib, changes)`

## 已知取舍（YAGNI）

- 路径键 = 相对路径：文件改名/移动后选择丢失，按新文件默认处理（不做移动追踪）
- `selection_new_files=include/exclude` 是把中性态钉死为纳入/排除，仍弱于显式选择
