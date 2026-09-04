# 安全官评审 · 阶段1（round-plan-2）

## 总评
安全四维（注入/密钥/权限/输入校验）逐面核过：子进程提取链路路径仅作数据使用、写面收敛于 data/extract_cache，无 shell；api_key 不进任何错误消息、chips、落盘日志（红线 8 成立）；MCP 工具库选择全白名单、extensions 校验大小写/双扩展名均堵死；GUI 渲染面（ft.Text/ft.Markdown 无 raw HTML）无注入执行路径。未发现 blocker。

**无 blocker。**

- [🟡建议] B1：MinerU 预签名 URL 可经 requests 异常文本落入持久化日志 — extractors.py:443,466,478-481 + gui/worker.py:78-102 — PUT 上传与 zip 下载用预签名 URL（URL 本身是凭证），失败时 str(e) 携带完整签名 URL，经 _warn_once 打到 stderr 并由 worker.py _stamp() 落盘 data/gui_index.log。建议三阶段异常只记 {e.__class__.__name__} + 阶段名。
- [🟡建议] B2：reindex_knowledge(allow_new_formats=true) 为 agent 自声明式授权缺事后审计痕迹 — server.py:281-288 — 建议 set_config 写 agent_formats 时向 GUI 日志区打显著 WARN 行。
- [🟡建议] B3（轻微可不改）：_preview_job 异常文本含完整路径显示于 chips — extractors.py:495 + widgets.py:1950-1952 — 本地单机可忽略；顺手可与 B1 同口径只记类名。

🟢 可忽略（核实过）：sanitize_render_md 定位为显示卫生非安全边界 / Markdown 无 raw HTML 执行面未挂自动跟链 / LogView+chips 纯文本渲染 / 缓存键无构造空间 / 后缀白名单大小写双扩展名全堵 / MCP 库名白名单+folder 内存过滤 / api_key 全链路不进日志与 UI / config.json 本地明文属单机惯例 / MinerU 上传为已披露功能语义。

结论：通过（阻断数 0；B1/B2 建议随本轮顺手采纳，不构成返工条件）。
