"""audit_regression_test.py — 2026-08-14 审计所列问题的回归测试。

覆盖全部可在纯逻辑层验证的修复（不加载模型、不碰真实 Chroma、不需要 GPU）：
  F2  配置首跑/第二跑一致 + 模板与 DEFAULTS 同步
  F3  值里的转义引号不再打断注释剥离
  F4  配置项类型/范围校验
  F5  _pid_alive 不再用 os.kill 做 Windows 探测
  F8  围栏代码块内的 # 不再被当标题（也不再污染其后真实小节的标题路径）
  F9  裸 [[wikilink]] 保留目标词
  F10 fusion 权重真的参与 RRF
  F13 index_vault 转发 incremental/full
  F14 kb_stale 感知 META_VERSION 变化
  F16 kb_stale 对"目录存在但为空"给出 emptied 标志
  F17 split_sentences 接收每库 chunk_max
  F19 frontmatter 解析 YAML 列表
  F20 文件名/title/tags 进入待嵌入文本

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\audit_regression_test.py
（Windows 相关分支只做静态断言，见 F5：Linux 上跑不到那条路径。）
"""
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import index  # noqa: E402


# ---------- F2 / F3 / F4：配置层 ----------

def test_template_matches_defaults():
    """出厂种子（CONFIG_TEMPLATE）必须与 DEFAULTS 键值一致。

    2026-08-13 只改了 DEFAULTS（chunk 1500→600、候选池 10→50），模板没跟上，
    于是首跑用 600、第二跑起读回 1500，v5 大改被静默回退。
    本断言只约束出厂种子，不限制用户此后怎么改 config.json。
    """
    errs = config.template_consistency_errors()
    assert not errs, "模板与 DEFAULTS 不一致：\n  " + "\n  ".join(errs)


def test_first_run_equals_second_run():
    with tempfile.TemporaryDirectory() as td:
        old_path, old_dir = config.CONFIG_PATH, config.DATA_DIR
        try:
            config.DATA_DIR = Path(td)
            config.CONFIG_PATH = Path(td) / "config.json"
            first = config.load_config()          # 无文件：写模板
            second = config.load_config()         # 有文件：读模板
            diff = {k: (first[k], second[k]) for k in config.DEFAULTS
                    if first[k] != second[k]}
            assert not diff, "首跑与第二跑配置不一致：%s" % diff
            assert second["chunk_char_limit"] == 600
            assert second["rerank_candidates"] == 50
        finally:
            config.CONFIG_PATH, config.DATA_DIR = old_path, old_dir


def test_escaped_quote_does_not_corrupt_config():
    """值里出现 \\" 时，其后的 // 不得被误当注释删掉。"""
    src = '{\n  // c\n  "truncate_mark": "say \\" hi // not a comment",\n  "default_top_k": 9\n}'
    parsed = json.loads(config._strip_trailing_commas(config._strip_json_comments(src)))
    assert parsed["default_top_k"] == 9
    assert parsed["truncate_mark"] == 'say " hi // not a comment'


def test_type_validation_falls_back_per_key():
    """非法值只回退该项，合法值照常生效（不是整份配置一起丢）。"""
    with tempfile.TemporaryDirectory() as td:
        old_path, old_dir = config.CONFIG_PATH, config.DATA_DIR
        try:
            config.DATA_DIR = Path(td)
            config.CONFIG_PATH = Path(td) / "config.json"
            config.CONFIG_PATH.write_text(json.dumps({
                "chunk_char_limit": "六百",     # 类型错
                "rerank_enabled": "false",      # 字符串 "false" 是真值，必须挡住
                "exclude_dirs": "not-a-list",   # 类型错
                "default_top_k": -5,            # 范围错
                "bm25_b": 0.9,                  # 合法，应保留
            }, ensure_ascii=False), encoding="utf-8")
            cfg = config.load_config()
            assert cfg["chunk_char_limit"] == config.DEFAULTS["chunk_char_limit"]
            assert cfg["rerank_enabled"] is True
            assert cfg["exclude_dirs"] == config.DEFAULTS["exclude_dirs"]
            assert cfg["default_top_k"] == config.DEFAULTS["default_top_k"]
            assert cfg["bm25_b"] == 0.9, "合法值不该被牵连"
        finally:
            config.CONFIG_PATH, config.DATA_DIR = old_path, old_dir


