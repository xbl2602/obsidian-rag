# Checker 机器验证报告（diff 门）

| 条目 | 判定 | 证据 |
|---|---|---|
| C1-1 audit_regression_test | PASS | 37/37 通过，EXITCODE=0 |
| C1-2 library_registry_test | PASS | 15/15 通过，EXITCODE=0 |
| C1-3 server_singleton_test | PASS | 5/5 通过，EXITCODE=0 |
| C1-4 test_config_editor | PASS | 0 failures（10 用例），EXITCODE=0 |
| C1-5 test_gui_store | PASS | 0 failures（50 用例），EXITCODE=0 |
| C1-6 test_extractors | PASS | 45/45 通过（含 converting_phase_restored_after_extract），EXITCODE=0 |
| C1-7 verify_export_import | PASS | 39 通过 / 0 失败，EXITCODE=0（约100s；真库一致性自愈 2693→1917 属预期） |
| C2 复现误报用例 | PASS | test_progress_text_grace_states @ audit:573、test_heartbeat_grace_running_then_expired_then_dead @ gui_store:885，均 PASS |
| C3 CUDA 边界六条 | PASS | switchback_entry_structure:660 / fallback_tail_marker_order:672 / get_model_branches_structure:687 / cooldown_sequence_clamp_and_max_merge:703 / instruments_zero_write_without_running_task:738 / waiting_lock_sequence_with_grace:767，全部 PASS |

## 对抗性复核
1. 执行前脚本安全审查：无 git 写/破坏性命令；rmtree/unlink 全部作用于 %TEMP% 测试自建目录。
2. 假全绿检查：_run_all() globals() 自动收集，数量自洽。
3. skip/xfail 掺水：零跳过标记。
4. 空壳断言抽查：grace_states 四组边界断言、zero_write 哨兵+零写入断言均为实质测试。

**总结论：全部 PASS。**
