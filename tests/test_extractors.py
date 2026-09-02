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
  - 问题30：MinerU 云端 API URL 回归（断言实际 URL 字符串，不只断言调用成功）；
    pdf_text_backend（文字层 PDF 可选送 MinerU 换结构识别，is_ocr=False）默认值
    零行为变化 / 开启后正确路由 / 缓存路由隔离；试验台 backend 覆盖对文字层文件生效

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_extractors.py
隔离：索引集成用例把 index 的全部落盘路径重定向到临时目录，并用假编码器
（numpy 全零向量）替代 embedding 模型——全程不加载模型、不碰真实 Chroma。
"""
import hashlib
import inspect
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import config as cfgmod  # noqa: E402
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


class _BoomPage:
    """能被打开、但页内部结构损坏：一读文字层就抛异常。"""

    def get_text(self, *_a, **_kw):
        raise RuntimeError("模拟页内部结构损坏")


class _BoomDoc:
    """pymupdf.open() 返回的假文档：page_count 正常，逐页扫描必炸。"""

    page_count = 3

    def __init__(self):
        self.closed = False

    def __iter__(self):
        return iter([_BoomPage(), _BoomPage(), _BoomPage()])

    def close(self):
        self.closed = True


def test_pdf_page_scan_exception_folds_to_extract_failed():
    """能打开但逐页 get_text 抛异常的 PDF 必须折叠成 extract-failed，绝不外抛。

    契约回归（AGENTS.md 架构红线 1）：异常一旦穿透 _extract_pdf 会一路冒泡到
    index._index_core 主循环，导致本轮全部文件处理结果作废（save_meta 在循环后
    才调用），且坏文件没落终态，下轮索引继续在它这里崩——死循环。
    """
    import pymupdf
    from unittest.mock import patch
    docs = []

    def fake_open(*_a, **_kw):
        d = _BoomDoc()
        docs.append(d)
        return d

    with tempfile.TemporaryDirectory() as td:
        ex.set_cache_dir(Path(td) / "cache")
        try:
            p = Path(td) / "boom.pdf"
            _make_text_pdf(p)  # 真实文件：只为让 _file_md5 能算出缓存键
            with patch.object(pymupdf, "open", fake_open):
                md, reason = ex.extract_to_markdown(p)
            assert md is None and reason == "extract-failed", \
                f"逐页扫描异常应折叠为 extract-failed（实得 {md!r}, {reason!r}）"
            assert docs and docs[0].closed, "无论走哪个分支，文档都必须被 close()"
        finally:
            ex.set_cache_dir(None)


class _BoomCloseDoc(_BoomDoc):
    """逐页扫描本身会炸，且 close() 自己也炸——模拟文档半失效后 close 再爆的极端情况。"""

    def close(self):
        raise RuntimeError("模拟 close() 自身失败（文档已处于半失效状态）")


def test_pdf_close_exception_does_not_leak_folds_to_extract_failed():
    """finally 里 doc.close() 自己抛异常时，不能覆盖/顶替掉已经决定的返回值外泄。

    契约回归（round-diff-2 红队复审）：finally 块本身不在 try/except 保护范围内，
    Python 语义下 finally 中的异常会覆盖 except 分支已产生的 return 并继续外抛，
    绕开刚修好的"绝不外抛"防护，重新触发整批清零式死循环风险。
    """
    import pymupdf
    from unittest.mock import patch

    def fake_open(*_a, **_kw):
        return _BoomCloseDoc()

    with tempfile.TemporaryDirectory() as td:
        ex.set_cache_dir(Path(td) / "cache")
        try:
            p = Path(td) / "boom_close.pdf"
            _make_text_pdf(p)  # 真实文件：只为让 _file_md5 能算出缓存键
            with patch.object(pymupdf, "open", fake_open):
                md, reason = ex.extract_to_markdown(p)
            assert md is None and reason == "extract-failed", \
                f"close() 自爆也必须折叠为 extract-failed（实得 {md!r}, {reason!r}）"
        finally:
            ex.set_cache_dir(None)


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

            def counting(path, **kw):
                key = Path(path).name
                calls[key] = calls.get(key, 0) + 1
                return orig(path, **kw)
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


def test_agent_gate_freezes_unapproved_binaries():
    """人机分权门禁：agent_allowed 受限轮次不转换未授权二进制、不破坏其条目与块；
    解除限制（用户批准）后自动补齐。"""
    allowed_md = {"md", "txt"}
    with _IsoEnv() as iso:
        try:
            good = iso.vault / "doc.pdf"
            _make_text_pdf(good)
            (iso.vault / "n.md").write_text("# 笔记\n正文内容。\n", encoding="utf-8")
            # 第一轮：人类全量路径 → pdf 入索引
            _run_index(iso)
            meta = _load_meta(iso)
            assert meta["doc.pdf"]["chunks"] >= 1
            n_calls = len(iso.encoder.calls)

            # 第二轮：Agent 受限视角 → pdf 冻结：零嵌入、条目原样保留
            index._index_core(str(iso.vault), "col_test", iso.meta_file,
                              set(), set(), (), ["md", "pdf", "docx"],
                              600, 200, library_label="t",
                              incremental=True, full=False, tbd_ratio=0.1,
                              agent_allowed=allowed_md)
            meta2 = _load_meta(iso)
            assert meta2["doc.pdf"] == meta["doc.pdf"], "冻结文件条目不得被改动/裁剪"
            assert len(iso.encoder.calls) == n_calls, "冻结文件不得触发嵌入"
            stale, stats = index.kb_stale(
                str(iso.vault), meta_file=iso.meta_file,
                collection_name="col_test",
                extensions=["md", "pdf", "docx"],
                agent_allowed=allowed_md)
            assert not stale, f"受限视角必须判稳（stats={stats}）"

            # 第三轮：批准后（解除限制）→ 指纹命中，已入索引文件不重复嵌入
            _run_index(iso)
            assert len(iso.encoder.calls) == n_calls

            # 第四/五轮：新增未授权 pdf → Agent 视角不纳入且判稳；人类路径纳入
            g2 = iso.vault / "new.pdf"
            _make_text_pdf(g2, pages=1, text="brand new document content here.")
            _touch(g2, time.time_ns())
            index._index_core(str(iso.vault), "col_test", iso.meta_file,
                              set(), set(), (), ["md", "pdf", "docx"],
                              600, 200, library_label="t",
                              incremental=True, full=False, tbd_ratio=0.1,
                              agent_allowed=allowed_md)
            assert "new.pdf" not in _load_meta(iso), "未授权新文件不得入索引"
            stale2, _ = index.kb_stale(
                str(iso.vault), meta_file=iso.meta_file,
                collection_name="col_test",
                extensions=["md", "pdf", "docx"],
                agent_allowed=allowed_md)
            assert not stale2, "受限视角下未授权新文件不算 added/stale"
            _run_index(iso)
            e = _load_meta(iso).get("new.pdf")
            assert e and not e.get("xfail") and e.get("chunks", 0) >= 1
        finally:
            iso.cleanup()


def test_unchecked_format_auto_purges_its_content():
    """用户取消勾选某格式（如 docx/pdf）→ 下轮索引自动清掉该格式的条目与块，
    回到「没有该文档类型」的版本；重新勾选后凭提取缓存快速恢复。"""
    with _IsoEnv() as iso:
        try:
            (iso.vault / "n.md").write_text("# N\ncontent here.\n", encoding="utf-8")
            pdf = iso.vault / "doc.pdf"
            _make_text_pdf(pdf)
            _run_index(iso)
            meta = _load_meta(iso)
            assert meta["doc.pdf"]["chunks"] >= 1 and meta["n.md"]["chunks"] >= 1

            # 用户取消勾选 pdf：extensions 收窄为 md → 该格式自动出局
            index._index_core(str(iso.vault), "col_test", iso.meta_file,
                              set(), set(), (), ["md"],
                              600, 200, library_label="t",
                              incremental=True, full=False, tbd_ratio=0.1)
            meta2 = _load_meta(iso)
            assert "doc.pdf" not in meta2, "取消格式后其指纹条目必须被清除"
            assert "n.md" in meta2
            import chromadb as _ch
            col = _ch.PersistentClient(path=str(index.CHROMA_DIR)) \
                .get_or_create_collection("col_test")
            assert col.count() == meta2["n.md"]["chunks"], \
                f"pdf 的块必须被清理（实存 {col.count()} 块）"
            stale, stats = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                          collection_name="col_test",
                                          extensions=["md"])
            assert not stale, f"收窄后必须收敛（stats={stats}）"

            # 重新勾选 pdf：内容字节未变 → 提取缓存命中，无需重新解析
            _run_index(iso)
            meta3 = _load_meta(iso)
            assert meta3["doc.pdf"].get("chunks", 0) >= 1, "重新启用应恢复该格式入索引"
        finally:
            iso.cleanup()


def test_deleted_binary_cleans_up_even_in_agent_restricted_view():
    """物理删除二进制文件：连 Agent 受限视角也会正常裁剪清理——
    冻结只作用于「仍存在于磁盘」的未授权文件，不给已删文件续命。"""
    allowed_md = {"md", "txt"}
    with _IsoEnv() as iso:
        try:
            good = iso.vault / "gone.pdf"
            _make_text_pdf(good)
            _run_index(iso)
            assert "gone.pdf" in _load_meta(iso)
            n_calls = len(iso.encoder.calls)

            good.unlink()  # 用户删除文件
            index._index_core(str(iso.vault), "col_test", iso.meta_file,
                              set(), set(), (), ["md", "pdf", "docx"],
                              600, 200, library_label="t",
                              incremental=True, full=False, tbd_ratio=0.1,
                              agent_allowed=allowed_md)
            assert "gone.pdf" not in _load_meta(iso), "已删文件的条目应被裁剪"
            assert len(iso.encoder.calls) == n_calls, "清理不得触发嵌入"
            import chromadb as _ch
            col = _ch.PersistentClient(path=str(index.CHROMA_DIR)) \
                .get_or_create_collection("col_test")
            assert col.count() == 0, "已删文件的块必须被清理"
            stale, _ = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                      collection_name="col_test",
                                      extensions=["md", "pdf", "docx"],
                                      agent_allowed=allowed_md)
            assert not stale
        finally:
            iso.cleanup()


# ---------- 问题28：双链关系图（wikilink 出链/入链，旁路于嵌入/BM25 之外）----------

def test_links_extracted_from_wikilinks():
    """索引含 wiki 链接的文件后，meta 记录其出链目标（供双链关系图使用）；
    这条抽取发生在 clean_wikilinks 清洗之前，不影响清洗产物本身。"""
    with _IsoEnv() as iso:
        try:
            (iso.vault / "a.md").write_text("# A\n正文见 [[b]] 一节。\n", encoding="utf-8")
            (iso.vault / "b.md").write_text("# B\n普通内容，不含任何链接。\n",
                                            encoding="utf-8")
            _run_index(iso)
            meta = _load_meta(iso)
            assert meta["a.md"]["links"] == ["b"], meta["a.md"]
            assert meta["b.md"]["links"] == [], meta["b.md"]
        finally:
            iso.cleanup()


def test_links_backfilled_for_legacy_entries():
    """回填机制（验证 _links_missing 真的生效）：功能上线前建立的旧索引条目
    （size/mtime/hash 与磁盘一致，但缺 links 键）必须在下一轮增量索引中自然
    穿透两处快速路径、被重新处理一次，补齐 links；其余字段不因此改变。
    """
    with _IsoEnv() as iso:
        try:
            (iso.vault / "a.md").write_text("# A\n正文见 [[b]] 一节。\n", encoding="utf-8")
            (iso.vault / "b.md").write_text("# B\n普通内容。\n", encoding="utf-8")
            _run_index(iso)
            meta = _load_meta(iso)
            old_entry = dict(meta["a.md"])
            assert old_entry.get("links") == ["b"]

            # 模拟"功能上线前的旧索引"：手工删掉 links 键，其余字段原样保留，
            # 与磁盘文件完全一致（文件本身未改动）。
            legacy_entry = {k: v for k, v in old_entry.items() if k != "links"}
            meta["a.md"] = legacy_entry
            iso.meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            n_calls = len(iso.encoder.calls)
            _run_index(iso)  # 增量：a.md size/mtime/hash 均未变，唯独缺 links
            assert len(iso.encoder.calls) > n_calls, \
                "缺 links 的旧条目必须穿透快速路径重新处理，不能被判 unchanged 跳过"

            meta2 = _load_meta(iso)
            assert meta2["a.md"].get("links") == ["b"], "重新处理后应补齐 links"
            for k in ("hash", "chunks", "size", "mtime"):
                assert meta2["a.md"][k] == old_entry[k], \
                    f"{k} 不应因单纯回填 links 而改变（文件内容本就没变）"
            assert meta2["b.md"] == meta["b.md"], "未受影响文件的条目不应被连带改动"
        finally:
            iso.cleanup()


def test_kb_stale_detects_missing_links():
    """kb_stale 一侧必须与 _index_core 同步生效（AGENTS.md 架构红线 6）：缺 links
    的正常条目要被判 stale/changed，不能只改 _index_core 一边留下两侧失配。"""
    with _IsoEnv() as iso:
        try:
            (iso.vault / "a.md").write_text("# A\n正文见 [[b]] 一节。\n", encoding="utf-8")
            (iso.vault / "b.md").write_text("# B\n普通内容。\n", encoding="utf-8")
            _run_index(iso)
            meta = _load_meta(iso)
            stale0, stats0 = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                             collection_name="col_test",
                                             extensions=["md", "pdf", "docx"])
            assert not stale0, f"前提：正常索引后应已收敛（stats={stats0}）"

            legacy = {k: v for k, v in meta["a.md"].items() if k != "links"}
            meta["a.md"] = legacy
            iso.meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            stale, stats = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                          collection_name="col_test",
                                          extensions=["md", "pdf", "docx"])
            assert stale and stats.get("changed", 0) >= 1, f"stats={stats}"
        finally:
            iso.cleanup()


def test_terminal_entry_missing_links_not_forced_reprocess():
    """终态（xfail/tbd）条目没有 links 字段是预期状态——它们没有成功解析出的
    正文可供抽取链接，自身的重试已由 _backend_changed/_entry_converged 负责。
    _links_missing 必须放过它们，不能把已收敛的终态又强行拉回正常处理分支。
    """
    with _IsoEnv() as iso:
        try:
            f = iso.vault / "draft.md"
            f.write_text("[TBD] 待补\nTBD — 待补\n[TBD] 待补\n", encoding="utf-8")
            _run_index(iso)
            e = _load_meta(iso).get("draft.md")
            assert e and e.get("xfail") and e.get("reason") == "tbd", str(e)
            assert "links" not in e, "终态条目不应有 links 字段（前提假设）"

            stale, stats = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                          collection_name="col_test",
                                          extensions=["md", "pdf", "docx"],
                                          tbd_ratio=0.1)
            assert not stale, f"终态条目缺 links 不应被判 stale（stats={stats}）"

            n_calls = len(iso.encoder.calls)
            _run_index(iso)
            assert len(iso.encoder.calls) == n_calls, \
                "终态条目缺 links 不应被强行拉回正常处理分支重新嵌入"
            assert _load_meta(iso).get("draft.md") == e, "终态条目应原样保持不变"
        finally:
            iso.cleanup()


def test_resolve_note_relations_end_to_end():
    """出链/入链现算查询：自链不计入自己的出链/入链；断链（目标文件不存在）
    静默不出现在出链里，不报错；查询不存在的标题返回 resolved=False。"""
    with _IsoEnv() as iso:
        try:
            (iso.vault / "A.md").write_text(
                "# A\n出链：[[B]]、[[A]]（自链）、[[Ghost]]（断链，无对应文件）。\n",
                encoding="utf-8")
            (iso.vault / "B.md").write_text("# B\n普通内容。\n", encoding="utf-8")
            (iso.vault / "C.md").write_text("# C\n引用 [[A]]。\n", encoding="utf-8")
            _run_index(iso)
            meta = _load_meta(iso)
            assert meta["A.md"]["links"] == sorted({"A", "B", "Ghost"}), \
                meta["A.md"]["links"]

            r = index.resolve_note_relations(iso.meta_file, "A")
            assert r["resolved"] and r["file"] == "A.md", r
            assert r["outlinks"] == ["B.md"], r["outlinks"]
            assert r["inlinks"] == ["C.md"], r["inlinks"]
            assert "A.md" not in r["outlinks"] and "A.md" not in r["inlinks"], \
                "自链不应出现在自己的出链/入链里"

            missing = index.resolve_note_relations(iso.meta_file, "不存在的标题")
            assert missing == {"resolved": False, "file": None,
                               "outlinks": [], "inlinks": []}, missing
        finally:
            iso.cleanup()


def test_resolve_note_relations_duplicate_stem_no_crash():
    """同名标题歧义：两个不同目录下的文件 stem 相同，按标题查询不应崩溃，
    稳定返回其中一个（不要求是哪个，只要求不抛异常，与 Obsidian 自身对同名
    笔记的处理一样存在歧义）。"""
    with _IsoEnv() as iso:
        try:
            (iso.vault / "dir1").mkdir()
            (iso.vault / "dir2").mkdir()
            (iso.vault / "dir1" / "笔记.md").write_text("# 笔记一\n内容一。\n",
                                                        encoding="utf-8")
            (iso.vault / "dir2" / "笔记.md").write_text("# 笔记二\n内容二。\n",
                                                        encoding="utf-8")
            _run_index(iso)
            r = index.resolve_note_relations(iso.meta_file, "笔记")
            assert r["resolved"] is True
            assert r["file"] in ("dir1/笔记.md", "dir2/笔记.md"), r["file"]
        finally:
            iso.cleanup()


# ---------- 扫描件 OCR 后端（MinerU 云端，mock HTTP 零网络） ----------

class _FakeResp:
    def __init__(self, j=None, status=200, content=b""):
        self._j = j or {}
        self.status_code = status
        self.content = content

    def json(self):
        return self._j


class _FakeRequests:
    """脚本化 requests：按 URL 前缀分发响应并记录调用。"""

    def __init__(self, zip_md="# OCR 标题\n识别出的正文内容\n"):
        self.calls = []
        self.zip_md = zip_md
        self.fail_post = None  # 置为异常类则 post 直接抛

    def _zip_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("sub/images/x.png", b"png")
            zf.writestr("sub/main.md", self.zip_md.encode("utf-8"))
            zf.writestr("tiny.md", b"small")
        return buf.getvalue()

    def post(self, url, **kw):
        # 记录 json 请求体（第三个元素）：URL 回归测试需要断言 is_ocr 是否被
        # 原样透传，只记 URL 不够（问题30：既有 mock 只验证"怎么调用"，从不
        # 验证请求体/URL 是否是服务器上真实存在/正确的内容，这正是硬编码错误
        # 路径长期未被挡住的原因）。
        self.calls.append(("POST", url, kw.get("json")))
        if self.fail_post:
            raise self.fail_post("模拟网络超时")
        return _FakeResp({"code": 0, "data": {
            "batch_id": "b1", "file_urls": ["http://presigned/put"]}}, 200)

    def put(self, url, data=None, **kw):
        self.calls.append(("PUT", url, len(data or b"")))
        return _FakeResp(status=200)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if url.endswith("/extract-results/batch/b1"):
            return _FakeResp({"code": 0, "data": {"extract_result": [
                {"state": "done",
                 "full_zip_url": "http://cdn/result.zip"}]}}, 200)
        if url.endswith("result.zip"):
            return _FakeResp(content=self._zip_bytes(), status=200)
        return _FakeResp({}, 404)


def _with_ocr_cfg(fn):
    """临时把后端切到 mineru-cloud 并注入假 Key（恢复现场）。"""
    saved = {k: cfgmod.CFG.get(k) for k in
             ("pdf_scan_backend", "mineru_api_key", "mineru_timeout_seconds")}
    cfgmod.CFG["pdf_scan_backend"] = "mineru-cloud"
    cfgmod.CFG["mineru_api_key"] = "test-key-请勿记录"
    cfgmod.CFG["mineru_timeout_seconds"] = 60
    try:
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                cfgmod.CFG.pop(k, None)
            else:
                cfgmod.CFG[k] = v


def test_mineru_cloud_happy_path_and_cache_route():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        ex.set_cache_dir(cache)
        fake = _FakeRequests()
        saved_req = sys.modules.get("requests")
        sys.modules["requests"] = fake
        try:
            p = Path(td) / "scan.pdf"
            _make_scanned_pdf(p)
            md, reason = _with_ocr_cfg(lambda: ex.extract_to_markdown(p))
            assert reason == "" and md and "# OCR 标题" in md
            kinds = [c[0] for c in fake.calls]
            assert kinds[0] == "POST" and kinds[1] == "PUT"
            # URL 回归（问题30）：断言实际记录到的 URL 字符串本身，不是只看调用顺序——
            # mock 只验证"怎么调用"永远挡不住"调用了一个服务器上不存在的路径"这类 bug。
            posts = [c for c in fake.calls if c[0] == "POST"]
            assert posts[0][1] == f"{ex._MINERU_BASE}/file-urls/batch", \
                f"提交任务必须 POST 到 file-urls/batch（实得 {posts[0][1]!r}）"
            assert posts[0][2]["files"][0]["is_ocr"] is True, \
                "扫描件分支必须把 is_ocr=True 传进请求体"
            gets = [c for c in fake.calls if c[0] == "GET"]
            assert any(u == f"{ex._MINERU_BASE}/extract-results/batch/b1" for _, u in
                      ((c[0], c[1]) for c in gets)), \
                f"轮询必须 GET extract-results/batch/{{id}}（实得 {[c[1] for c in gets]!r}）"
            assert any(k == "GET" and k_url.endswith("result.zip")
                       for k, k_url in (c[:2] for c in fake.calls))
            # 缓存落在 ocr 路由键下（文件名中冒号净化为连字符）；二次调用命中缓存、零网络调用
            import hashlib as _h
            key = _h.md5(p.read_bytes()).hexdigest()
            cached = list(cache.glob(f"{key}.ocr-*.v{ex.EXTRACT_VERSION}.md"))
            assert len(cached) == 1, f"ocr 路由缓存缺失：{list(cache.glob('*.md'))}"
            n_calls = len(fake.calls)
            md2, reason2 = ex.extract_to_markdown(p)
            assert reason2 == "" and md2 == md
            assert len(fake.calls) == n_calls, "命中缓存不得再发网络请求"
        finally:
            if saved_req is not None:
                sys.modules["requests"] = saved_req
            else:
                sys.modules.pop("requests", None)
            ex.set_cache_dir(None)


def test_mineru_cloud_extract_uses_correct_api_urls_both_is_ocr_values():
    """URL 回归（问题30）：_mineru_cloud_extract 提交/轮询必须命中 mineru.net 真实
    存在的路径，且 is_ocr 必须原样透传进请求体——两个调用点（扫描件传 True、
    文字层传 False）都要覆盖，不能只测一个。

    历史 bug：硬编码用了 file-protocol/batch[/{id}]，实测服务器对该路径返回
    HTTP 404（纯文本 "page not found"，路由层面不存在，不是鉴权/参数错误）；
    正确路径是提交 file-urls/batch、轮询 extract-results/batch/{id}。既有 mock
    测试只验证"代码怎么调用 requests"，从不检查 URL 字符串是否是服务器上真实
    存在的路径——这正是该 bug 未被回归挡住的原因，因此这里必须直接断言 mock
    记录到的实际 URL 字符串，而不是只断言"提取成功"这种弱结论。
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "doc.pdf"
        p.write_bytes(b"%PDF-1.7 fake bytes, only used for PUT upload + md5 key")
        saved_req = sys.modules.get("requests")
        try:
            for is_ocr in (True, False):
                fake = _FakeRequests()
                sys.modules["requests"] = fake
                md, reason = _with_ocr_cfg(
                    lambda: ex._mineru_cloud_extract(p, is_ocr=is_ocr))
                assert reason == "" and md, f"is_ocr={is_ocr} 提取应成功：{reason}"

                posts = [c for c in fake.calls if c[0] == "POST"]
                assert posts, "必须发起过一次提交请求"
                assert posts[0][1] == f"{ex._MINERU_BASE}/file-urls/batch", \
                    f"提交任务必须 POST 到 file-urls/batch（实得 {posts[0][1]!r}）"
                assert posts[0][2]["files"][0]["is_ocr"] is is_ocr, \
                    f"is_ocr={is_ocr} 必须原样传进请求体（实得 {posts[0][2]!r}）"

                gets = [c for c in fake.calls if c[0] == "GET"]
                poll_urls = [c[1] for c in gets
                            if c[1] == f"{ex._MINERU_BASE}/extract-results/batch/b1"]
                assert poll_urls, \
                    f"轮询必须 GET extract-results/batch/{{id}}" \
                    f"（实得 GET 调用 {[c[1] for c in gets]!r}）"

                all_urls = " ".join(c[1] for c in fake.calls)
                assert "/file-protocol/" not in all_urls, \
                    f"旧的错误路径 file-protocol 不得再出现（实得调用：{fake.calls!r}）"
        finally:
            if saved_req is not None:
                sys.modules["requests"] = saved_req
            else:
                sys.modules.pop("requests", None)


