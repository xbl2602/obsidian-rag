# 当前目标

## 目标
把当前工作树提交为 R1 成果，随后执行 TODO.md 的 R2（GUI 适配：converting 相位展示、xfail 终态可见性、extensions 格式提示）。

## 验收标准

- **C1 R1 已提交且跟踪文件干净**：
  ```powershell
  git log -1 --format=%s | Select-String -Quiet -Pattern "R1"; (git status --porcelain --untracked-files=no | Measure-Object -Line).Lines -eq 0
  ```
  预期：两条均 True（最新提交信息含 R1；已跟踪文件无未提交改动。.opencode 会话产物不入库，保持 untracked）。

- **C2 GUI 相关既有测试不红**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\test_gui_store.py; if ($?) { .venv\Scripts\python tests\test_config_editor.py }
  ```
  预期：两个都 0 failures、退出码 0。

- **C3 GUI 层新能力静态落位**（converting 相位豁免 + xfail/reason 可见性）：
  ```powershell
  (Select-String -Pattern "converting" gui\widgets.py,gui\store.py,gui\app.py -ErrorAction SilentlyContinue | Measure-Object).Count -ge 1; (Select-String -Pattern "xfail|reason" gui\store.py,gui\widgets.py -ErrorAction SilentlyContinue | Measure-Object).Count -ge 1
  ```
  预期：两条均 True。

- **C4 六件套回归仍全绿**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\audit_regression_test.py; if ($?) { .venv\Scripts\python tests\library_registry_test.py }; if ($?) { .venv\Scripts\python tests\server_singleton_test.py }; if ($?) { .venv\Scripts\python tests\test_config_editor.py }; if ($?) { .venv\Scripts\python tests\test_gui_store.py }; if ($?) { .venv\Scripts\python tests\verify_export_import.py }
  ```
  预期：全部正常结束，无 FAILED/Traceback。

- **C5 文档落位**：
  ```powershell
  Select-String -Quiet -Pattern "R2" TASK_LOG.md
  ```
  预期：True（TASK_LOG 有 R2 完成记录）。R2 的 GUI 行为细节（肉眼观感）以人工确认为准，见注意事项。

## 范围
- 做:
  - 前置：将当前改动（extractors.py、index/library/check_notes、测试×2、TODO/TASK_LOG/AI_GUIDE/GOAL、requirements）作为单个 R1 提交
  - gui/widgets.py 库设置页：extensions 字段旁标注支持格式（md/txt/pdf/docx）与扫描件暂不支持提示
  - 索引进度：converting 相位在 GUI 进度区展示；heartbeat_state 停滞判定对 converting 豁免（与 index.progress_text 口径一致）
  - xfail 终态可见性：库统计/列表区分提取失败条目（含 reason），给处置指引文案
  - TASK_LOG.md 追加 R2 记录
- 不做:
  - 启用真实 vault 的 extensions、重建真实索引（行为变更，单独请示用户）
  - 扫描件 OCR / MinerU / config 新键（R3）
  - server.py 检索口径改动（retriever 链路零改动原则不变）

## 注意事项
- R1 已完成并验证（六件套全绿，明细 TASK_LOG 问题 23）；本目标先固化成果再动 GUI
- gui/widgets.py 约 1562 行，改动前必须先读相关段落，遵循现有代码风格；flet 版本 0.86.5
- GUI 视觉效果无法纯机器断言的部分（文案观感、布局），完成后向用户人工确认
- 提交信息用中文、feat: 前缀，风格对齐仓库既有提交

## 当前进度
- [ ] C1 R1 提交落位
- [ ] C2 GUI 既有测试不红
- [ ] C3 GUI 层 converting/xfail 能力静态落位
- [ ] C4 六件套全绿
- [ ] C5 TASK_LOG R2 记录

## 上一目标完成记录（2026-08-24）
R1 全部交付：依赖锁定（pymupdf 1.28.2 / pymupdf4llm 1.28.2 / python-docx 1.2.0）、extractors.py（DOCX+文字层 PDF→Markdown、缓存、扫描件 scanned 终态）、index.py v9（_load_text 字节指纹、统一终态、一致性自愈推广）、16 项新测试、六件套全绿（19/19、14/14、5/5、0 failures、0 failures、39/39）、真库自愈 1490→1594 块。明细见 TASK_LOG.md 问题 23。
