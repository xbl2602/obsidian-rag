# diff 门第 1 轮评审意见索引（round-diff-4）

| 委员 | 判定 | Blocker |
|---|---|---|
| architect | 🔴 BLOCKER ×1 | B1: index.py:643+647 `del old` 在 try 内先于 log/_report_device，异常窗口内回滚引用已删名字 → UnboundLocalError 掩盖原异常、模型状态损坏。修复：del 移出 try 或 old=None + 补 monkeypatch _report_device 抛哨兵的回归用例 |
| reliability | 🔴 BLOCKER ×1 | 同一处同一根因（独立发现）。补充证据：log 实现为裸 print(stderr)，管道断裂真实可达；要求补复现用例 |
| security | ✅ PASS | S1/S2/S3 全落地双侧镜像；bool 注入攻击三处闭环；无注入面；测试隔离合规 |
| product | ✅ PASS | 四态正交（符号/措辞/量化/颜色）；waiting-lock 全链路中文无裸英文；stall_timeout 调小推演自洽 |
| redteam | ✅ PASS | 规格偏差 D1-D6 全无害；慢批链缝隙攻击不成立（清除性更新本身刷新 advance）；bool 时间戳攻击三处闭环；:647 问题定级遗留代码非本次恶化（但建议顺手修） |
| performance | ✅ PASS | 三项性能承诺全部实读证实；埋点零新增写盘；锁持有 <1% 影响 |

## 合并终裁
唯一 blocker = **B1（index.py `_try_switch_back_cuda` 回滚路径变量解绑定）**，architect 与 reliability 独立一致报告，文件:行号主键相同。
两位均明示：修复为一行级 + 一条回归用例，修毕无需全轮复审，checker 核对该用例即可放行。

## 非阻塞建议池（供收尾参考）
- architect S2: audit:537 AST 断言加 isinstance FunctionDef 前置断言
- reliability 🟡: 判侧可加 math.isfinite 防 Infinity/超大值放行近永久豁免
- reliability N2 族 TOCTOU: 快照后任务 finish 的终态文案噪音，有界自愈备案
- product: server.py index_status docstring 可补一句宽限语义；heartbeat_note 按 waiting-lock 相位细分文案；hint 可加"内置宽限不随本值缩放"
- redteam: δ 二次索引起 started_at 残留（既有缺陷）建议另立问题编号；C3-4 循环前预置 prev=0.0 使断言每轮实判；清理仓库根遗留 scratchpad 临时文件并入 .gitignore
- performance N1: app 每秒双读进度文件的既有冗余，后续可参数化收敛