def test_get_model_version_default_and_invalid_fallback():
    """问题33：get_model_version() 默认 vlm；非法值防御回退 vlm（照抄
    get_scan_backend/get_text_backend 的防御风格：脏配置值绝不让上层拿到
    一个 mineru.net 服务端会拒绝或产生非预期行为的字符串）。"""
    saved = cfgmod.CFG.get("mineru_model_version")
    try:
        cfgmod.CFG.pop("mineru_model_version", None)
        assert ex.get_model_version() == "vlm", "未配置时默认应为 vlm"
        cfgmod.CFG["mineru_model_version"] = "pipeline"
        assert ex.get_model_version() == "pipeline"
        cfgmod.CFG["mineru_model_version"] = "VLM"  # 大小写不敏感
        assert ex.get_model_version() == "vlm"
        cfgmod.CFG["mineru_model_version"] = "some-garbage-value"
        assert ex.get_model_version() == "vlm", "非法值必须回退 vlm，不能原样透传"
    finally:
        if saved is None:
            cfgmod.CFG.pop("mineru_model_version", None)
        else:
            cfgmod.CFG["mineru_model_version"] = saved


def test_mineru_cloud_extract_sends_model_version_both_is_ocr_values():
    """问题33：请求体必须携带 model_version 字段，且跟随 config.mineru_model_version
    变化——两个调用点（扫描件 is_ocr=True、文字层 is_ocr=False）都要覆盖。

    背景：此前请求体从未传这个字段，服务端会用未声明时的默认版本（较弱的
    pipeline 模式），且这个遗漏不会以任何错误形式暴露（请求照样成功、返回
    200，只是解析精度更低）——纯 mock 断言"提取成功"完全测不出这类缺陷，
    必须直接断言请求体里的字段值。
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "doc.pdf"
        p.write_bytes(b"%PDF-1.7 fake bytes, only used for PUT upload + md5 key")
        saved_req = sys.modules.get("requests")
        saved_mv = cfgmod.CFG.get("mineru_model_version")
        try:
            for is_ocr in (True, False):
                for mv in ("vlm", "pipeline"):
                    cfgmod.CFG["mineru_model_version"] = mv
                    fake = _FakeRequests()
                    sys.modules["requests"] = fake
                    md, reason = _with_ocr_cfg(
                        lambda: ex._mineru_cloud_extract(p, is_ocr=is_ocr))
                    assert reason == "" and md,                         f"is_ocr={is_ocr} model_version={mv} 提取应成功：{reason}"
                    posts = [c for c in fake.calls if c[0] == "POST"]
                    assert posts, "必须发起过一次提交请求"
                    assert posts[0][2].get("model_version") == mv,                         f"is_ocr={is_ocr} 时 model_version={mv} 必须原样传进请求体"                         f"（实得 {posts[0][2]!r}）"
        finally:
            if saved_req is not None:
                sys.modules["requests"] = saved_req
            else:
                sys.modules.pop("requests", None)
            if saved_mv is None:
                cfgmod.CFG.pop("mineru_model_version", None)
            else:
                cfgmod.CFG["mineru_model_version"] = saved_mv


def test_mineru_cloud_extract_default_model_version_is_vlm_when_unset():
    """问题33：config 里完全不设 mineru_model_version 时（例如旧 config.json
    未来某天被手工清空这一键），请求体仍必须落到 vlm，绝不能悄悄退回"没有
    这个字段"的旧行为（那正是本次要修的缺陷本身）。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "doc.pdf"
        p.write_bytes(b"%PDF-1.7 fake bytes")
        saved_req = sys.modules.get("requests")
        saved_mv = cfgmod.CFG.get("mineru_model_version")
        try:
            cfgmod.CFG.pop("mineru_model_version", None)
            fake = _FakeRequests()
            sys.modules["requests"] = fake
            md, reason = _with_ocr_cfg(
                lambda: ex._mineru_cloud_extract(p, is_ocr=True))
            assert reason == "" and md
            posts = [c for c in fake.calls if c[0] == "POST"]
            assert posts[0][2].get("model_version") == "vlm",                 f"未配置时必须落到 vlm（实得 {posts[0][2]!r}）"
        finally:
            if saved_req is not None:
                sys.modules["requests"] = saved_req
            else:
                sys.modules.pop("requests", None)
            if saved_mv is None:
                cfgmod.CFG.pop("mineru_model_version", None)
            else:
                cfgmod.CFG["mineru_model_version"] = saved_mv


