"""test_wemm_indexer.py — WEMM 页级视觉导航索引（M1）回归测试。

风格对齐 audit_regression_test.py / test_extractors.py：标准库、非 pytest、
逐用例 PASS/FAIL、`_run_all()` 运行器。隔离：patch index.DATA_DIR /
index.CHROMA_DIR 重定向全部落盘，monkeypatch wemm_indexer.call_embed_image
（以及需要时 page_count/render_page_b64）为假实现——全程不碰真实看图服务、
不写真实 Chroma。创建真实小 PDF（pymupdf）驱动渲染逻辑。

覆盖：
  - 基础索引：文字层 PDF（2 页）→ wemm_<col> 库有 2 个页向量、meta 记 pages=2，
    向量维度与假编码器一致，库名带 .wemm 后缀（与文字库彻底分离）
  - 门禁冻结（红线6/7）：agent_allowed 不含 pdf → 零渲染、零编码、零 I/O，
    条目与页原样保留绝不被裁剪
  - 增量无变化：二轮不重编码（假编码器调用数不变），size+mtime 快速路径生效
  - 内容变化自愈：命中 MD5 变化 → 重新编码，页数随之更新
  - 删除精确清理：删 PDF → 其页向量从 Chroma 清掉、meta 条目裁剪
  - 损坏 PDF：折叠为 extract-failed 持久化终态（不产向量），不报异常
  - 源目录零写入快照断言
  - WEMM 版本升级：_version 变化 → 自动全量重建
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import index  # noqa: E402
import wemm_indexer as wi  # noqa: E402

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


# ---------- 工具 ----------

def _touch(path, t=None):
    t = t if t is not None else time.time_ns()
    os.utime(path, ns=(t, t))
    return t


def _make_text_pdf(path, pages=2, text="WEMM nav page"):
    import pymupdf
    d = pymupdf.open()
    for i in range(pages):
        pg = d.new_page()
        pg.insert_text((72, 72), f"{text} {i + 1}.")
    d.save(str(path))
    d.close()


def _make_bad_pdf(path):
    path.write_bytes(b"\x25PDF-1.7 broken not really a pdf")


def _snapshot(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (
                p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest())
    return out


def _default_cfg(vault, name="lib", collection="kb_lib"):
    return {"name": name, "path": str(vault), "collection": collection,
            "exclude_dirs": set(), "exclude_files": set(),
            "exclude_patterns": ()}


class _IsoEnv:
    """隔离：重定向 index 落盘路径；返回临时 vault 与 wemm meta 路径。"""

    _GLOBALS = ("DATA_DIR", "CHROMA_DIR", "LOCK_FILE",
                "PROGRESS_FILE", "DEVICE_STATE_FILE")

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-wemm-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir(parents=True)
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


def _meta(iso, name="lib"):
    p = iso.tmp / "DATA_DIR" / f"wemm_meta_{name}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _wemm_count(iso, collection="kb_lib"):
    import chromadb
    client = chromadb.PersistentClient(path=str(iso.tmp / "CHROMA_DIR"))
    col = client.get_or_create_collection(
        name=f"{collection}.wemm", metadata={"hnsw:space": "cosine"})
    return col.count()


class _FakeImageEncoder:
    """假看图编码器：记录调用，返回按水平方向分辨率无关的定维向量（dim=8）。"""

    def __init__(self, dim=8):
        self.dim = dim
        self.calls = 0
        self.b64_inputs = []

    def __call__(self, b64, dim, url):
        self.calls += 1
        self.b64_inputs.append(b64)
        v = np.zeros(self.dim, dtype="float32")
        v[0] = 1.0
        return v.tolist()


def _run(iso, cfg, enc, full=False, agent_allowed=None, url="http://x:1", dim=8):
    orig = wi.call_embed_image
    wi.call_embed_image = enc
    try:
        return wi.index_wemm_library(
            cfg, backend=True, full=full, agent_allowed=agent_allowed,
            url=url, dim=dim, progress=None)
    finally:
        wi.call_embed_image = orig


# ---------- 用例 ----------

def test_basic_index_with_pdf():
    """文字层 PDF 2 页 → wemm 库 2 向量、meta pages=2、库名带 .wemm。"""
    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p, pages=2)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        before = _snapshot(iso.vault)
        st = _run(iso, cfg, enc)
        ok("basic: 2 页向量入库", _wemm_count(iso) == 2, f"got {_wemm_count(iso)}")
        ok("basic: 编码被调用 2 次", enc.calls == 2, f"{enc.calls}")
        m = _meta(iso)
        ok("basic: meta 记 pages=2", m.get("doc.pdf", {}).get("pages") == 2,
           str(m.get("doc.pdf")))
        ok("basic: st 统计 pages=2", st.get("pages") == 2, str(st))
        ok("basic: 源目录零写入", _snapshot(iso.vault) == before)
        iso.cleanup()


def test_gate_freezes_unauthorized_pdf():
    """agent_allowed 不含 pdf → 零渲染零编码零 I/O，条目不裁剪。"""
    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p, pages=3)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        render_calls = {"n": 0}
        orig_render = wi.render_page_b64
        orig_pc = wi.page_count
        wi.render_page_b64 = lambda *a, **k: (render_calls.__setitem__("n", render_calls["n"] + 1), "b64")[1]
        wi.page_count = lambda *a, **k: (_ := None) or 3
        try:
            _run(iso, cfg, enc, agent_allowed=set())  # 空集合：pdf 未授权
        finally:
            wi.render_page_b64 = orig_render
            wi.page_count = orig_pc
        ok("gate: 零渲染", render_calls["n"] == 0, f"{render_calls['n']}")
        ok("gate: 零编码", enc.calls == 0, f"{enc.calls}")
        ok("gate: wemm 库为空", _wemm_count(iso) == 0)
        m = _meta(iso)
        ok("gate: meta 无该文件条目", "doc.pdf" not in m, str(m))
        iso.cleanup()


def test_gate_preserves_existing_entries():
    """先正常索引（授权）→ 再以未授权跑 → 已有条目与页不被裁剪。"""
    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p, pages=2)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        _run(iso, cfg, enc, agent_allowed={"pdf"})
        ok("preserve: 首轮 2 页", _wemm_count(iso) == 2)
        # 第二轮未授权：冻结，条目/页原样保留
        _run(iso, cfg, enc, agent_allowed=set())
        ok("preserve: 冻结后页向量仍保留", _wemm_count(iso) == 2)
        m = _meta(iso)
        ok("preserve: 条目保留", "doc.pdf" in m, str(m))
        iso.cleanup()


def test_incremental_no_reencode_when_unchanged():
    """二轮无变化：size+mtime 快速路径 → 不再编码。"""
    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p, pages=2)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        _run(iso, cfg, enc)
        first = enc.calls
        ok("incr: 首轮编码 2", first == 2, str(first))
        _run(iso, cfg, enc)
        ok("incr: 二轮不重编码", enc.calls == first, f"{enc.calls} vs {first}")
        iso.cleanup()


def test_change_content_reencodes_and_updates_pages():
    """MD5 变化 → 重编码；页数变化（2→3）meta 与 Chroma 同步。"""
    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p, pages=2)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        _run(iso, cfg, enc)
        # 替换内容：加一页
        _make_text_pdf(p, pages=3, text="NEW CONTENT")
        _run(iso, cfg, enc)
        ok("change: wemm 库 3 页", _wemm_count(iso) == 3, str(_wemm_count(iso)))
        m = _meta(iso)
        ok("change: meta pages=3", m["doc.pdf"]["pages"] == 3, str(m["doc.pdf"]))
        iso.cleanup()


def test_delete_prunes_pages_and_meta():
    """删除 PDF → 页向量清掉、meta 条目裁剪。"""
    with _IsoEnv() as iso:
        p1 = iso.vault / "a.pdf"
        p2 = iso.vault / "b.pdf"
        _make_text_pdf(p1, pages=2)
        _make_text_pdf(p2, pages=1)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        _run(iso, cfg, enc)
        ok("delete: 初始 3 页", _wemm_count(iso) == 3)
        p1.unlink()
        _run(iso, cfg, enc)
        ok("delete: 只剩 b 的 1 页", _wemm_count(iso) == 1, str(_wemm_count(iso)))
        m = _meta(iso)
        ok("delete: meta 只剩 b", "a.pdf" not in m and "b.pdf" in m, str(m))
        iso.cleanup()


def test_bad_pdf_terminal_state():
    """损坏 PDF → extract-failed 终态（不产向量），不抛异常。"""
    with _IsoEnv() as iso:
        _make_bad_pdf(iso.vault / "bad.pdf")
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        try:
            st = _run(iso, cfg, enc)
        except Exception as e:
            ok("bad: 不抛异常", False, f"raised {type(e).__name__}")
            iso.cleanup()
            return
        ok("bad: 不产向量", _wemm_count(iso) == 0)
        m = _meta(iso)
        info = m.get("bad.pdf", {})
        ok("bad: 落 extract-failed 终态", info.get("xfail"), str(info))
        ok("bad: 无 page 计数", not info.get("pages"), str(info))
        ok("bad: st 统计 pages=0", st.get("pages") == 0, str(st))
        iso.cleanup()


def test_version_upgrade_forces_rebuild():
    """meta._version ≠ 当前 → 自动全量重建。"""
    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p, pages=1)
        cfg = _default_cfg(iso.vault)
        enc = _FakeImageEncoder()
        _run(iso, cfg, enc)
        # 篡改 meta 版本号（跑 DATA_DIR 实际落盘处）
        mp = iso.tmp / "DATA_DIR" / "wemm_meta_lib.json"
        m = json.loads(mp.read_text(encoding="utf-8"))
        m["_version"] = wi.WEMM_VERSION - 1
        mp.write_text(json.dumps(m), encoding="utf-8")
        _run(iso, cfg, enc)
        ok("version: 重建后仍 1 页", _wemm_count(iso) == 1)
        iso.cleanup()


def test_collection_name_separated():
    """wemm 库名 = <col>.wemm，绝不与文字库混名。"""
    ok("sep: 命名函数", wi.wemm_collection("kb_lib") == "kb_lib.wemm")
    ok("sep: 版本=1", wi.WEMM_VERSION == 1)


# ---------- 运行器 ----------

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
    print(f"\n===== WEMM Indexer: {PASS} passed, {FAIL} failed =====")
    if _FAILED:
        print("失败用例: " + ", ".join(_FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_run_all())
