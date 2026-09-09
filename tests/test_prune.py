"""问题49：索引完成后全局回收（prune_unreferenced_data）回归用例。

全部用临时隔离目录注入（data_dir/cache_dir/chroma_dir/entries），绝不碰真数据。
标准库 + 逐用例 PASS/FAIL + _run_all() 运行器，风格对齐 audit_regression_test.py。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index  # noqa: E402

QUIET = lambda *a, **k: None  # noqa: E731 测试用静默 log

# 必须是合法 hex（0-9a-f）：md5 hexdigest 不会出现 g/h/z
A = "a" * 32
B = "b" * 32
C = "c" * 32
D = "d" * 32


def _write_meta(data_dir, name, hashes):
    m = {f"{n}.md": {"hash": h, "chunks": 1} for n, h in enumerate(hashes)}
    (data_dir / f"index_meta_{name}.json").write_text(
        json.dumps(m), encoding="utf-8")


def _entry(name, collection, vault):
    return {"name": name, "path": str(vault), "collection": collection}


def test_prune_orphan_caches_and_unregistered_meta():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_dir = td / "data"
        cache_dir = td / "cache"
        data_dir.mkdir()
        cache_dir.mkdir()
        # 注册库 t 引用 A；B 是纯孤儿；C 属于"已从注册表移除的库 zzz"
        _write_meta(data_dir, "t", [A])
        _write_meta(data_dir, "zzz", [C])
        (data_dir / "wemm_meta_zzz.json").write_text("{}", encoding="utf-8")
        files = {
            f"{A}.v4.md": "alive",
            f"{B}.v4.md": "orphan md",
            f"{B}.v4.mineru.json": "[]",
            f"{C}.v4.md": "from removed lib",
            "mineru_pending.json": "{}",  # 簿记不删
        }
        for n, c in files.items():
            (cache_dir / n).write_text(c, encoding="utf-8")
        # base index_meta.json（旧单库入口默认名）不匹配 index_meta_*.json，保留
        (data_dir / "index_meta.json").write_text("{}", encoding="utf-8")

        n_cache, n_col, n_meta = index.prune_unreferenced_data(
            data_dir=data_dir, cache_dir=cache_dir,
            chroma_dir=td / "chroma" / "nonexistent",  # 不存在 → 跳过 collection 段
            entries=[_entry("t", "tc", td)],
            log=QUIET)
        assert n_meta == 2, n_meta          # index_meta_zzz + wemm_meta_zzz
        assert n_cache == 3, n_cache        # B.md + B.sidecar + C.md
        assert n_col == 0, n_col
        left = sorted(p.name for p in cache_dir.iterdir())
        assert left == [f"{A}.v4.md", "mineru_pending.json"], left
        assert (data_dir / "index_meta_t.json").exists(), "注册库指纹绝不能被删"
        assert (data_dir / "index_meta.json").exists(), "base 默认指纹保留"
        assert not (data_dir / "index_meta_zzz.json").exists()
        assert not (data_dir / "wemm_meta_zzz.json").exists()


def test_prune_keeps_live_cache_and_leaves_bookkeeping_subdir():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_dir = td / "data"
        cache_dir = td / "cache"
        data_dir.mkdir()
        cache_dir.mkdir()
        _write_meta(data_dir, "t", [A])
        sub = cache_dir / "_orphan_v1_bak_2026-09-08"  # 子目录不递归、不删
        sub.mkdir()
        (sub / "deadbeef.v1.md").write_text("keep me", encoding="utf-8")
        (cache_dir / f"{A}.v4.md").write_text("live", encoding="utf-8")
        (cache_dir / f"{A}.v4.mineru.json").write_text("[]", encoding="utf-8")

        n_cache, _, _ = index.prune_unreferenced_data(
            data_dir=data_dir, cache_dir=cache_dir,
            chroma_dir=td / "chroma" / "nonexistent",
            entries=[_entry("t", "tc", td)],
            log=QUIET)
        assert n_cache == 0, n_cache
        assert (cache_dir / f"{A}.v4.md").exists()
        assert (cache_dir / f"{A}.v4.mineru.json").exists()
        assert (sub / "deadbeef.v1.md").exists(), "备份子目录里的文件不得动"


def test_prune_deletes_removed_library_collections_keeps_active():
    # Windows：chromadb 持 sqlite 句柄，进程结束前临时目录删不掉 → 忽略清理错误
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        data_dir = td / "data"
        chroma_dir = td / "chroma"
        data_dir.mkdir()
        _write_meta(data_dir, "t", [A])
        import chromadb
        client = chromadb.PersistentClient(path=str(chroma_dir))
        for c in ("tcol", "tcol.wemm", "leftover"):
            client.create_collection(c)

        _, n_col, _ = index.prune_unreferenced_data(
            data_dir=data_dir, cache_dir=td / "cache" / "nonexistent",
            chroma_dir=chroma_dir,
            entries=[_entry("t", "tcol", td)],
            log=QUIET)
        assert n_col == 1, n_col
        remain = sorted(getattr(c, "name", c)
                        for c in client.list_collections())
        assert remain == ["tcol", "tcol.wemm"], remain


def test_prune_empty_registry_is_noop():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cache_dir = td / "cache"
        cache_dir.mkdir()
        (cache_dir / f"{B}.v4.md").write_text("x", encoding="utf-8")
        res = index.prune_unreferenced_data(
            data_dir=td / "data", cache_dir=cache_dir,
            chroma_dir=td / "chroma" / "nonexistent",
            entries=[], log=QUIET)
        assert res == (0, 0, 0), res
        assert (cache_dir / f"{B}.v4.md").exists(), "无注册库时绝不能删任何东西"


def test_prune_pending_task_fingerprint_protected():
    """在途 MinerU 断点簿记的 md5 即使没有 meta 引用，缓存也绝不删（续接要用）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_dir = td / "data"
        cache_dir = td / "cache"
        data_dir.mkdir()
        cache_dir.mkdir()
        _write_meta(data_dir, "t", [A])
        import extractors as ex
        try:
            ex.set_cache_dir(cache_dir)
            ex._pending_add("batch-1", "D:/v/s.pdf", "ocr:mineru-cloud", D)
            # 在途指纹 D 必须留；纯孤儿 B 仍应被删（证明保护不是"啥都不删"）
            (cache_dir / f"{D}.v4.md").write_text("in flight", encoding="utf-8")
            (cache_dir / f"{B}.v4.md").write_text("orphan", encoding="utf-8")

            n_cache, _, _ = index.prune_unreferenced_data(
                data_dir=data_dir, cache_dir=cache_dir,
                chroma_dir=td / "chroma" / "nonexistent",
                entries=[_entry("t", "tc", td)],
                log=QUIET)
            assert n_cache == 1, n_cache   # 只删 B，D 受保护
            assert (cache_dir / f"{D}.v4.md").exists(), \
                "在途任务指纹必须受保护，不能被全局回收误删"
            assert not (cache_dir / f"{B}.v4.md").exists()
        finally:
            ex.set_cache_dir(None)


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