def test_mineru_no_key_keeps_scanned_and_api_failure_folds():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.pdf"
        _make_scanned_pdf(p)

        def run_nokey():
            saved = cfgmod.CFG.get("pdf_scan_backend")
            cfgmod.CFG["pdf_scan_backend"] = "mineru-cloud"
            cfgmod.CFG["mineru_api_key"] = ""
            try:
                return ex.extract_to_markdown(p)
            finally:
                if saved is None:
                    cfgmod.CFG.pop("pdf_scan_backend", None)
                else:
                    cfgmod.CFG["pdf_scan_backend"] = saved

        md, reason = run_nokey()
        assert md is None and reason == "scanned", "未配 Key 保持 scanned 语义"

        # API 异常（超时等）必须折叠为 extract-failed，绝不外抛
        fake = _FakeRequests()
        fake.fail_post = TimeoutError
        saved_req = sys.modules.get("requests")
        sys.modules["requests"] = fake
        saved_backend = cfgmod.CFG.get("pdf_scan_backend")
        saved_key = cfgmod.CFG.get("mineru_api_key")
        cfgmod.CFG["pdf_scan_backend"] = "mineru-cloud"
        cfgmod.CFG["mineru_api_key"] = "k"
        try:
            md, reason = ex.extract_to_markdown(p)
            assert md is None and reason == "extract-failed"
        finally:
            if saved_req is not None:
                sys.modules["requests"] = saved_req
            else:
                sys.modules.pop("requests", None)
            for k, v in (("pdf_scan_backend", saved_backend),
                         ("mineru_api_key", saved_key)):
                if v is None:
                    cfgmod.CFG.pop(k, None)
                else:
                    cfgmod.CFG[k] = v


