# 安全审查报告 — R1/R2/R3a 多格式文档支持（92af6e6 → d3fbf5c）

只审注入、密钥泄露、权限、输入校验四维。

## 核心红线验证结论（无 blocker）
- API Key 不进日志：确认遵守，所有 raise/log 只拼接状态码，不含响应体全文。
- 密钥落盘：`data/config.json` 被 `.gitignore` 覆盖，未提交进 git。
- 子进程调用：均以参数列表方式调用，未发现 shell=True。
- extensions 白名单：`library.py:762-794` 严格 allow-list 比对 SUPPORTED_EXTS，非法值直接拒绝。
- MinerU zip 处理：只按成员对象读字节到内存，不按文件名落盘，无 zip slip 利用面。

## 发现（均为 note，无 blocker）

**B1** 🟡 note — `gui/widgets.py:876-887` `mineru_api_key` 用普通 TextField 渲染，无密码遮罩，长期明文显示。

**B2** 🟡 note — `extractors.py:397-500` 上传/下载阶段网络异常被同一个 `except Exception as e` 兜底，`str(e)` 可能带出预签名 URL（含临时 token）写入本地日志。API Key 本身不受影响。

**B3** 🟡 note — `tests/library_registry_test.py` 的 zip slip 回归用例未覆盖新增的 MinerU/DOCX zip 消费路径（当前代码走查确认不可利用，但无回归锁定）。

## 总体结论
未发现可立即远程利用的注入、密钥泄露或路径穿越漏洞。三条发现均为纵深防御/覆盖面建议，非 blocker。
