"""test_wemm_retriever.py — WEMM 页级视觉导航检索（M1）回归测试。

风格对齐：标准库、非 pytest、逐用例 PASS/FAIL、`_run_all()`。
隔离：patch index.CHROMA_DIR 重定向落盘；monkeypatch wemm_retriever 的
load_registry/effective_config/call_embed_text/health——不碰真实注册表与看图服务。
页库直接以受控向量（one-hot）写入 Chroma 以验证排序/过滤/归一化。

覆盖：
  - 后端 off → 空结果 + 提示
  - 看图服务不可用 → 空结果 + 提示（health 抛异常）
  - 命中：返回 库/相对路径/绝对路径/页码/分数，按余弦降序，页码从 0 基输出
  - libraries 过滤：只查指定库，排除其余库
  - 空页库 → 不报错，返回空
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import index  # noqa: E402
import wemm_indexer as wi  # noqa: E402
import wemm_retriever as wr  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r is not None:
        _r(encoding="utf-8", errors="replace")


PASS = 0
FAIL = 0
_FAILED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        _FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def _onehot(idx, dim=6):
    v = np.zeros(dim, dtype="float32")
    v[idx % dim] = 1.0
    return v.tolist()


class _IsoEnv:
    _GLOBALS = ("CHROMA_DIR", "DATA_DIR")

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-wemm-ret-"))
        self._saved = {}

    def __enter__(self):
        for n in self._GLOBALS:
            self._saved[n] = getattr(index, n)
            setattr(index, n, self.tmp / n)
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            setattr(index, n, v)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _seed_collection(iso, collection, rows):
    """rows: [(file, page, abs_path, library, vec)]"""
    import chromadb
    client = chromadb.PersistentClient(path=str(self_tmp(iso)))
    col = client.get_or_create_collection(
        name=f"{collection}.wemm", metadata={"hnsw:space": "cosine"})
    if rows:
        col.add(
            ids=[f"{r[0]}::{r[1]}" for r in rows],
            embeddings=[r[4] for r in rows],
            metadatas=[{"file": r[0], "page": r[1], "abs_path": r[2],
                        "library": r[3]} for r in rows],
        )
    return client


def _fake_ok_health():
    """把 health 替换为假 OK（避免真实网络调用），原函数在 with 块外还原。"""
    import contextlib
    @contextlib.contextmanager
    def _cm():
        orig = wr.health
        wr.health = lambda url: {"ok": True, "loaded": True}
        try:
            yield
        finally:
            wr.health = orig
    return _cm()


def self_tmp(iso):
    return iso.tmp / "CHROMA_DIR"


_FCFG = {
    "libA": {"name": "libA", "collection": "kb_a", "path": "C:/vaultA"},
    "libB": {"name": "libB", "collection": "kb_b", "path": "C:/vaultB"},
}


def _patch_targets(entries, cfgs):
    wr.load_registry = lambda: entries
    wr.effective_config = lambda e: cfgs[e["name"]]


def test_backend_off_returns_empty():
    res, err = wr.wemm_search("q", backend="off")
    ok("off: 空结果", res == [])
    ok("off: 有提示", bool(err) and "后端未开启" in err, str(err))


def test_service_down_returns_empty():
    with _IsoEnv() as iso:
        _seed_collection(iso, "kb_a", [
            ("a.pdf", 0, "C:/vaultA/a.pdf", "libA", _onehot(0)),
        ])
        _patch_targets([{"name": "libA"}], _FCFG)
        orig_health = wr.health
        wr.health = lambda url: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            res, err = wr.wemm_search("q", backend="on", url="http://127.0.0.1:1", dim=6)
        finally:
            wr.health = orig_health
        ok("down: 空结果", res == [])
        ok("down: 提示服务不可用", bool(err) and "不可用" in err, str(err))
        iso.cleanup()


def test_hit_sorted_with_metadata():
    with _IsoEnv() as iso:
        # libA: a.pdf 第1页向量离查询(onehot0)最近
        rows = [
            ("a.pdf", 0, "C:/vaultA/a.pdf", "libA", _onehot(0)),
            ("a.pdf", 1, "C:/vaultA/a.pdf", "libA", _onehot(2)),
            ("b.pdf", 0, "C:/vaultA/b.pdf", "libA", _onehot(4)),
        ]
        _seed_collection(iso, "kb_a", rows)
        _patch_targets([{"name": "libA"}], _FCFG)
        orig_te = wr.call_embed_text
        wr.call_embed_text = lambda text, dim, url: _onehot(0, dim)
        try:
            with _fake_ok_health():
                res, err = wr.wemm_search("vapor pressure", backend="on",
                                          url="http://127.0.0.1:1", dim=6, top_k=5)
        finally:
            wr.call_embed_text = orig_te
        ok("hit: 无误", err is None, str(err))
        ok("hit: 3 条", len(res) == 3, str(res))
        ok("hit: 首条 a.pdf 第0页", res[0][1] == "a.pdf" and res[0][3] == 0, str(res[0]))
        ok("hit: 按分降序", res[0][4] >= res[1][4] >= res[2][4],
           f"{[r[4] for r in res]}")
        ok("hit: 绝对路径透出", res[0][2] == "C:/vaultA/a.pdf", str(res[0][2]))
        ok("hit: 库名正确", res[0][0] == "libA", str(res[0][0]))
        iso.cleanup()


def test_libraries_filter():
    with _IsoEnv() as iso:
        _seed_collection(iso, "kb_a", [
            ("a.pdf", 0, "C:/vaultA/a.pdf", "libA", _onehot(0)),
        ])
        _seed_collection(iso, "kb_b", [
            ("c.pdf", 0, "C:/vaultB/c.pdf", "libB", _onehot(0)),
        ])
        _patch_targets([{"name": "libA"}, {"name": "libB"}], _FCFG)
        orig_te = wr.call_embed_text
        wr.call_embed_text = lambda text, dim, url: _onehot(0, dim)
        try:
            with _fake_ok_health():
                res, _ = wr.wemm_search("q", backend="on", libraries=["libA"],
                                        url="http://127.0.0.1:1", dim=6, top_k=5)
        finally:
            wr.call_embed_text = orig_te
        ok("filter: 只含 libA", all(r[0] == "libA" for r in res), str(res))
        ok("filter: 1 条", len(res) == 1, str(res))
        iso.cleanup()


def test_empty_collection_returns_empty():
    with _IsoEnv() as iso:
        _seed_collection(iso, "kb_a", [])  # 空页库
        _patch_targets([{"name": "libA"}], _FCFG)
        orig_te = wr.call_embed_text
        wr.call_embed_text = lambda text, dim, url: _onehot(0, dim)
        try:
            with _fake_ok_health():
                res, err = wr.wemm_search("q", backend="on",
                                          url="http://127.0.0.1:1", dim=6, top_k=5)
        finally:
            wr.call_embed_text = orig_te
        ok("empty: 空结果无误", res == [] and err is None, f"{res} {err}")
        iso.cleanup()


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            _FAILED.append(t.__name__)
            import traceback
            print(f"  FAIL  {t.__name__}  异常: {e}")
            traceback.print_exc()
    print(f"\n===== WEMM Retriever: {PASS} passed, {FAIL} failed =====")
    if _FAILED:
        print("失败用例: " + ", ".join(_FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_run_all())