def test_mineru_no_key_text_branch_folds_to_extract_failed_not_scanned():
    """文字层分支（is_ocr=False）缺 Key：绝不能沿用扫描件分支的 "scanned" reason。

    "scanned" 在本项目的语义里明确是"这份文件是扫描件、没有文字层"（GUI 据此提示
    "发现扫描件 PDF...或改用文字层版本"）。若文字层 PDF 因为 pdf_text_backend=
    mineru-cloud 但缺 Key 而失败，也标成 scanned，会让用户看到"改用文字层版本"
    这种荒谬建议——这份文件本来就是文字层。缺 Key 属于配置问题，应折叠为
    extract-failed（提取器现有的通用失败语义），不是扫描件判定。is_ocr=True
    分支的既有行为（保持 scanned）不受影响，另有用例锁定。
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.pdf"
        p.write_bytes(b"%PDF-1.7 irrelevant bytes, direct call bypasses pymupdf.open")
        saved_key = cfgmod.CFG.get("mineru_api_key")
        cfgmod.CFG["mineru_api_key"] = ""
        try:
            md, reason = ex._mineru_cloud_extract(p, is_ocr=False)
            assert md is None and reason == "extract-failed", \
                f"文字层分支缺 Key 必须是 extract-failed，不是 scanned（实得 {reason!r}）"
        finally:
            if saved_key is None:
                cfgmod.CFG.pop("mineru_api_key", None)
            else:
                cfgmod.CFG["mineru_api_key"] = saved_key


def test_xsrc_retry_after_enabling_ocr_backend():
    """存量 scanned 终态在启用后端后自动重试转正；失败重落终态带新签名。"""
    orig_cloud = ex._mineru_cloud_extract
    with _IsoEnv() as iso:
        try:
            s = iso.vault / "scan.pdf"
            _make_scanned_pdf(s)
            (iso.vault / "n.md").write_text("# N\nhello world\n", encoding="utf-8")
            _run_index(iso)  # 后端=none：落 scanned 终态
            e0 = _load_meta(iso)["scan.pdf"]
            assert e0["xfail"] and e0["reason"] == "scanned"
            assert e0["xsrc"] == ex.current_backend_sig()

            stale, _ = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                      collection_name="col_test",
                                      extensions=["md", "pdf", "docx"])
            assert not stale, "能力未变时必须判稳"

            # 用户启用云端 OCR（mock 客户端返回成功结果）
            cfgmod.CFG["pdf_scan_backend"] = "mineru-cloud"
            cfgmod.CFG["mineru_api_key"] = "k"
            # is_ocr 现为必填形参（问题30）：桩函数须接受任意调用方式（含关键字），
            # 不能硬编码只接受 1 个位置参数，否则调用点一改就抛 TypeError。
            ex._mineru_cloud_extract = lambda p, **_kw: ("# OCR 转正\n来自扫描件的正文\n", "")
            try:
                stale, stats = index.kb_stale(
                    str(iso.vault), meta_file=iso.meta_file,
                    collection_name="col_test",
                    extensions=["md", "pdf", "docx"])
                assert stale and stats.get("changed") == 1, \
                    f"签名失配应判待重试（stats={stats}）"
                _run_index(iso)
                e1 = _load_meta(iso)["scan.pdf"]
                assert not e1.get("xfail") and e1.get("chunks", 0) >= 1, str(e1)
                assert any("OCR 转正" in t for batch in iso.encoder.calls
                           for t in batch), "转正内容应进入嵌入管线"
                # 幂等：转正后再跑不再重复嵌入
                n_calls = len(iso.encoder.calls)
                _run_index(iso)
                assert len(iso.encoder.calls) == n_calls
            finally:
                ex._mineru_cloud_extract = orig_cloud
                for k in ("pdf_scan_backend", "mineru_api_key"):
                    cfgmod.CFG.pop(k, None)
        finally:
            iso.cleanup()


# ---------- 问题30：pdf_text_backend（有文字层 PDF 可选送 MinerU 换结构识别） ----------

def test_pdf_text_backend_default_local_unchanged():
    """默认值回归：不设置 pdf_text_backend（或显式设为 "local"）时，文字层 PDF 的
    提取路径/产出内容/缓存路由必须与改动前逐字节一致——本次改动"零行为变化除非
    用户主动打开"的核心保证。用调用计数断言默认配置下绝不触达 _mineru_cloud_extract
    （不依赖 sys.modules 探测 requests 是否被 import，避免和同进程内其它用例的
    import 顺序产生耦合，那样断言会很脆弱）。
    """
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        ex.set_cache_dir(cache)
        saved = cfgmod.CFG.get("pdf_text_backend")
        assert saved in (None, "local"), \
            f"前置假设：全局 pdf_text_backend 应为默认 local（实得 {saved!r}）——存在测试间配置泄漏"
        orig_cloud = ex._mineru_cloud_extract
        calls = []
        ex._mineru_cloud_extract = lambda *a, **kw: calls.append((a, kw))
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            md, reason, route, cached = ex._extract_full(p)
            assert reason == "" and md and "mixing model" in md and "page 2" in md
            assert route == "local", f"默认必须走 local 路由（实得 {route!r}）"
            assert cached is False
            assert not calls, "默认（local）后端绝不该调用 _mineru_cloud_extract"
            key = hashlib.md5(p.read_bytes()).hexdigest()
            assert (cache / f"{key}.local.v{ex.EXTRACT_VERSION}.md").exists(), \
                "缓存文件必须落在 local 路由键下，与改动前的命名格式一致"

            # 显式设为 "local"（而非留空走 DEFAULTS 回退）同样验证一次，两种表达
            # 方式必须行为一致
            cfgmod.CFG["pdf_text_backend"] = "local"
            md2, reason2, route2, cached2 = ex._extract_full(p)
            assert reason2 == "" and md2 == md and route2 == "local" and cached2 is True
            assert not calls
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)
            if saved is None:
                cfgmod.CFG.pop("pdf_text_backend", None)
            else:
                cfgmod.CFG["pdf_text_backend"] = saved


def test_pdf_text_backend_mineru_local_degrades_to_local_no_network():
    """问题33：pdf_text_backend=mineru-local 是尚未实现的占位入口。选中后必须
    安全退化为本地直提（pymupdf4llm）——绝不能因为"选了本地模型但没实现"就让
    一份本来能被本地处理的文字层 PDF 变成不产出。同时验证零网络调用（不能
    因为退化逻辑写错而误触 _mineru_cloud_extract），且 route/缓存键与纯 local
    路径完全一致（因为产出内容确实等价，缓存不该为一个"实际没发生的云端调用"
    单独开一条路由标签）。
    """
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        ex.set_cache_dir(cache)
        p = Path(td) / "t.pdf"
        _make_text_pdf(p)
        saved_backend = cfgmod.CFG.get("pdf_text_backend")
        orig_cloud = ex._mineru_cloud_extract
        calls = []
        ex._mineru_cloud_extract = lambda *a, **kw: calls.append((a, kw))
        try:
            cfgmod.CFG["pdf_text_backend"] = "mineru-local"
            md, reason, route, cached = ex._extract_full(p)
            assert reason == "" and md and "mixing model" in md and "page 2" in md,                 f"mineru-local 占位必须退化产出本地直提内容，不能变成不产出（{reason!r}）"
            assert route == "local",                 f"退化后 route 必须仍是 local（内容确实是本地直提产出，实得 {route!r}）"
            assert cached is False
            assert not calls, "mineru-local 占位绝不能触发任何网络调用"

            key = hashlib.md5(p.read_bytes()).hexdigest()
            assert (cache / f"{key}.local.v{ex.EXTRACT_VERSION}.md").exists(),                 "缓存必须落在与纯 local 路径相同的 local 路由键下"

            # 二次调用命中缓存，且缓存与显式 local 配置下产出的文件是同一份
            # （验证退化逻辑与 local 分支产出内容逐字节一致，不是"看起来一样"）
            cfgmod.CFG["pdf_text_backend"] = "local"
            md_local, reason_local, route_local, cached_local = ex._extract_full(p)
            assert cached_local is True and md_local == md and route_local == "local"
            assert not calls, "缓存命中路径同样不该触发网络调用"
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)
            if saved_backend is None:
                cfgmod.CFG.pop("pdf_text_backend", None)
            else:
                cfgmod.CFG["pdf_text_backend"] = saved_backend


def test_get_text_backend_accepts_mineru_local():
    """get_text_backend() 必须认可 mineru-local 为合法值（不能被非法值防御误伤
    回退成 local——那样配置页选中它会在读回时"看起来什么都没选"，与
    test_choice_fields_match_defaults 对 GUI 层的假设脱节）。"""
    saved = cfgmod.CFG.get("pdf_text_backend")
    try:
        cfgmod.CFG["pdf_text_backend"] = "mineru-local"
        assert ex.get_text_backend() == "mineru-local"
    finally:
        if saved is None:
            cfgmod.CFG.pop("pdf_text_backend", None)
        else:
            cfgmod.CFG["pdf_text_backend"] = saved


def test_pdf_text_backend_mineru_cloud_routes_with_is_ocr_false():
    """pdf_text_backend=mineru-cloud 开启后：文字层 PDF 改走
    _mineru_cloud_extract(path, is_ocr=False)（断言 is_ocr 参数值，不能漏），
    产出路由为 "mineru-text"，且缓存能按这个新路由正确命中/未命中。
    """
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        ex.set_cache_dir(cache)
        p = Path(td) / "t.pdf"
        _make_text_pdf(p)
        saved_backend = cfgmod.CFG.get("pdf_text_backend")
        orig_cloud = ex._mineru_cloud_extract
        calls = []

        def fake_cloud(path, is_ocr):
            calls.append((Path(path).name, is_ocr))
            return "# 云端结构识别结果\n正文由 MinerU 返回\n", ""

        ex._mineru_cloud_extract = fake_cloud
        cfgmod.CFG["pdf_text_backend"] = "mineru-cloud"
        try:
            md, reason, route, cached = ex._extract_full(p)
            assert reason == "" and "云端结构识别结果" in md
            assert route == "mineru-text", f"文字层送云端的路由必须是 mineru-text（实得 {route!r}）"
            assert cached is False
            assert calls == [("t.pdf", False)], \
                f"必须调用 _mineru_cloud_extract(path, is_ocr=False)（实得 {calls!r}）"

            key = hashlib.md5(p.read_bytes()).hexdigest()
            assert (cache / f"{key}.mineru-text.v{ex.EXTRACT_VERSION}.md").exists(), \
                "缓存必须落在独立的 mineru-text 路由键下"

            # 二次调用应命中缓存，零重复调用云端
            n_calls = len(calls)
            md2, reason2, route2, cached2 = ex._extract_full(p)
            assert cached2 is True and route2 == "mineru-text" and md2 == md
            assert len(calls) == n_calls, "缓存命中不应重复调用 _mineru_cloud_extract"
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)
            if saved_backend is None:
                cfgmod.CFG.pop("pdf_text_backend", None)
            else:
                cfgmod.CFG["pdf_text_backend"] = saved_backend


def test_pdf_text_backend_cache_route_isolation():
    """缓存路由隔离：同一份文件在 "local" 路由下已有缓存时，切换
    pdf_text_backend=mineru-cloud 不会被 local 缓存假命中——应该走新路由重新
    提取，两条缓存互不干扰、各自独立存在（即便理论上同一份文件字节不可能同时
    产生两种路由的缓存，独立标签仍能让缓存层不依赖"分类逻辑永远不变"这个假设）。
    """
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        ex.set_cache_dir(cache)
        p = Path(td) / "t.pdf"
        _make_text_pdf(p)
        saved_backend = cfgmod.CFG.get("pdf_text_backend")
        orig_cloud = ex._mineru_cloud_extract
        try:
            # 第一步：默认 local 后端先跑一次，产生 local 路由缓存
            md_local, reason_local, route_local, _ = ex._extract_full(p)
            assert reason_local == "" and route_local == "local"

            # 第二步：切到 mineru-cloud，必须重新提取（不得命中上一步的 local 缓存）
            calls = []

            def fake_cloud(path, is_ocr):
                calls.append(is_ocr)
                return "# 云端版\n与本地直提内容不同的标记文字\n", ""

            ex._mineru_cloud_extract = fake_cloud
            cfgmod.CFG["pdf_text_backend"] = "mineru-cloud"
            md_cloud, reason_cloud, route_cloud, cached_cloud = ex._extract_full(p)
            assert reason_cloud == "" and route_cloud == "mineru-text"
            assert cached_cloud is False, "不得假命中 local 路由的缓存"
            assert calls == [False], "必须真正调用云端（证明没有被旧缓存拦截）"
            assert md_cloud != md_local, "两条路由的产出应彼此独立（用不同内容验证未被串用）"

            # 第三步：两条缓存应同时存在、互不覆盖
            key = hashlib.md5(p.read_bytes()).hexdigest()
            assert (cache / f"{key}.local.v{ex.EXTRACT_VERSION}.md").exists()
            assert (cache / f"{key}.mineru-text.v{ex.EXTRACT_VERSION}.md").exists()

            # 第四步：切回 local，仍能命中第一步留下的 local 缓存（未被第二步覆盖/污染）
            cfgmod.CFG["pdf_text_backend"] = "local"
            md_local2, reason_local2, route_local2, cached_local2 = ex._extract_full(p)
            assert cached_local2 is True and route_local2 == "local" and md_local2 == md_local
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)
            if saved_backend is None:
                cfgmod.CFG.pop("pdf_text_backend", None)
            else:
                cfgmod.CFG["pdf_text_backend"] = saved_backend


def test_extract_preview_backend_override_reaches_text_layer_branch():
    """提取试验台"后端单次覆盖"下拉对文字层文件生效的端到端验证（问题30）。

    _extract_pdf 重构前，backend 参数只在扫描件分支被读取（函数顶部 `backend =
    backend or get_scan_backend()` 无条件把 backend resolve 成扫描件语义，文字层
    分支从不引用这个变量）；对一份有文字层的 PDF 在试验台下拉选择"MinerU 云端
    OCR"是完全的死选项——不报错，但也绝不会真的调用云端，静默地照常走本地直提，
    用户怎么切这个下拉、对文字层文件都看不出任何区别。
    这里直接验证 extract_preview(path, backend="mineru-cloud") 对文字层文件确实
    调用了 _mineru_cloud_extract(is_ocr=False)、产出的是云端内容而非本地
    pymupdf4llm 的原文；backend=None 时则相反（跟随全局 local，绝不碰云端）——
    两条路径必须真正分叉，不是巧合。
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.pdf"
        _make_text_pdf(p)
        orig_cloud = ex._mineru_cloud_extract
        calls = []
        ex._mineru_cloud_extract = lambda path, is_ocr: (
            calls.append(is_ocr) or ("# 云端版结构识别专属标记\n", ""))
        saved = cfgmod.CFG.get("pdf_text_backend")
        assert saved in (None, "local"), \
            f"前置假设：全局应跟随默认 local（实得 {saved!r}），证明下面的云端调用" \
            f"确实来自 backend 参数覆盖，不是全局配置本来就是云端"
        try:
            ex.set_cache_dir(Path(td) / "cache_over")
            r = ex.extract_preview(p, backend="mineru-cloud")
            assert r["reason"] == "" and "云端版结构识别专属标记" in r["md"]
            assert "mixing model" not in r["md"], "覆盖生效时不该再混入本地提取的原文"
            assert r["route"] == "mineru-text"
            assert calls == [False], \
                f"backend 覆盖对文字层文件必须真正调用云端且 is_ocr=False（实得 {calls}）"

            ex.set_cache_dir(Path(td) / "cache_auto")
            r2 = ex.extract_preview(p, backend=None)
            assert r2["reason"] == "" and "mixing model" in r2["md"]
            assert r2["route"] == "local"
            assert calls == [False], "backend=None 跟随全局 local 时绝不该调用云端"
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)