def test_missing_keys_backfilled_and_idempotent():
    """老 config.json 应能获得新版本新增的键，且补写后仍可解析、不重复补。"""
    with tempfile.TemporaryDirectory() as td:
        old_path, old_dir = config.CONFIG_PATH, config.DATA_DIR
        try:
            config.DATA_DIR = Path(td)
            config.CONFIG_PATH = Path(td) / "config.json"
            config.CONFIG_PATH.write_text('{\n  // 保留我\n  "default_top_k": 7,\n}\n',
                                          encoding="utf-8")
            config.load_config()
            after = config.CONFIG_PATH.read_text(encoding="utf-8")
            assert "// 保留我" in after, "补写不得抹掉注释"
            parsed = json.loads(config._strip_trailing_commas(
                config._strip_json_comments(after)))
            assert not set(config.DEFAULTS) - set(parsed), "补写后仍有缺键"
            assert parsed["default_top_k"] == 7, "补写不得改动既有值"
            cfg2 = config.load_config()
            assert cfg2["default_top_k"] == 7
        finally:
            config.CONFIG_PATH, config.DATA_DIR = old_path, old_dir


# ---------- F5：进程存活探测 ----------

def test_pid_alive_is_not_os_kill_on_windows():
    """Windows 上 os.kill(pid, 0) 等于 TerminateProcess，绝不能用作存活探测。"""
    src = inspect.getsource(index._pid_alive)
    assert "WaitForSingleObject" in src, "Windows 分支必须走只读的句柄等待探测"
    win_branch = src.split("if _IS_WINDOWS")[1].split("try:\n        os.kill")[0]
    assert "os.kill" not in win_branch, "Windows 分支里不得出现 os.kill"
    assert index._pid_alive(os.getpid()) is True
    assert index._pid_alive(999999999) is False
    assert index._pid_alive(None) is False
    assert index._pid_alive(-1) is False


def test_singleton_reuses_safe_probe():
    import singleton
    assert singleton.pid_alive(os.getpid()) is True
    assert singleton.pid_alive(999999999) is False


# ---------- F8：围栏代码块 ----------

def test_code_fence_headings_ignored():
    doc = ("# 真标题\n正文一\n\n```python\n# 这是注释不是标题\ndef f(): pass\n"
           "## 也不是标题\n```\n\n## 真的二级标题\n正文二\n")
    secs = index.split_by_headings(doc)
    paths = [hp for hp, _ in secs]
    assert paths == ["真标题", "真标题 / 真的二级标题"], paths
    # 关键：伪标题不只是多切一节，它会成为其后真实小节的父标题，
    # 而标题路径是要拼进嵌入文本的 —— 等于把代码注释混进正文向量。
    assert not any("注释" in p for p in paths)


def test_tilde_fence_and_unclosed_fence():
    doc = "# A\n~~~\n# 围栏内\n~~~\n## B\n正文\n"
    assert [hp for hp, _ in index.split_by_headings(doc)] == ["A", "A / B"]
    unclosed = "# A\n```\n# 未闭合围栏内\n更多内容\n"
    assert [hp for hp, _ in index.split_by_headings(unclosed)] == ["A"]


# ---------- F9：wikilink ----------

def test_bare_wikilink_kept():
    cases = {
        "见 [[火箭发动机]] 一节": "见 火箭发动机 一节",
        "见 [[火箭发动机|发动机]] 一节": "见 发动机 一节",
        "![[图片.png]]": "",
        "[[folder/笔记#小节]]": "笔记",
        "[[目标#^blk1]]": "目标",
        "[[#本文锚点]]": "本文锚点",
        "[[目标\\|别名]]": "别名",
    }
    for src, want in cases.items():
        got = index.clean_wikilinks(src)
        assert got == want, f"{src!r} -> {got!r}，期望 {want!r}"


# ---------- F19：frontmatter ----------

def test_frontmatter_yaml_lists():
    fm = ("---\ntitle: 火箭发动机笔记\ntags:\n  - 航天\n  - CFD\n"
          "aliases: [发动机, 引擎]\n---\n\n正文开始\n")
    meta, body = index.extract_frontmatter(fm)
    assert meta["title"] == "火箭发动机笔记"
    assert meta["tags"] == "航天, CFD", meta
    assert meta["aliases"] == "发动机, 引擎", meta
    assert body.strip() == "正文开始"


# ---------- F17：句子切分守每库上限 ----------

def test_split_sentences_honours_explicit_max():
    para = "这是第一句话。" * 100
    assert max(len(x) for x in index.split_sentences(para, 200)) <= 200
    assert max(len(x) for x in index.split_sentences(para, 600)) <= 600
    # 单句本身超限时"宁长勿断"是既定设计，不在此改变
    assert len(index.split_sentences("无标点长句" * 100, 200)) == 1


def test_index_core_passes_chunk_max_to_sentence_split():
    src = inspect.getsource(index._index_core)
    assert "split_sentences(p, chunk_max)" in src, \
        "句子切分必须收到该库的 chunk_max，而不是回落到全局 CFG"


# ---------- F13 / F14 / F16：索引入口与指纹 ----------

def test_index_vault_forwards_flags():
    src = " ".join(inspect.getsource(index.index_vault).split())
    assert "incremental=incremental, full=full" in src, \
        "index_vault 必须把 incremental/full 传给 _index_core（否则 --full 静默降级为增量）"


