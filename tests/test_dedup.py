"""test_dedup.py — 近似文档去重（MinHash+LSH，文本级）（M1）回归测试。

风格对齐：标准库、非 pytest、逐用例 PASS/FAIL、`_run_all()`。
只读建议工具：绝不动源文件。测试用隔离临时 vault + 手写 cfg dict（不碰注册表/
Chroma）；md/txt 直读源，pdf/docx 走 read_cached_markdown（未提取→跳过计数）。

覆盖：
  - MinHash/sketch_jaccard：同一自比 1.0、异文近 0、短文档用精确交并比
  - find_duplicates：两份相同 md → 一组且相似度 ≥ 阈值
  - 阈值过滤：近似但在阈值以下 → 不输出
  - 完全不同文档 → 无重复组
  - 未提取的 pdf → 计入 skipped，不算 scanned
  - 源目录零写入快照断言
  - 连通分组合并：A≈B、B≈C → 同组，且链接齐全
"""
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dedup  # noqa: E402

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


class _Vault:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-dedup-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir(parents=True)

    def cfg(self):
        return {"name": "lib", "path": str(self.vault), "collection": "kb_lib",
                "exclude_dirs": set(), "exclude_files": set(),
                "exclude_patterns": ()}

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _snapshot(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (
                p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest())
    return out


_TXT = ("fluid mechanics manometer pressure gauge equation bernoulli "
        "continuity momentum energy viscous flow reynolds number laminar ")


def test_sketch_jaccard_units():
    t = dedup.tokenize(_TXT * 5)
    s = dedup.minhash_sketch(t)
    ok("unit: 自比 1.0", dedup.sketch_jaccard(s, s) == 1.0)
    t2 = dedup.tokenize(_TXT * 5)
    s2 = dedup.minhash_sketch(t2)
    ok("unit: 完全相同 1.0", dedup.sketch_jaccard(s, s2) > 0.99,
       str(dedup.sketch_jaccard(s, s2)))
    t3 = dedup.tokenize(("quantum field theory black holes entropy " * 5))
    s3 = dedup.minhash_sketch(t3)
    j = dedup.sketch_jaccard(s, s3)
    ok("unit: 异文近 0", j < 0.1, str(j))
    empty = dedup.sketch_jaccard([], [])
    ok("unit: 双空 1.0", empty == 1.0)


def test_identical_md_detected():
    v = _Vault()
    try:
        (v.vault / "a.md").write_text(_TXT * 20, encoding="utf-8")
        (v.vault / "b.md").write_text(_TXT * 20, encoding="utf-8")
        before = _snapshot(v.vault)
        clusters, stats = dedup.find_duplicates(v.cfg(), threshold=0.8)
        ok("id: 1 组", stats["groups"] == 1, str(stats))
        ok("id: 1 对", stats["pairs"] == 1, str(stats))
        ok("id: a 与 b 同组", len(clusters) == 1
           and set(clusters[0]["files"]) == {"a.md", "b.md"})
        ok("id: 链接相似度≥0.8", clusters[0]["links"][0][2] >= 0.8,
           str(clusters[0]["links"]))
        ok("id: 扫描 2", stats["scanned"] == 2, str(stats))
        ok("id: 零写入", _snapshot(v.vault) == before)
    finally:
        v.cleanup()


def test_threshold_filter_low_similarity():
    v = _Vault()
    try:
        # 两份部分相同：a 是全文，half 是前半段 → 相似度 0.5~0.6，低于 0.9
        toks = dedup.tokenize(_TXT * 20)
        half = " ".join(toks[:len(toks) // 2])
        (v.vault / "full.md").write_text(_TXT * 20, encoding="utf-8")
        (v.vault / "half.md").write_text(half * 2, encoding="utf-8")
        _, stats = dedup.find_duplicates(v.cfg(), threshold=0.9)
        ok("thr: 0.9 阈值下无重复对", stats["pairs"] == 0, str(stats))
        _, stats2 = dedup.find_duplicates(v.cfg(), threshold=0.3)
        ok("thr: 0.3 阈值下检出对", stats2["pairs"] >= 1, str(stats2))
    finally:
        v.cleanup()


def test_distinct_docs_no_group():
    v = _Vault()
    try:
        (v.vault / "a.md").write_text(_TXT * 20, encoding="utf-8")
        (v.vault / "b.md").write_text(
            ("quantum field theory black holes entropy " * 20), encoding="utf-8")
        clusters, stats = dedup.find_duplicates(v.cfg(), threshold=0.8)
        ok("dis: 无重复组", stats["groups"] == 0 and len(clusters) == 0, str(stats))
    finally:
        v.cleanup()


def test_uncached_pdf_skipped():
    v = _Vault()
    try:
        (v.vault / "a.md").write_text(_TXT * 20, encoding="utf-8")
        # 未提取的 pdf（伪造字节），应计入 skipped，不触发任何提取
        (v.vault / "doc.pdf").write_bytes(b"%PDF-1.4 fake no cache")
        clusters, stats = dedup.find_duplicates(v.cfg(), threshold=0.8)
        ok("skip: scanned=1", stats["scanned"] == 1, str(stats))
        ok("skip: skipped≥1 含 pdf", stats["skipped"] >= 1, str(stats))
        ok("skip: 无重复组", stats["groups"] == 0, str(stats))
    finally:
        v.cleanup()


def test_sketch_jaccard_bottomk_z_truncation():
    """满 k 签名的估计分子必须按并集第 k 小值 z 截断。

    反例（k=2）：A={1,3}、B={2,3}，交集 {3} > z=2 → 估计 0。旧实现直接
    |A∩B|/k = 1/2，把「在两边 bottom-k 里但大于 z」的交集元素多算了——
    对阈值附近的边界对系统性偏高，产生假阳性重复对。
    """
    ok("z 截断: k=2 边界对不虚高",
       dedup.sketch_jaccard([1, 3], [2, 3], k=2) == 0.0,
       str(dedup.sketch_jaccard([1, 3], [2, 3], k=2)))
    ok("z 截断: k=4 估计 0.75",
       dedup.sketch_jaccard([1, 2, 3, 4], [1, 2, 3, 5], k=4) == 0.75,
       str(dedup.sketch_jaccard([1, 2, 3, 4], [1, 2, 3, 5], k=4)))
    ok("z 截断: 满签自比仍 1.0",
       dedup.sketch_jaccard([1, 2, 3, 4], [1, 2, 3, 4], k=4) == 1.0)
    ok("z 截断: 单侧空 → 0（不误报）",
       dedup.sketch_jaccard([], [1, 2, 3, 4], k=4) == 0.0)


def test_connected_component_grouping():
    v = _Vault()
    try:
        # A≈B（重复），A≈C（重复），B 与 C 未必直接同一边，但应同组（连通分量）
        t = dedup.tokenize(_TXT * 20)
        (v.vault / "a.md").write_text(_TXT * 20, encoding="utf-8")
        (v.vault / "b.md").write_text(_TXT * 20, encoding="utf-8")
        (v.vault / "c.md").write_text(_TXT * 20, encoding="utf-8")
        clusters, stats = dedup.find_duplicates(v.cfg(), threshold=0.8)
        ok("cc: 1 组", stats["groups"] == 1, str(stats))
        ok("cc: 组内 3 份", len(clusters[0]["files"]) == 3, str(clusters[0]["files"]))
        ok("cc: 链接 3 对", stats["pairs"] == 3, str(stats))
    finally:
        v.cleanup()


def _run_all():
    tests = [val for key, val in sorted(globals().items())
             if key.startswith("test_") and callable(val)]
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
    print(f"\n===== Dedup: {PASS} passed, {FAIL} failed =====")
    if _FAILED:
        print("失败用例: " + ", ".join(_FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_run_all())