def test_extract_preview_contract():
    """试验台预览接口：字段齐全、公开 pair API 行为不变、缓存命中可见。"""
    with tempfile.TemporaryDirectory() as td:
        ex.set_cache_dir(Path(td) / "cache")
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            r1 = ex.extract_preview(p)
            assert r1["reason"] == "" and r1["md"] and r1["chars"] > 0
            assert r1["cached"] is False and r1["route"] == "local"
            assert isinstance(r1["elapsed"], float)
            # 公开 API 契约不回归：仍是 (md, reason) 二元组
            pair = ex.extract_to_markdown(p)
            assert isinstance(pair, tuple) and len(pair) == 2
            r2 = ex.extract_preview(p)
            assert r2["cached"] is True and r2["md"] == r1["md"]
            # 失败路径的字段形态
            bad = Path(td) / "b.pdf"
            bad.write_bytes(b"%PDF broken")
            r3 = ex.extract_preview(bad)
            assert r3["md"] is None and r3["reason"] == "extract-failed"
        finally:
            ex.set_cache_dir(None)


def test_sanitize_render_md_inline_tags():
    """渲染预览净化：<b>/<i> 转 Markdown 强调，<u>/<span> 等裸 HTML 剥除。"""
    s = ex.sanitize_render_md("<b>粗</b> 与 <strong>强</strong>、<i>斜</i>"
                              "<u>下划</u><span style=x>杂</span>尾")
    assert s == "**粗** 与 **强**、*斜*下划杂尾"
    assert "<" not in s and ">" not in s


