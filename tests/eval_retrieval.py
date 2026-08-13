"""eval_retrieval.py — 检索质量回归评估集（问题 18 沉淀）。

5 组查询 × 黄金文件（用户实测痛点 + 代表性场景），跑 hybrid_search 后
统计 top1/top3/top5 文件级命中。改索引/检索代码后一键回归：

    .venv\\Scripts\\python.exe tests\\eval_retrieval.py

依赖真实库与模型（首次加载数十秒）。命中判定：top 结果中任一来源行的
文件路径包含任一黄金文件名片段（gold 支持多个候选文件）。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import retriever  # noqa: E402
from retriever import hybrid_search

QUERIES = [
    ("fluent配置", ["20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md"]),
    ("个人能力", ["01-Meta/user-profile/B3_CFD能力评估.md",
                  "01-Meta/user-profile/B4_CAD与编程能力.md",
                  "01-Meta/user-profile/C3_优势风险盲点.md",
                  "01-Meta/user-profile/D2_成长路径建议.md"]),
    ("y+ 控制", ["20-Projects/Summer-2026/ROCKETRY/概念/y+控制与壁面处理策略.md"]),
    ("网格无关性", ["10-Areas/Aerospace/概念/网格无关性验证方法.md",
                   "20-Projects/Summer-2026/ROCKETRY/报告/report_checklist.md"]),
    ("OfficeCLI", ["20-Projects/Summer-2026/AI Dev Workflow/附录/OfficeCLI-SKILL.md"]),
    # 泛化查询（2026-08-13 新增）：词面与目标笔记标题无重叠，检验 dense 语义
    # 与 RRF 对"语义相近词面不同"查询的召回能力。
    ("免费在线认证课程", ["20-Projects/Summer-2026/OTHER CERT/论点/免费入门证书清单.md",
                      "20-Projects/Summer-2026/OTHER CERT/论点/AI证书与学习平台清单.md"]),
    ("CFD 近壁面网格 湍流 怎么处理", ["20-Projects/Summer-2026/ROCKETRY/概念/y+控制与壁面处理策略.md",
                                    "20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md"]),
    ("火箭设计怎么学", ["01-Meta/user-profile/B2_Rocketry.md",
                      "20-Projects/Summer-2026/ROCKETRY/目录.md"]),
]


def run(top_k=5):
    rows = []
    for q, gold in QUERIES:
        text = hybrid_search(q, top_k=top_k, include_body=False,
                             libraries="Obsidian Vault")
        files = [m.group(1) for line in text.splitlines()
                 if (m := re.match(r"\[来源\] [^/]+/(.+?) \(##", line))]
        pos = next((i + 1 for i, f in enumerate(files)
                    if any(g in f for g in gold)), None)
        rows.append((q, pos))
        print("%-8s pos=%s top1=%s" % (q, pos, files[0].split("/")[-1] if files else "-"))
    t1 = sum(1 for _, p in rows if p == 1)
    t3 = sum(1 for _, p in rows if p and p <= 3)
    t5 = sum(1 for _, p in rows if p and p <= 5)
    print("\n命中: top1=%d/%d top3=%d/%d top5=%d/%d" % (t1, len(rows), t3, len(rows), t5, len(rows)))
    # 重排降级检测：retriever.rerank_failures > 0 说明重排路径静默失败（如解包 bug），
    # 本评估跑的是降级排序，结果不可信——显式警告（不再静默）。
    if retriever.rerank_failures:
        print(f"⚠ 重排器执行失败 {retriever.rerank_failures} 次（本次为降级排序结果，请修复重排路径）")
    return t1, t3, t5


if __name__ == "__main__":
    # 期望基线（2026-08-11 标题链 + 顺序修复 + 重排器后）：top1=3/5 top3=5/5 top5=5/5
    run()
