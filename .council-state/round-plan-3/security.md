# Security 评审（方案门 第1轮）

## 总体判定：PASS

### Blocker 清单
无。

### 审查重点结论
1. 进度 JSON 跨进程解析健壮：read_progress（index.py:248-253）json 异常兜底返回 {}；写入 tmp+os.replace 原子替换；GUI _refresh_loop 有 except Exception 兜底。篡改 stall_grace_until 为字符串不会崩 GUI，但见 S1 fail-closed 建议。
2. update_progress kwargs pop 机制干净；无过滤合并是既有行为非本方案引入，现存调用点键名全为字面量。
3. message 展示全部走 ft.Text 纯文本渲染，无 HTML/markdown 注入面；唯一 ft.Markdown 在试验台且有 sanitize 隔离。
4. 写入原子性与截断容错齐备。
5. 密钥/隐私红线不涉及；进度文件无绝对路径无凭据。

### 非阻塞建议
- S1 判侧读 stall_grace_until 必须 isinstance(v,(int,float)) 校验后参与比较，非法值视为"无宽限"（fail-closed）；项目已有此惯例（store.py:200/218），勿照抄 updated_at/last_advance_at 的裸算术比较（store.py:144,148）。
- S2 stall_grace_s 做 clamp（≤600s）防未来接配置/MCP 参数形成永久静默开关。
- S3 progress_start 显式重置 stall_grace_until（跨轮残留会抑制下一轮早期 stalled 判定）。
- S4 长期建议对未知 kwargs 打告警或白名单过滤。
- S5 （既有全局面，不在本次范围）progress_text 回传文件名的理论 prompt injection 通道。

### 可忽略
- 进度文件本机单用户可写，攻击者等价已持代码执行权，S1 定级建议而非阻断。
