"""run.py — 统一回归入口（标准库，单进程，零新依赖）。

用法（仓库根目录）：
  python tests/run.py                  # 全部跑完，A→B→C 顺序
  python tests/run.py --suite test_dedup  # 只跑某套（调试用，默认全量）
  python tests/run.py --list            # 列出分组与顺序，不执行

设计：
- 单进程顺序执行：只交一次重模块（torch/chromadb/index）导入税；
  Windows 上 Chroma sqlite 句柄锁 + 显存争用，默认不做多进程并行。
- 每套前后快照/还原共享全局（library 注册表路径、config 路径、
  index 落盘路径、gpu_arbiter 拉起函数），防套间污染——各文件原有的
  make_isolated 类写法只管进不管出，这里统一兜底。
- 各文件原有 _run_all / main / test_* 循环原样调用，一个断言都不删；
  计时精确到套，>5s 的套件单独点名。
"""
import argparse
import copy
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# A 快检：纯逻辑；B 中速：小文件+临时库；C 慢速：真造文件+真索引。
SUITES = [
    ("A", "test_config_editor"),
    ("A", "library_registry_test"),
    ("A", "server_singleton_test"),
    ("A", "audit_regression_test"),
    ("A", "test_mcp_scheduling"),
    ("A", "test_selection"),
    ("A", "test_library_summary"),
    ("B", "test_gui_store"),
    ("B", "test_guiweb"),
    ("B", "test_dedup"),
    ("B", "test_prune"),
    ("B", "test_gpu_arbiter"),
    ("B", "test_wemm_retriever"),
    ("C", "test_extractors"),
    ("C", "test_wemm_indexer"),
    ("C", "verify_export_import"),
]

_GENERIC = {"test_gui_store", "test_config_editor"}  # 无 _run_all，走 test_* 循环


def _snapshot():
    snap = {}
    try:
        import library as _lib
        snap["library"] = {
            "LIBRARIES_FILE": _lib.LIBRARIES_FILE,
            "DATA_DIR": _lib.DATA_DIR,
            "CFG": copy.deepcopy(_lib.CFG),
        }
    except Exception:
        pass
    try:
        import config as _cfg
        snap["config"] = {"CONFIG_PATH": _cfg.CONFIG_PATH, "DATA_DIR": _cfg.DATA_DIR}
    except Exception:
        pass
    try:
        import index as _idx
        snap["index"] = {n: getattr(_idx, n) for n in
                         ("DATA_DIR", "CHROMA_DIR", "LOCK_FILE",
                          "PROGRESS_FILE", "DEVICE_STATE_FILE")}
        snap["index_encode"] = _idx.encode_safe
    except Exception:
        pass
    try:
        import gpu_arbiter as _ga
        snap["ensure_server"] = _ga.ensure_server
    except Exception:
        pass
    try:
        # retriever.CHROMA_DIR 是 import 期绑定值，随 index 补丁够不着，单列快照
        import retriever as _ret
        snap["retriever"] = (_ret.CHROMA_DIR, _ret._chroma_client)
    except Exception:
        pass
    return snap


def _restore(snap):
    try:
        import library as _lib
        if "library" in snap:
            _lib.LIBRARIES_FILE = snap["library"]["LIBRARIES_FILE"]
            _lib.DATA_DIR = snap["library"]["DATA_DIR"]
            _lib.CFG = snap["library"]["CFG"]
    except Exception:
        pass
    try:
        import config as _cfg
        if "config" in snap:
            _cfg.CONFIG_PATH = snap["config"]["CONFIG_PATH"]
            _cfg.DATA_DIR = snap["config"]["DATA_DIR"]
    except Exception:
        pass
    try:
        import index as _idx
        if "index" in snap:
            for n, v in snap["index"].items():
                setattr(_idx, n, v)
            _idx.encode_safe = snap["index_encode"]
    except Exception:
        pass
    try:
        import gpu_arbiter as _ga
        if "ensure_server" in snap:
            _ga.ensure_server = snap["ensure_server"]
    except Exception:
        pass
    try:
        import retriever as _ret
        if "retriever" in snap:
            _ret.CHROMA_DIR, _ret._chroma_client = snap["retriever"]
    except Exception:
        pass


def _run_generic(mod):
    fns = [(k, v) for k, v in sorted(vars(mod).items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    return failed, len(fns)


def _run_suite(name):
    snap = _snapshot()
    t0 = time.time()
    try:
        mod = __import__(name)
        if name == "verify_export_import":
            try:
                mod.main()
            except SystemExit as e:
                return ("FAIL" if e.code else "PASS"), time.time() - t0, "main()"
            return "PASS", time.time() - t0, "main()"
        if name in _GENERIC:
            failed, total = _run_generic(mod)
            return ("FAIL" if failed else "PASS"), time.time() - t0, \
                f"{total - failed}/{total} 通过"
        rc = mod._run_all()
        ok = (rc in (0, True, None))
        return ("PASS" if ok else "FAIL"), time.time() - t0, f"rc={rc}"
    except SystemExit as e:
        return ("FAIL" if e.code else "PASS"), time.time() - t0, f"exit={e.code}"
    except Exception:
        traceback.print_exc()
        return "FAIL", time.time() - t0, "异常"
    finally:
        _restore(snap)


def main(argv=None):
    ap = argparse.ArgumentParser(description="统一回归入口（全量，A→B→C）")
    ap.add_argument("--suite", default="", help="只跑某套（调试用）")
    ap.add_argument("--list", action="store_true", help="只列出分组与顺序")
    args = ap.parse_args(argv)
    if args.list:
        for grp, name in SUITES:
            print(f"{grp}  {name}")
        return 0
    suites = [(g, n) for g, n in SUITES if not args.suite or n == args.suite]
    if args.suite and not suites:
        print(f"未知套件：{args.suite}")
        return 2
    print(f"统一回归：{len(suites)} 套，单进程 A→B→C")
    results = []
    for grp, name in suites:
        print(f"\n=== [{grp}] {name} ===")
        status, secs, note = _run_suite(name)
        print(f"--- {name}: {status}（{secs:.1f}s，{note}）")
        results.append((grp, name, status, secs))
    npass = sum(1 for r in results if r[2] == "PASS")
    total_t = sum(r[3] for r in results)
    print(f"\n结果：{npass}/{len(results)} 套通过，总耗时 {total_t:.1f}s")
    slow = sorted(results, key=lambda r: -r[3])[:10]
    print("最慢 Top（套级）：")
    for grp, name, status, secs in slow:
        flag = "  ← 超 5s" if secs > 5 else ""
        print(f"  {secs:7.1f}s  [{grp}] {name} {status}{flag}")
    bad = [n for _, n, s, _ in results if s != "PASS"]
    if bad:
        print("失败套件：" + ", ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
