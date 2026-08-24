"""test_extractors.py — 多格式文档提取（R1）回归测试。

风格对齐 audit_regression_test.py：标准库、非 pytest、逐用例 PASS/FAIL。
覆盖：
  - DOCX 提取回环（标题层级钳制 / 管道表转义 / 单元格换行 / 空文档）
  - 文字层 PDF 提取 / 扫描件判 scanned / 损坏文件折叠为 extract-failed
  - 大写扩展名路由（红队 B3：.PDF / .Docx 不被静默误判）
  - 缓存行为（命中不重提取 / 失败不写缓存 / 写失败不影响返回值 / 孤儿 tmp 清扫）
  - xfail 统一终态防死循环全序列（坏文件 → 终态稳定 → 修复后自动恢复；
    红队 B1 TBD 回归；empty/unreadable/scanned 各终态；哨兵两轮判稳）
  - 源目录零写入快照断言
  - 静态断言（kb_stale/_index_core 走 _load_text、META_VERSION=9、_skipped 收拢）
  - library.set_config 扩展名白名单校验

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_extractors.py
隔离：索引集成用例把 index 的全部落盘路径重定向到临时目录，并用假编码器
（numpy 全零向量）替代 embedding 模型——全程不加载模型、不碰真实 Chroma。
"""
import hashlib
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import extractors as ex  # noqa: E402
import index  # noqa: E402
import library  # noqa: E402

# 管道输出环境下强制 UTF-8（cp1252 控制台打中文会 UnicodeEncodeError）
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        _reconf(encoding="utf-8", errors="replace")


# ---------- 工具 ----------

def _touch(path, t=None):
    """显式设置 mtime（纳秒级确定性变化，不依赖文件系统时间精度）。"""
    t = t if t is not None else time.time_ns()
    os.utime(path, ns=(t, t))
    return t


class _FakeEncoder:
    """替代 encode_safe：记录调用次数，返回维度 8 的全零向量。"""

    def __init__(self):
        self.calls = []
        self.texts_seen = []

    def __call__(self, texts, batch_size=None):
        self.calls.append(list(texts))
        self.texts_seen.extend(texts)
        return np.zeros((len(texts), 8), dtype="float32")


class _IsoEnv:
    """索引集成用例的隔离环境：临时目录接管全部落盘 + 假编码器。

    用法：
        with _IsoEnv() as iso:
            index._index_core(iso.vault, "col_test", iso.meta_file, ...)
    """

    _GLOBALS = ("DATA_DIR", "CHROMA_DIR", "LOCK_FILE",
                "PROGRESS_FILE", "DEVICE_STATE_FILE")

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-extract-test-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir(parents=True)
        self.encoder = _FakeEncoder()
        self._saved = {}

    def __enter__(self):
        for name in self._GLOBALS:
            self._saved[name] = getattr(index, name)
            setattr(index, name, self.tmp / name)
        self.meta_file = self.tmp / "meta.json"
        self._saved_encode = index.encode_safe
        index.encode_safe = self.encoder
        return self

    def __exit__(self, *exc):
        index.encode_safe = self._saved_encode
        for name, val in self._saved.items():
            setattr(index, name, val)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def _run_index(iso, incremental=True, full=False):
    """对隔离库跑一轮索引（extensions 含多格式）。"""
    return index._index_core(
        str(iso.vault), "col_test", iso.meta_file,
        set(), set(), (), ["md", "pdf", "docx"],
        600, 200, library_label="t",
        incremental=incremental, full=full, tbd_ratio=0.1)


def _load_meta(iso):
    if iso.meta_file.exists():
        return json.loads(iso.meta_file.read_text(encoding="utf-8"))
    return {}


def _snapshot(root):
    """目录树快照：rel → (字节数, 内容md5, mtime_ns)，用于零写入断言。"""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            b = p.read_bytes()
            out[str(p.relative_to(root))] = (
                len(b), hashlib.md5(b).hexdigest(), p.stat().st_mtime_ns)
    return out


def _make_text_pdf(path, pages=2, text="Fluent mixing model basics."):
    import pymupdf
    d = pymupdf.open()
    for i in range(pages):
        pg = d.new_page()
        pg.insert_text((72, 72), f"{text} page {i + 1}.")
    d.save(str(path))
    d.close()


def _make_scanned_pdf(path):
    import pymupdf
    d = pymupdf.open()
    d.new_page()  # 空白页：无文字层
    d.save(str(path))
    d.close()


# ---------- DOCX 提取 ----------

