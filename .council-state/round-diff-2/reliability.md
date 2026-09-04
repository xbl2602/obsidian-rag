# 可靠工 round-diff-2 复审 — B1(_extract_pdf异常穿透)/B2(试验台竞态)

## B1 ✅ 已修复，无新问题
外层 except（extractors.py:380-385）完整覆盖 doc.page_count/逐页扫描/OCR路由调用；finally: doc.close() 在所有路径下执行；内层 pymupdf4llm except 不受影响，两层不重叠。新测试 test_pdf_page_scan_exception_folds_to_extract_failed 真实构造"能打开、扫描时炸"场景，与"打不开"场景明确区分。28/28 通过。

## B2 ✅ 已修复，无新问题
run_id/proc/stop_event 确实作为参数传给 _poll，函数体内零处读 self.*。顺序：清理自己捕获的proc → 判run_id不匹配 → 判outcome==cancelled → 才碰共享UI，与描述一致。时序推演确认T1不可能误杀proc2。self._proc 属性变成死代码但无害（只写不读）。测试 test_extract_lab_poll_stale_run_only_cleans_up/test_extract_lab_poll_current_run_finishes 真实复现竞态条件。36/36通过。

边缘情况记录（非blocker）：_close 与结果入队同一轮询周期内触发时可能对已关闭对话框做无害UI空写，被_safe_update吞掉，不影响判定。

## 总体结论
两处修复均可通过，放行。
