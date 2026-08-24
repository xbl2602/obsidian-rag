# 当前目标

## 目标
实现多格式的「人机分权」：默认开启 pdf/docx 索引（人经 GUI/CLI 完全控制），Agent 触发的索引（含检索自动同步）默认仅限文本类，二进制格式须经用户一次性批准（持久化、可撤销）。

## 验收标准

- **C1 默认开启**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -X utf8 -c "import sys; sys.path.insert(0,'.'); from library import effective_config; assert effective_config({'name':'t','path':'.'})['extensions']==['md','pdf','docx']; print('C1 PASS')"
  ```
  预期：打印 C1 PASS（entry.extensions 为 null 时继承新默认）。

- **C2 Agent 门禁语义**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\test_extractors.py
  ```
  预期：退出码 0，且包含新增用例——受限模式下：① 未授权二进制文件不被转换/嵌入；② 其既有 meta 条目与块被保留（不被裁剪清理）；③ 解除限制后自动补齐。

- **C3 注册表校验**：agent_formats 键合法（⊆ 当前 extensions 且仅二进制类）、非法值报错：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); import tests.noop" 2>$null; .venv\Scripts\python tests\library_registry_test.py
  ```
  预期：library_registry 全过（含新增 agent_formats 校验用例）。

- **C4 GUI 勾选块落位**：
  ```powershell
  (Select-String -Pattern "agent_formats" gui\widgets.py,gui\app.py | Measure-Object).Count -ge 1; (Select-String -Quiet -Pattern "ft.Checkbox" gui\widgets.py)
  ```
  预期：两条 True（库配置对话框含格式勾选与 Agent 授权勾选）。

- **C5 六件套全绿 + 文档**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\audit_regression_test.py; if ($?) { .venv\Scripts\python tests\test_gui_store.py }; if ($?) { Select-String -Quiet -Pattern "问题 25" TASK_LOG.md }
  ```
  预期：audit 19/19、gui_store 0 failures、TASK_LOG 含本条记录（问题 25）；另完整跑六件套确认无回归。

## 范围
- 做:
  - library.py：DEFAULT_EXTENSIONS=["md","pdf","docx"]；OVERRIDE_KEYS/LIST_KEYS 增加 agent_formats；set_config 校验（小写归一、⊆extensions、仅 BINARY_EXTS）
  - index.py：kb_stale/_index_core 增加 agent_allowed 参数——未授权后缀文件「冻结」（跳过 stat/读取，保留条目与块，不计变更加不触发清理）；全量路径不受影响
  - server.py / retriever.py：Agent 可达的索引入口（reindex 工具与检索自动同步）传受限集合 = TEXT_EXTS ∪ (extensions∩agent_formats)；reindex 工具新增 allow_new_formats 确认参数，确认为真时把新格式写入 agent_formats 持久化；待确认时返回明确提示文案
  - gui/widgets.py：库配置对话框 extensions 改为四个 Checkbox；新增「允许 AI Agent 索引」勾选行（仅列出已启用的二进制格式）；_do_config 保存两类勾选
  - 测试：更新受影响断言（默认 extensions 变更）；新增门禁集成用例（复用 test_extractors 隔离环境）
  - 文档：TASK_LOG 问题 25；TODO 补记该特性
- 不做:
  - 扫描件 OCR（R3）
  - 真实 vault 的重建执行（默认开启后由用户下次索引自然生效，不在本轮手动触发）
  - git push

## 注意事项
- 冻结语义必须保证：Agent 受限轮询绝不裁剪/清理未授权文件的块与 meta 条目（否则等于变相删库）；一致性自愈的期望块数把冻结条目计算在内
- 既有测试若断言旧默认 ["md"]，按新语义更新断言并在提交信息说明
- MCP 工具签名变化（新增可选参数）需向后兼容：不传参=严格模式

## 当前进度
- [x] C1 默认开启（effective_config 缺省 = md,pdf,docx；agent_formats 交集派生）
- [x] C2 门禁语义测试（test_extractors 17/17，新增 agent_gate 冻结/补齐全序列）
- [x] C3 注册表校验（library_registry 15/15，含 agent_formats 校验用例）
- [x] C4 GUI 勾选块（格式四选 + AI 权限行，agent_formats 落位 gui/widgets.py）
- [x] C5 六件套全绿（19/19、15/15、5/5、0f、30 PASS、39/39）+ TASK_LOG 问题 25

## 上一目标完成记录（2026-08-24）
R1（177ede6）+R2（2ac21b5）已交付：DOCX+文字层PDF 提取、统一终态(v9)、一致性自愈、GUI converting 相位与 xfail 可见性；TODO/TASK_LOG 同步完毕（e11afbe/f1877a0）。
