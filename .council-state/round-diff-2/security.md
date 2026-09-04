# 安全官 round-diff-2 复审 — 全部6处修复新增代码专项

## Blocker
无。

## Note（均非blocker）
- N1（值得关注）：试验台被强杀（超时/关闭对话框）时_preview_job的tempfile.TemporaryDirectory不会走finally清理（terminate()不触发子进程内用户态清理代码），临时目录可能以extract_preview_*前缀残留在系统临时目录，其中可能含已上传/下载过的云端OCR扫描件内容。建议后续由父进程预先创建临时目录、以路径传给子进程，杀死后父进程负责rmtree。
- N2：新增_warn_once日志调用（extractors.py:383-384）确认不含敏感信息，_mineru_cloud_extract内部已有独立except先行截获敏感异常。
- N3：SettingsDialog._save大小写/空白比较逻辑正确，无绕过风险。
- N4：确认框UI走查无欺骗性，本地桌面场景风险可忽略。
- N5：index.py常量化重构无安全相关性。

## 总体结论
本轮新增代码通过复审，不构成blocker。N1建议列入后续修复项。