def test_docx_roundtrip_headings_tables():
    import docx
    d = docx.Document()
    d.add_heading("发动机总览", level=1)
    d.add_paragraph("概述段落。")
    d.add_heading("燃烧室", level=2)
    d.add_paragraph("细节段落。")
    d.add_heading("深层标题", level=4)   # >3 级应钳到 ###
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "参数"
    t.cell(0, 1).text = "数值"
    t.cell(1, 0).text = "a|b"
    t.cell(1, 1).text = "多\n行"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.docx"
        d.save(str(p))
        md, reason = ex.extract_to_markdown(p)
    assert reason == "" and md, f"提取失败：{reason}"
    assert "# 发动机总览" in md
    assert "## 燃烧室" in md
    assert "### 深层标题" in md and "####" not in md
    assert "| 参数 | 数值 |" in md and "|---|" in md
    assert "a\\|b" in md, "单元格竖线必须转义"
    assert "多 行" in md, "单元格换行必须压成空格"


def test_docx_empty_and_corrupt_fold_to_reason():
    import docx
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "e.docx"
        docx.Document().save(str(empty))
        md, reason = ex.extract_to_markdown(empty)
        assert md is None and reason == "empty"
        bad = Path(td) / "b.docx"
        bad.write_bytes(b"PK\x03\x04 not really a zip")
        md, reason = ex.extract_to_markdown(bad)
        assert md is None and reason == "extract-failed"


# ---------- PDF 提取 ----------

def test_pdf_text_layer_extracts():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.pdf"
        _make_text_pdf(p)
        md, reason = ex.extract_to_markdown(p)
    assert reason == "" and md
    assert "mixing model" in md and "page 2" in md


def test_pdf_scanned_returns_scanned_reason():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.pdf"
        _make_scanned_pdf(p)
        md, reason = ex.extract_to_markdown(p)
    assert md is None and reason == "scanned"


def test_pdf_corrupt_folds_to_extract_failed():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.pdf"
        p.write_bytes(b"%PDF-1.7 garbage not a real pdf")
        md, reason = ex.extract_to_markdown(p)
    assert md is None and reason == "extract-failed"


# ---------- 扩展名路由与缓存 ----------

def test_uppercase_extension_routing():
    """大写扩展名不被静默误判（红队 B3 回归）：提取器与 _load_text 都要 lower()。"""
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "U.PDF"
        _make_text_pdf(pdf)
        md, reason = ex.extract_to_markdown(pdf)
        assert reason == "" and md and "mixing model" in md
        docx_mod = __import__("docx")
        dx = Path(td) / "U.Docx"
        d = docx_mod.Document()
        d.add_heading("大写扩展名", level=1)
        d.save(str(dx))
        md, reason = ex.extract_to_markdown(dx)
        assert reason == "" and md and "# 大写扩展名" in md
        md_file = Path(td) / "N.MD"
        md_file.write_text("# 标题\n正文", encoding="utf-8")
        text, bhash = index._load_text(md_file)
        assert text is not None and "正文" in text, ".MD 必须按文本解码"


def test_cache_hit_skips_reextraction_and_none_not_cached():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        ex.set_cache_dir(cache)
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            sp = Path(td) / "s.pdf"
            _make_scanned_pdf(sp)
            calls = {}
            orig = ex._extract_pdf

            def counting(path):
                key = Path(path).name
                calls[key] = calls.get(key, 0) + 1
                return orig(path)
            ex._extract_pdf = counting
            try:
                md1, r1 = ex.extract_to_markdown(p)
                md2, r2 = ex.extract_to_markdown(p)      # 应命中缓存
                _, sr = ex.extract_to_markdown(sp)       # 失败：不写缓存
                md3, r3 = ex.extract_to_markdown(p)      # 仍命中缓存
            finally:
                ex._extract_pdf = orig
            assert r1 == r2 == r3 == "" and md1 == md2 == md3
            assert calls.get("t.pdf", 0) == 1, \
                f"缓存命中不应重复提取（实提 {calls.get('t.pdf', 0)} 次）"
            assert calls.get("s.pdf", 0) == 1
            assert sr == "scanned"
            cached = list(cache.glob("*.md"))
            assert len(cached) == 1, "只有成功结果进缓存"
            assert not list(cache.glob("*.tmp")), "原子写完成后不得残留 tmp"
        finally:
            ex.set_cache_dir(None)


def test_cache_write_failure_is_nonfatal():
    with tempfile.TemporaryDirectory() as td:
        blocker = Path(td) / "not-a-dir"
        blocker.write_text("占位：缓存目录路径被文件占据 → mkdir 必败", encoding="utf-8")
        ex.set_cache_dir(blocker / "sub")
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            md, reason = ex.extract_to_markdown(p)
            assert reason == "" and md and "mixing model" in md, "缓存写失败不得影响返回值"
        finally:
            ex.set_cache_dir(None)


