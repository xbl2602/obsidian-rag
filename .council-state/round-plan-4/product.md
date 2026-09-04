# Product 复审（方案门 第2轮）

## 总体判定：PASS（可进实现）

## 原 blocker 复核
- B1 升级过渡期 → RESOLVED：白名单无条件、不依赖字段（store.py:146-147 / index.py:405-409 实读确认），双向矩阵闭合，配套用例成对。
- B2 MinerU 600s → RESOLVED：覆盖链逐环核实（入口唯一且紧邻 :1431/:1433；轮询期无 update_progress；G7 还原点物理上不可能早于 OCR 结束）。豁免窗口严格等于转换真实耗时，"MinerU 不加埋点"成立。解法比第 1 轮设想的独立宽限更干净。

## 建议复核
S2/S3/S4/S5 → RESOLVED；S1 → 部分解决（量化信息进了 MCP 侧，GUI note 仍定性文案，并入 N1）。

## 专项核查结论
1. entering converting 的 update_progress 不带 grace kwarg 会顺手 pop 残留——白名单与宽限路径零耦合 ✔
2. waiting-lock 可见性：stepper 全灰不崩、KPI 裸显英文内部值、message="等待写库" 在 GUI 无消费方只进 MCP 文本——可用但不理想（见 N1）
3. 三态措辞配合色态可区分；真卡死在宽限内与合法安静期同貌是已接受 trade-off

## 新问题（建议级）
- N1 (a) 建议在 widgets PHASE_TEXT/PHASE_COLOR 加 "waiting-lock": "等锁" 一行（用户困惑成本 > 零改动收益）；(b) 宽限 note 附安静秒数。
- 回应待确认②：waiting-lock 值建议保留不要退化——"排队等别人释放锁"和"正在写库"处置含义不同。
