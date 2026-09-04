# 红队 round-diff-2 复审 — B1(_extract_pdf异常穿透)/B2(试验台缓存污染)

## B1 ⚠️ 已修复主路径，残留缝隙（已在本轮追加补丁修复）
原三类反例（密码未认证PDF、页面损坏、page_count异常）均已被380行外层except兜住。但finally: doc.close()本身不在except保护范围内——若doc.close()自身抛异常会绕过防护裸抛出去。**追加补丁已修复**：finally里包了try/except Exception: pass，新增测试test_pdf_close_exception_does_not_leak_folds_to_extract_failed验证，29/29通过。

## B2 ✅ 已修复，找不到反例
_preview_job运行在独立子进程，_cache_dir模块全局变量父子进程物理隔离；所有缓存读写统一走get_cache_dir()，_mineru_cloud_extract无独立落盘逻辑；finally还原逻辑正确。新测试test_preview_job_uses_isolated_cache直接断言生产缓存目录零新增文件，覆盖了比子进程测试更严格的同进程直调场景。

理论风险记录（非blocker）：_cache_dir是进程内共享全局变量，若未来有代码在同进程内用线程并发调用_preview_job会有互相污染风险，但当前代码库无此路径。

## 总体结论
B2完全修复；B1原缝隙已通过追加补丁closed。
