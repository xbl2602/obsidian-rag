# 架构师 round-diff-2 复审 — B1(kb_stale/_index_core终态判定重复)

## ✅ 已修复，无新问题
5个常量值与原字面量逐字符核对一致，无数据格式破坏风险。_entry_converged与原三处表达式逐词对应，边界情况（entry=None/空字典）处理一致。kb_stale三处映射无错位，_index_core五处_terminal_entry调用全部替换、无遗漏。_backend_changed替换正确。

架构判断：仍需手动在kb_stale/_index_core两处加代码，未变成"改一处两边生效"的强制收拢——但已把"静默失配"风险转化为"引用不存在常量名时立即报错"，在不引入更大重构风险前提下是合理、够用的解法。

结构性测试test_terminal_reason_constants_single_source_of_truth对"无意手滑写回裸字符串"有实际拦截力。

如实指出（非缺陷）：is_tbd_heavy等"触发条件"本身的重复未被这次修复触及，这超出本次blocker的报告范围。

## 总体结论
可以通过，放行。