def test_kb_stale_flags():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        meta_cur = td / "cur.json"
        meta_cur.write_text(json.dumps(
            {"a.md": {"size": 1, "mtime": 1, "hash": "x", "chunks": 3},
             "_version": index.META_VERSION}), encoding="utf-8")
        empty_dir = td / "emptylib"
        empty_dir.mkdir()
        live_dir = td / "livelib"
        live_dir.mkdir()
        (live_dir / "a.md").write_text("正文", encoding="utf-8")

        # 目录存在但扫不到文件 → emptied（调用方据此跳过，避免清空索引）。
        # 注意这条优先于 version_upgrade：宁可不重建，也不能把索引清空。
        stale, stats = index.kb_stale(str(empty_dir), meta_cur, "c")
        assert stale and stats.get("emptied") and not stats.get("missing"), stats

        # 路径整个消失 → missing
        stale, stats = index.kb_stale(str(td / "nope"), meta_cur, "c")
        assert stale and stats.get("missing"), stats

        # 切块版本变化 → version_upgrade（此前 kb_stale 完全不看 _version）
        meta_old = td / "old.json"
        meta_old.write_text(json.dumps(
            {"a.md": {"size": 1, "mtime": 1, "hash": "x", "chunks": 3},
             "_version": index.META_VERSION - 1}), encoding="utf-8")
        stale, stats = index.kb_stale(str(live_dir), meta_old, "c")
        assert stale and stats.get("version_upgrade"), stats


# ---------- F20：待嵌入文本包含文件级锚点 ----------

def test_embedded_text_includes_file_context():
    src = inspect.getsource(index._index_core)
    assert 'new_texts.append((ctx + "\\n" if ctx else "") + chunk_text)' in src
    assert '"ctx": ctx,' in src, "完整前缀需写入 metadata 供检索侧剥离"
    assert "Path(rel).stem" in src, "文件名应参与文件级锚点"
    assert "_ctx_for" in src, "锚点需逐段去重（文件名/title/标题常常同名）"


def test_ctx_prefix_deduplicated():
    """文件名、title、标题路径重名时不得在锚点里重复出现。

    短文档走整篇成块时 heading 直接取 title，文件名与 title 也常常相同；
    不去重会得到 "A / A笔记 / 航天 / A笔记"，白占 token，还会让该词在块内
    词频虚高、扭曲 BM25 打分。
    """
    import re as _re
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        vault = Path(td) / "v"
        vault.mkdir()
        (vault / "火箭发动机.md").write_text(
            "---\ntitle: 火箭发动机\ntags:\n  - 航天\n---\n\n正文内容\n", encoding="utf-8")
        collected = []

        real_encode = index.encode_safe

        class _Arr(list):
            def tolist(self):
                return [list(v) for v in self]

        def fake_encode(texts):
            collected.extend(texts)
            return _Arr([[0.1, 0.2, 0.3] for _ in texts])

        index.encode_safe = fake_encode
        try:
            index._index_core(str(vault), "kb_ctx_test", Path(td) / "m.json",
                              set(), set(), (), ["md"], 600, 50,
                              library_label="t", incremental=False, full=True)
        except Exception:
            pass  # 没有真实 Chroma 时写库会失败，但切块/拼文本已经跑过了
        finally:
            index.encode_safe = real_encode
        assert collected, "未收集到待嵌入文本"
        for text in collected:
            head = text.split("\n", 1)[0]
            segs = [s.strip() for s in head.split(" / ")]
            assert len(segs) == len(set(segs)), "锚点存在重复段：%r" % head
            assert "火箭发动机" in head and "航天" in head, head


# ---------- F10：融合权重 ----------

def test_fusion_weights_affect_ranking():
    import retriever
    dense_ids, dense_dists = ["a::0", "b::0"], [0.1, 0.2]
    bm25_map = {"b::0": 9.0}
    equal = retriever._rrf_combine(dense_ids, dense_dists, bm25_map,
                                   dense_w=1.0, bm25_w=1.0)
    dense_heavy = retriever._rrf_combine(dense_ids, dense_dists, bm25_map,
                                         dense_w=5.0, bm25_w=1.0)
    assert equal["b::0"] > equal["a::0"], "等权下双路命中的 b 应更高"
    assert dense_heavy["a::0"] > dense_heavy["b::0"], "重压 dense 后 a 应反超"

    sig = inspect.signature(retriever.hybrid_search)
    assert "return_top_confidence" in sig.parameters


def test_confidence_matches_display_order():
    """置信度必须与最终排序同源（重排生效时用重排分）。"""
    import retriever
    src = inspect.getsource(retriever.hybrid_search)
    assert "rr_conf" in src and "math.exp" in src, "重排生效时应以重排分派生置信度"
    assert "_top1_confidence" not in dir(retriever), "HyDE 不应再靠多跑一轮检索取置信度"


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