def test_preview_backend_override():
    """试验台后端单次覆盖：覆盖值只影响本次预览，不污染全局配置。"""
    orig_cloud = ex._mineru_cloud_extract
    with tempfile.TemporaryDirectory() as td:
        ex.set_cache_dir(Path(td) / "cache")
        p = Path(td) / "s.pdf"
        _make_scanned_pdf(p)
        saved = cfgmod.CFG.get("pdf_scan_backend")
        assert saved == "none", \
            f"前置假设：全局应为 none（实得 {saved!r}）——存在测试间配置泄漏"
        try:
            ex._mineru_cloud_extract = lambda _p, **_kw: ("# 云端识别\n正文\n", "")
            r_default = ex.extract_preview(p)
            assert r_default["reason"] == "scanned" and r_default["md"] is None
            r_over = ex.extract_preview(p, backend="mineru-cloud")
            assert r_over["reason"] == "" and r_over["route"] == "ocr:mineru-cloud"
            assert "云端识别" in r_over["md"]
            assert cfgmod.CFG.get("pdf_scan_backend") == (
                saved if saved is not None else cfgmod.CFG.get("pdf_scan_backend"))
        finally:
            ex._mineru_cloud_extract = orig_cloud
            if saved is None:
                cfgmod.CFG.pop("pdf_scan_backend", None)
            else:
                cfgmod.CFG["pdf_scan_backend"] = saved
            ex.set_cache_dir(None)


