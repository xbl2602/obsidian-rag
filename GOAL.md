# 当前目标

## 目标
完成 R3a：扫描件 PDF 经 MinerU 云端 API 自动 OCR 入索引（R3b 本地部署搁置）；OCR 相关配置（含 API Key）可在 config.json 与 GUI 双端设置，天然后写覆盖。

## 验收标准

- **C1 配置层**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); import config; errs=config.template_consistency_errors(); assert not errs, errs; assert 'mineru_api_key' in config.DEFAULTS and config.DEFAULTS['pdf_scan_backend']=='none'; print('C1 PASS')"
  ```
  预期：C1 PASS（模板与 DEFAULTS 一致；后端默认安全值 none=维持现状）。
- **C2 提取器后端框架 + 云端客户端（mock HTTP 全覆盖）**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\test_extractors.py
  ```
  预期：退出码 0；含新增用例——scanned 文件在后端可用时自动走 OCR 转正、API 失败折叠为 extract-failed 终态不炸轮次、缓存键含 backend 维度。
- **C3 xsrc 重试语义**：存量 scanned/extract-failed 终态条目在后端能力变化后自动重试转正（测试断言），且 Agent 门禁冻结优先级不受影响。
- **C4 GUI 设置组落位**：
  ```powershell
  Select-String -Quiet -Pattern "PDF/OCR|mineru" gui\config_editor.py; Select-String -Quiet -Pattern "mineru_api_key" gui\config_editor.py
  ```
  预期：均 True（GUI 设置页可改 API Key 等键；与 config.json 同一存储，后保存者生效）。
- **C5 六件套全绿 + 文档**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\audit_regression_test.py; if ($?) { Select-String -Quiet -Pattern "问题 26" TASK_LOG.md }; if ($?) { Select-String -Quiet -Pattern "MinerU" AI_GUIDE.md }
  ```
  预期：audit 19/19、TASK_LOG 有问题 26、AI_GUIDE 含 MinerU 段；另完整跑六件套。

## 范围
- 做: 后端分发（none/mineru-cloud；mineru-local 留桩报未支持）、HTTP 客户端（提交→轮询→下载 zip→取 md）、子进程零依赖方案、缓存键加 backend 维度、终态 xsrc 记录+失配自动重试、config 四~五键+模板+GUI 组、mock 单测、文档
- 不做: R3b 本地部署联调；真实云端冒烟（无 Key，待用户提供后单独验证）；push

## 注意事项
- API 契约以官方文档为准（实现前先查证）；外部调用全部带超时+异常折叠，绝不抛出 extractors 边界
- 默认 pdf_scan_backend=none：不装/不配 Key 的用户行为与 R2 完全一致
- API Key 属敏感值：不写入日志/traceback
- 最新输入覆盖语义由单一存储天然满足：config.json 与 GUI 编辑的是同一文件，后保存者胜

## 当前进度
- [x] C1 配置层（三键入 DEFAULTS/模板/_POSITIVE_KEYS；默认 none 安全值）
- [x] C2 后端框架+客户端（mock HTTP 全覆盖：happy/no-key/fail-fold/缓存路由维度）
- [x] C3 xsrc 重试语义（签名失配穿透快速路径，转正后幂等；门禁冻结优先级不变）
- [x] C4 GUI 设置组（config_editor GROUPS「扫描件 OCR」三键；test_config_editor 守护通过）
- [x] C5 回归+文档（六件套全绿；TASK_LOG 问题 26、AI_GUIDE MinerU 段、TODO R3a 标记）

## 上一目标完成记录（2026-08-24）
人机分权门禁已交付（d02bda0）：默认多格式开启、agent_formats 门禁+GUI 开关（7b9ecd0 合并单开关）、格式撤销/删除语义回归（eca3918）。六件套全绿。