def test_orphan_tmp_swept_once():
    import time as _t
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        cache.mkdir()
        old_tmp = cache / "deadbeef.v1.999.tmp"
        old_tmp.write_text("残留半成品", encoding="utf-8")
        stale = _t.time() - 25 * 3600
        os.utime(old_tmp, (stale, stale))
        fresh_tmp = cache / "cafebabe.v1.999.tmp"
        fresh_tmp.write_text("新进程正在写", encoding="utf-8")
        ex.set_cache_dir(cache)
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            md, reason = ex.extract_to_markdown(p)
            assert reason == ""
            assert not old_tmp.exists(), ">24h 孤儿 tmp 应被清扫"
            assert fresh_tmp.exists(), "<24h 的 tmp 不得误删"
        finally:
            ex.set_cache_dir(None)


# ---------- 统一终态机制（索引集成，假编码器隔离） ----------

def test_xfail_deadloop_sequence_bad_pdf_recovers():
    """坏 pdf → 终态 → 第二轮零变更 → 换好文件+mtime 变 → 第三轮出块（防死循环主序列）。"""
    with _IsoEnv() as iso:
        try:
            bad = iso.vault / "bad.PDF"     # 大写扩展名一并覆盖路由
            bad.write_bytes(b"%PDF broken")
            _run_index(iso)
            meta = _load_meta(iso)
            e = meta.get("bad.PDF")
            assert e and e.get("xfail") and e.get("reason") == "extract-failed", str(e)
            assert e.get("chunks") == 0
            stale, _ = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                      collection_name="col_test",
                                      extensions=["md", "pdf", "docx"])
            assert not stale, "终态落盘后指纹检查必须判稳"

            n_calls = len(iso.encoder.calls)
            _touch(bad, time.time_ns())
            _run_index(iso)                  # 内容没变但 mtime 变：终态条目重估
            assert len(iso.encoder.calls) == n_calls, "仍是坏文件，不得产生嵌入调用"

            good = iso.vault / "good.pdf"
            _make_text_pdf(good)
            bad.unlink()
            _run_index(iso)
            meta = _load_meta(iso)
            assert "bad.PDF" not in meta, "被替换文件的旧终态条目应被裁剪"
            g = meta.get("good.pdf")
            assert g and g.get("chunks", 0) >= 1 and not g.get("xfail"), str(g)
            assert any("mixing model" in t for batch in iso.encoder.calls
                       for t in batch), "修复后应真正进入嵌入管线"
        finally:
            iso.cleanup()


def test_tbd_md_terminal_state_no_rebuild_loop():
    """TBD 重文件落 tbd 终态且不再触发重建（红队 B1 回归）；补全后自动恢复。"""
    with _IsoEnv() as iso:
        try:
            f = iso.vault / "draft.md"
            f.write_text("[TBD] 待补\nTBD — 待补\n[TBD] 待补\n", encoding="utf-8")
            _run_index(iso)
            e = _load_meta(iso).get("draft.md")
            assert e and e.get("xfail") and e.get("reason") == "tbd", str(e)
            stale, _ = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                      collection_name="col_test",
                                      extensions=["md", "pdf", "docx"],
                                      tbd_ratio=0.1)
            assert not stale
            n_calls = len(iso.encoder.calls)
            _run_index(iso)
            assert len(iso.encoder.calls) == n_calls, "稳定的 tbd 终态不得再触发嵌入"

            f.write_text("# 完成\n正式内容，不再占位。\n", encoding="utf-8")
            _touch(f, time.time_ns())
            _run_index(iso)
            e = _load_meta(iso).get("draft.md")
            assert e and not e.get("xfail") and e.get("chunks", 0) >= 1, str(e)
        finally:
            iso.cleanup()


def test_empty_md_gets_persistent_empty_terminal():
    """空文件（含仅 frontmatter）落 empty 终态并收敛——历史隐患（每轮误判 stale）就此封堵。"""
    with _IsoEnv() as iso:
        try:
            (iso.vault / "blank.md").write_text("", encoding="utf-8")
            (iso.vault / "fmonly.md").write_text("---\ntitle: 只有头\n---\n",
                                                  encoding="utf-8")
            (iso.vault / "real.md").write_text("# 正常\n内容。\n", encoding="utf-8")
            _run_index(iso)
            meta = _load_meta(iso)
            assert meta["blank.md"].get("reason") == "empty"
            assert meta["fmonly.md"].get("reason") == "empty"
            assert meta["real.md"].get("chunks") >= 1
            stale, stats = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                          collection_name="col_test",
                                          extensions=["md", "pdf", "docx"])
            assert not stale, f"空文件终态必须收敛（stats={stats}）"
            n_calls = len(iso.encoder.calls)
            _run_index(iso)
            assert len(iso.encoder.calls) == n_calls
        finally:
            iso.cleanup()