def test_preview_job_uses_isolated_cache():
    """试验台预览必须用一次性临时缓存：既不写、也不读生产缓存目录。

    否则用户在试验台里手选 mineru-cloud 试一个文件，产物会落进生产缓存；
    之后即便全局 pdf_scan_backend=none，正式索引也会在查缓存那步直接命中这份
    云端产物、跳过 backend=none 本该走的"跳过"分支（且 --full 不清 extract_cache，
    不可追溯、不可撤销）。
    """
    import queue as _q
    orig_cloud = ex._mineru_cloud_extract
    prod = ex.get_cache_dir()

    def _snap():
        try:
            return sorted(p.name for p in prod.glob("*"))
        except OSError:
            return []

    with tempfile.TemporaryDirectory() as td:
        try:
            p = Path(td) / "s.pdf"
            _make_scanned_pdf(p)
            prod_existed = prod.exists()
            before = _snap()

            ex._mineru_cloud_extract = lambda _p, **_kw: ("# 预览专用云端结果\n正文\n", "")
            q = _q.Queue()
            ex._preview_job(q, str(p), "mineru-cloud")
            payload = q.get_nowait()
            assert payload["ok"], payload
            info = payload["info"]
            assert info["reason"] == "" and "预览专用云端结果" in info["md"], str(info)
            assert info["route"] == "ocr:mineru-cloud"

            assert _snap() == before, "预览绝不能往生产缓存目录写任何文件"
            assert prod.exists() == prod_existed, "预览不得凭空创建生产缓存目录"
            assert ex.get_cache_dir() == prod, "预览结束后必须还原缓存目录"

            # 生产路径（全局 backend=none，默认缓存目录）不得看见预览的云端产物
            md, reason = ex.extract_to_markdown(p)
            assert md is None and reason == "scanned", \
                f"正式索引不得命中预览产生的云端缓存（实得 {reason!r}）"
            assert _snap() == before, "scanned 是失败终态，同样不写缓存"
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)


