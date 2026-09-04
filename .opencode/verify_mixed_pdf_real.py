# -*- coding: utf-8 -*-
"""隔离验证问题34 整本云端路由对两份真实混合型课件 PDF 的产出完整性。

按架构红线 7：用 set_cache_dir 把缓存隔离到临时目录，只在内存改 config.CFG，
绝不写回 config.json、绝不触发正式索引、绝不污染生产 data/extract_cache。
本脚本会真实调用 MinerU 云端（联网 + 消耗配额）。
"""
import os
import sys
import tempfile
import traceback

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import extractors as e

FILES = {
    "Manometer": r"D:\.material\.WORKSTATION\USM COURSE RELATED\Y1S2\ESA122 fluid\LECTURE NOTE\ManometerEquation.pdf",
    "Note9": r"D:\.material\.WORKSTATION\USM COURSE RELATED\Y1S2\EMM102 static\LECTURE NOTE\Note9 equilibrium force distributed.pdf",
}

# 期望出现的关键片段（跨页，用于判断整本云端 MD 是否完整）
EXPECT = {
    "Manometer": [
        # 第3/5/7 页纯图片页的核心内容（宽度/水银柱换算相关）
        "29.28",   # pA 计算结果（图片页）
        "kPa", 
        "900",     # 密度相关换算
    ],
    "Note9": [
        "4.20",    # EXAMPLE 4.20（第9页例题）
        "4.21",
        "4.22",
        "1.5",     # x̄ 积分推导结果
    ],
}


def main():
    # 隔离缓存
    tmp = tempfile.mkdtemp(prefix="verify-mixed-")
    e.set_cache_dir(tmp)
    print("隔离缓存目录:", tmp)

    # 内存覆盖扫描后端为云端（不写回 config.json）
    orig_scan = config.CFG.get("pdf_scan_backend")
    orig_text = config.CFG.get("pdf_text_backend")
    config.CFG["pdf_scan_backend"] = "mineru-cloud"
    print("pdf_scan_backend(内存覆盖):", config.CFG["pdf_scan_backend"])
    print("pdf_text_backend:", orig_text)
    print("model_version:", config.CFG.get("mineru_model_version"))

    ok_all = True
    for name, path in FILES.items():
        print("\n" + "=" * 70)
        print(f"[{name}] {path}")
        if not os.path.exists(path):
            print("  !! 文件不存在，跳过")
            ok_all = False
            continue
        try:
            md, reason, route, cached = e._extract_full(path)
        except Exception as ex:
            print("  异常外泄（不该发生）:", repr(ex))
            traceback.print_exc()
            ok_all = False
            continue
        print(f"  reason={reason!r} route={route!r} cached={cached!r} len_md={len(md) if md else 0}")
        if not md:
            print("  !!! 无产出 —— 混合型在云端开启时未得到 MD")
            ok_all = False
            continue
        # 检查期望片段
        for frag in EXPECT[name]:
            hit = (frag.lower() in md.lower())
            print(f"    含 {frag!r}: {hit}")
            if not hit:
                ok_all = False
        # 打印前 60 行预览
        print("  --- MD 前 60 行预览 ---")
        for i, line in enumerate(md.splitlines()[:60]):
            print(f"    {i}: {line}")

    # 确认生产缓存未被污染
    print("\n" + "=" * 70)
    prod_cache = e.DEFAULT_CACHE_DIR
    print("生产缓存目录内容（应为本次未触达的原有状态）:")
    if os.path.isdir(prod_cache):
        for name in sorted(os.listdir(prod_cache)):
            print("  ", name)
    else:
        print("   (不存在)")

    print("\n隔离缓存目录留下的条目:")
    for name in sorted(os.listdir(tmp)):
        print("  ", name)

    config.CFG["pdf_scan_backend"] = orig_scan
    print("\n结论:", "ALL OK" if ok_all else "有缺失/异常，见上")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