def test_scanned_pdf_terminal_and_zero_write():
    """扫描件 → scanned 终态 → 稳定；全程源目录零写入。"""
    with _IsoEnv() as iso:
        try:
            s = iso.vault / "scan.pdf"
            _make_scanned_pdf(s)
            (iso.vault / "note.md").write_text("# N\nhello world\n", encoding="utf-8")
            before = _snapshot(iso.vault)
            _run_index(iso)
            e = _load_meta(iso).get("scan.pdf")
            assert e and e.get("xfail") and e.get("reason") == "scanned", str(e)
            stale, _ = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                      collection_name="col_test",
                                      extensions=["md", "pdf", "docx"])
            assert not stale
            _run_index(iso)
            assert _snapshot(iso.vault) == before, "源目录必须零写入（含 mtime）"
        finally:
            iso.cleanup()


def test_unreadable_sentinel_converges_then_recovers():
    """OSError → unreadable 哨兵终态两轮判稳；解除占用后自动恢复入索引。"""
    orig_read_bytes = Path.read_bytes
    state = {"locked": True}

    def selective_read_bytes(self):
        if state["locked"] and self.name == "locked.pdf":
            raise PermissionError(32, "模拟独占占用")
        return orig_read_bytes(self)

    with _IsoEnv() as iso:
        try:
            f = iso.vault / "locked.pdf"
            _make_text_pdf(f)
            Path.read_bytes = selective_read_bytes
            try:
                _run_index(iso)
                e = _load_meta(iso).get("locked.pdf")
                assert e and e.get("xfail") and e.get("reason") == "unreadable", str(e)
                assert e.get("hash") == index._UNREADABLE
                stale, _ = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                          collection_name="col_test",
                                          extensions=["md", "pdf", "docx"])
                assert not stale, "unreadable 终态必须判稳"
                n_calls = len(iso.encoder.calls)
                _touch(f, time.time_ns())
                _run_index(iso)
                assert len(iso.encoder.calls) == n_calls, "仍不可读，不得触发嵌入"
            finally:
                state["locked"] = False
                Path.read_bytes = orig_read_bytes
            _touch(f, time.time_ns())
            _run_index(iso)
            e = _load_meta(iso).get("locked.pdf")
            assert e and not e.get("xfail") and e.get("chunks", 0) >= 1, \
                "解锁后真实 hash ≠ 哨兵，应自动重试转正"
        finally:
            Path.read_bytes = orig_read_bytes
            iso.cleanup()


# ---------- 静态断言与白名单 ----------

def test_static_single_source_of_truth():
    src_stale = inspect.getsource(index.kb_stale)
    src_core = inspect.getsource(index._index_core)
    for src, name in ((src_stale, "kb_stale"), (src_core, "_index_core")):
        assert "_load_text(" in src, f"{name} 必须走统一读取入口"
        assert ".read_text(" not in src, f"{name} 不得再裸读源文件"
        assert "is_tbd_heavy" in src
    core_before_tbd = src_core.index("_UNREADABLE")
    assert "_skipped(" in src_stale and "_skipped(" in src_core, \
        "排除口径必须收拢到单点谓词 _skipped"
    assert core_before_tbd > -1
    assert index.META_VERSION == 9, "v9：多格式提取+字节指纹"
    assert ex.TEXT_EXTS == {"md", "txt"}
    assert ex.BINARY_EXTS == {"pdf", "docx"}
    assert ex.SUPPORTED_EXTS == ex.TEXT_EXTS | ex.BINARY_EXTS
    lib_src = inspect.getsource(library)
    assert "SUPPORTED_EXTS" in lib_src, "library 白名单校验须引用单一事实来源"


def test_library_extensions_whitelist_validation():
    with tempfile.TemporaryDirectory() as td:
        saved = library.LIBRARIES_FILE
        try:
            library.LIBRARIES_FILE = Path(td) / "libraries.json"
            library.save_registry([library._blank_entry("t", str(td))])
            e = library.set_config("t", "extensions", "MD, .Pdf, docx ,md")
            assert e["extensions"] == ["md", "pdf", "docx"], \
                "小写归一+去重+保序"
            for bad in ("epub", "md,epub"):
                try:
                    library.set_config("t", "extensions", bad)
                    raise AssertionError(f"{bad!r} 应被白名单拒绝")
                except ValueError as ve:
                    assert "不支持" in str(ve)
            e = library.unset_config("t", "extensions")
            assert e["extensions"] is None
        finally:
            library.LIBRARIES_FILE = saved


# ---------- 运行器（对齐 audit_regression_test.py） ----------

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