def test_preview_job_keeps_caller_owned_cache_dir():
    """父进程传入 cache_dir 时，子进程只用不删——清理责任在调用方。

    子进程随时会被 terminate() 硬杀（超时/取消），杀掉的进程执行不到任何 Python
    收尾，"自己建自己删"的承诺必然落空、云端 OCR 产物永久残留 %TEMP%。
    """
    import queue as _q
    orig_cloud = ex._mineru_cloud_extract
    prod = ex.get_cache_dir()
    with tempfile.TemporaryDirectory() as td:
        try:
            p = Path(td) / "s.pdf"
            _make_scanned_pdf(p)
            owned = Path(td) / "caller_cache"
            owned.mkdir()

            ex._mineru_cloud_extract = lambda _p, **_kw: ("# 云端结果\n正文\n", "")
            q = _q.Queue()
            ex._preview_job(q, str(p), "mineru-cloud", str(owned))
            payload = q.get_nowait()
            assert payload["ok"], payload
            assert "云端结果" in payload["info"]["md"]

            assert owned.is_dir(), "调用方传入的缓存目录不得被子进程删除"
            assert list(owned.glob("*")), "预览产物应落在调用方指定的隔离目录里"
            assert ex.get_cache_dir() == prod, "预览结束后必须还原缓存目录"
        finally:
            ex._mineru_cloud_extract = orig_cloud
            ex.set_cache_dir(None)


def test_preview_job_self_cleans_when_no_cache_dir():
    """不传 cache_dir（直调/测试路径）：仍是自己建、自己清，向后兼容不破。"""
    import queue as _q

    def _snap():
        return {str(d) for d in Path(tempfile.gettempdir())
                .glob("extract_preview_*")}

    prod = ex.get_cache_dir()
    with tempfile.TemporaryDirectory() as td:
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            before = _snap()
            q = _q.Queue()
            ex._preview_job(q, str(p), None)
            payload = q.get_nowait()
            assert payload["ok"], payload
            assert _snap() - before == set(), \
                "不传 cache_dir 时自建的临时目录必须由自己清理干净"
            assert ex.get_cache_dir() == prod, "预览结束后必须还原缓存目录"
        finally:
            ex.set_cache_dir(None)


def test_preview_job_result_survives_cleanup_failure():
    """收尾失败不得覆盖已成功的结果：q.put 必须排在任何清理动作之前。"""
    import queue as _q
    orig_set = ex.set_cache_dir
    calls = []

    def _boom(p):
        calls.append(p)
        orig_set(p)
        if len(calls) > 1:          # 还原那次（收尾步骤）炸掉
            raise OSError("cleanup boom")

    with tempfile.TemporaryDirectory() as td:
        try:
            p = Path(td) / "t.pdf"
            _make_text_pdf(p)
            owned = Path(td) / "cache"
            owned.mkdir()
            q = _q.Queue()
            ex.set_cache_dir = _boom
            try:
                ex._preview_job(q, str(p), None, str(owned))
            except OSError:
                pass  # 收尾异常允许上抛，但结果必须已经送出
            payload = q.get_nowait()
            assert payload["ok"], f"收尾失败不得把成功误报为失败：{payload}"
            assert q.empty(), "同一轮不得投递第二个结果"
        finally:
            ex.set_cache_dir = orig_set
            ex.set_cache_dir(None)


def test_preview_job_process_isolation():
    """试验台的进程隔离根基：spawn 子进程提取成功经队列回传（GIL 解耦）。"""
    import multiprocessing as mp
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.pdf"
        _make_text_pdf(p)
        q = mp.Queue()
        proc = mp.Process(target=ex._preview_job, args=(q, str(p), None),
                          daemon=True)
        proc.start()
        payload = q.get(timeout=120)
        proc.join(timeout=10)
        assert payload["ok"], payload
        info = payload["info"]
        assert info["reason"] == "" and info["route"] == "local"
        assert info["chars"] > 0 and "mixing model" in info["md"]


# ---------- 问题32 / R3-G7：converting 相位单点还原 ----------

def test_converting_phase_restored_after_extract():
    """R3/G7 锁定（问题 32）：converting 置位后在 extract_to_markdown 返回处
    单点还原 scanning——提取失败/空 body/成功切块三个出口全覆盖，转换豁免
    窗口严格闭合，绝不泄漏进 embedding/writing 等后续相位。"""
    calls = []
    real_update = index.update_progress

    def spy(**fields):
        real_update(**fields)
        calls.append(dict(index._progress))

    with _IsoEnv() as iso:
        p = iso.vault / "doc.pdf"
        _make_text_pdf(p)
        index.update_progress = spy
        try:
            _run_index(iso, incremental=False, full=True)
        finally:
            index.update_progress = real_update
        phases = [c.get("phase") for c in calls]
        assert "converting" in phases, phases
        i = phases.index("converting")
        assert phases[i + 1] == "scanning", \
            f"转换返回后必须紧跟 scanning 还原：{phases[i:i + 3]}"
        rest = phases[i + 1:]
        assert "converting" not in rest, \
            f"转换豁免泄漏进后续相位：{rest}"
        # 结构断言：还原语句位于提取调用之后、第一个出口分支（if body is None）
        # 之前——物理上覆盖全部三个出口
        src = inspect.getsource(index._index_core)
        i_ext = src.index("extract_to_markdown(fpath)")
        i_restore = src.find('phase="scanning"', i_ext)
        i_none = src.index("if body is None:", i_ext)
        assert i_ext < i_restore < i_none, \
            "单点还原必须紧随提取调用、先于任何出口分支"


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
