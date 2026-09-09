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
import time
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


# ---------- 问题48（第一档）：提取噪声清洗 ----------

def test_page_number_lines_stripped():
    doc = "正文第一行\n第 12 页\n正文第二行\nPage 5\n- 13 -\n42\n结尾\n"
    got = index.strip_page_number_lines(doc)
    assert "第 12 页" not in got and "Page 5" not in got and "- 13 -" not in got
    assert "42" in got, "单个孤立裸数字行可能是正文（年份/编号），必须保留"
    assert "正文第一行" in got and "结尾" in got
    # ≥2 个互不相同的裸数字行 = 分页信号，全删
    multi = "a\n12\nb\n13\nc\n"
    got2 = index.strip_page_number_lines(multi)
    assert "12" not in got2.splitlines() and "13" not in got2.splitlines()
    # 列表序号与含数字正文不受影响
    assert "1. 试验步骤" in index.strip_page_number_lines("1. 试验步骤\n2. 记录数据\n")
    assert "2024年总结" in index.strip_page_number_lines("2024年总结\n正文\n")


def test_boilerplate_repeated_lines():
    header = "USM Aerospace Exchange Report"
    body = "\n\n".join(f"{header}\n\n第{i}章内容在此" for i in range(1, 6))
    got = index.strip_boilerplate_lines(body)
    assert header not in got, "重复 5 次的页眉必须全删"
    assert "第3章内容在此" in got
    # 只出现 2 次（目录+正文）的不删
    two = "3.2 试验结果\n\n一些内容\n\n3.2 试验结果\n\n另一些内容\n"
    assert "3.2 试验结果" in index.strip_boilerplate_lines(two)
    # 保护边界：重复标题/表格行/列表项/围栏内容/短行一律不动
    prot = ("## 结论\n\n内容一\n\n## 结论\n\n内容二\n\n## 结论\n\n内容三\n\n"
            "| 型号 | 推力 |\n| 甲 | 1 |\n\n| 型号 | 推力 |\n| 乙 | 2 |\n\n"
            "| 型号 | 推力 |\n| 丙 | 3 |\n")
    gotp = index.strip_boilerplate_lines(prot)
    assert gotp.count("## 结论") == 3, "标题行删了会连带丢掉切块标题路径"
    assert gotp.count("| 型号 | 推力 |") == 3, "表格行不动，宁可留噪声不拆表"
    lst = "- 待办事项\n\n正文\n\n- 待办事项\n\n正文\n\n- 待办事项\n\n正文\n"
    assert "- 待办事项" in index.strip_boilerplate_lines(lst)
    fence = "```\nimport os\n```\n\n文字\n\n```\nimport os\n```\n\n文字\n\n```\nimport os\n```\n"
    assert "import os" in index.strip_boilerplate_lines(fence)
    short = "结论\n\n甲\n\n结论\n\n乙\n\n结论\n\n丙\n"
    assert "结论" in index.strip_boilerplate_lines(short), "短行（<4字）不受影响"


def test_dead_image_refs_stripped():
    got = index.strip_dead_image_refs("见下图\n\n![](images/fig1.jpg)\n\n后续文字\n")
    assert "images/fig1.jpg" not in got and "见下图" in got and "后续文字" in got
    assert index.strip_dead_image_refs("![发动机结构图](images/fig1.jpg)") == "发动机结构图"
    assert index.strip_dead_image_refs("![](images/fig1.jpg)") == ""
    keep = "![logo](http://example.com/a.png)"
    assert index.strip_dead_image_refs(keep) == keep, "远端活图不动"
    assert index.strip_dead_image_refs('文字<img src="a.jpg" alt="剖面图">后续') == "文字剖面图后续"
    assert index.strip_dead_image_refs('<img src="a.jpg">') == ""


def test_cleaning_pipeline_no_cascade():
    """图链先剥：避免重复的图片路径行被误判成样板连累正文。"""
    doc = "![示意图](images/a.jpg)\n\n![示意图](images/a.jpg)\n\n![示意图](images/a.jpg)\n"
    body = index.strip_dead_image_refs(doc)
    body = index.strip_boilerplate_lines(body)
    assert "images/" not in body


# ---------- 问题48附记：MinerU sidecar 官方标注清洗（第二档治本） ----------

def test_sidecar_annotated_noise_stripped():
    """按官方块标注精确删页眉/页脚/页码（整行逐字匹配），正文/表格/标题不动。"""
    sidecar = [
        {"type": "header", "text": "USM Aerospace Exchange Report", "page_idx": 0},
        {"type": "page_number", "text": "Page 3", "page_idx": 0},
        {"type": "text", "text": "The measured thrust values are listed below.", "page_idx": 0},
        {"type": "table", "table_body": "<table>...</table>", "page_idx": 0},
    ]
    body = ("USM Aerospace Exchange Report\n\nThe measured thrust values are "
            "listed below.\n\nPage 3\n")
    got = index.strip_sidecar_noise(body, sidecar)
    assert "USM Aerospace Exchange Report" not in got
    assert "Page 3" not in got
    assert "measured thrust" in got
    # 标题行即使文本撞页眉也保留（md 结构，标题路径进切块向量）
    body2 = "# USM Aerospace Exchange Report\n\n正文\n\nUSM Aerospace Exchange Report\n"
    got2 = index.strip_sidecar_noise(body2, sidecar)
    assert got2.startswith("# USM Aerospace Exchange Report")
    assert got2.count("USM Aerospace Exchange Report") == 1, got2
    # 非 list / 空 / 无噪声类型 → 原样返回
    assert index.strip_sidecar_noise(body, None) == body
    assert index.strip_sidecar_noise(body, []) == body
    assert index.strip_sidecar_noise(body, [{"type": "text", "text": "正文"}]) == body


# ---------- 问题28：wikilink 目标抽取（双链关系图，与 clean_wikilinks 取值方向相反）----------

def test_extract_wikilink_targets():
    """extract_wikilink_targets 与 clean_wikilinks 共用同一条 [[...]] 语法规则，
    但取「目标」而非「显示文字」——两者在带别名场景下取值方向相反，容易搞反。
    """
    cases = {
        "[[机器]]": ["机器"],
        "[[机器|好机器]]": ["机器"],          # 取目标非别名，与 clean_wikilinks 相反
        "[[folder/机器#说明]]": ["机器"],      # 剥路径、剥锚点
        "![[图片.png]]": [],                  # 嵌入不计入关系
        "[[#标题]]": [],                      # 纯锚点（无目标头）不计入
        "见 [[机器]]，又见 [[机器]] 一次": ["机器"],  # 去重
        "[[机器\\|说明]]": ["机器"],           # 表格转义管道
    }
    for src, want in cases.items():
        got = index.extract_wikilink_targets(src)
        assert got == want, f"{src!r} -> {got!r}，期望 {want!r}"
    # 去重 + 排序：重复目标只算一次，多目标按 sorted() 顺序返回
    dup = "见 [[目标]]，又见一次 [[目标]]"
    assert index.extract_wikilink_targets(dup) == ["目标"]
    multi = "[[乙笔记]] 与 [[甲笔记]] 都提到 [[甲笔记]]"
    assert index.extract_wikilink_targets(multi) == sorted({"乙笔记", "甲笔记"})


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


def test_confidence_tier_semantic_anchor():
    """问题43/45（2026-09-06）：重排 sigmoid 绝对分实测挤在 0.50~0.73（噪音地板
    0.50~0.52，强命中上限 ~0.73），数字差值与语义差距非线性错位，人和 LLM 都会按
    百分比直觉误读。两层修复都必须在位：
    - 展示层附分档词（[置信度 x.xx·高相关/中相关/弱相关]），边界 0.65/warn；
    - 展示数值经 _conf_display 零点重标定：噪音地板 0.50→0.00、强命中 0.73→1.00，
      未命中不再显示 50%；单调保序；锚点外钳位 0~1。
    - 两套 GUI 配色档位与展示分档一致（高 0.85 / 弱 0.20），解析正则兼容带档位格式。
    分档/重标定只改展示文本，排序与阈值过滤不得受影响。
    **换重排/嵌入打分模型后必须重测锚点与档位边界。**"""
    import retriever
    assert retriever.CONF_TIER_STRONG == 0.65, "高相关分档线实测标定值 0.65，改前先重测分布"
    assert retriever._conf_tier(0.65, 0.55) == "高相关"
    assert retriever._conf_tier(0.61, 0.55) == "中相关"
    assert retriever._conf_tier(0.55, 0.55) == "中相关"
    assert retriever._conf_tier(0.51, 0.55) == "弱相关"
    # 零点重标定：无关归零、强命中满档、单调保序、钳位
    assert retriever._conf_display(0.50) == 0.0, "噪音地板必须归零（问题45 的核心）"
    assert retriever._conf_display(0.49) == 0.0
    assert retriever._conf_display(0.73) == 1.0, "实测强命中上限应映射为满档"
    assert retriever._conf_display(0.80) == 1.0, "锚点之上钳位 1.0"
    raws = [0.30, 0.50, 0.52, 0.55, 0.58, 0.61, 0.65, 0.70, 0.73]
    disp = [retriever._conf_display(x) for x in raws]
    assert disp == sorted(disp), "重标定必须单调保序"
    assert all(0.0 <= d <= 1.0 for d in disp)
    assert abs(retriever._conf_display(0.55) - 0.20) < 1e-9, "warn 线锚点应落在展示分 0.20"
    assert abs(retriever._conf_display(0.65) - 0.85) < 1e-9, "高相关线锚点应落在展示分 0.85"
    fmt_src = inspect.getsource(retriever._format_results)
    assert "_conf_tier" in fmt_src, "来源行置信度必须附分档词"
    assert "_conf_display" in fmt_src, "来源行数值必须经零点重标定"
    # 分档/重标定只进展示：_rrf_combine / 排序路径不得引用
    rrf_src = inspect.getsource(retriever._rrf_combine)
    assert "_conf_tier" not in rrf_src and "_conf_display" not in rrf_src
    # flet GUI：解析兼容带档位格式 + 配色档位与展示分尺度一致
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
    import gui.widgets as widgets
    assert r"\[置信度 ([\d.]+)(?:·[^\]]*)?\]" in inspect.getsource(widgets._parse_src), \
        "flet 解析正则必须兼容·分档词后缀"
    color_src = inspect.getsource(widgets._conf_color)
    assert "0.85" in color_src and "0.2" in color_src, \
        "flet 配色档位应与展示分尺度（0.85/0.20）一致"
    # guiweb 桥：解析兼容带档位格式
    import guiweb.bridge as bridge
    assert r"\[置信度 ([\d.]+)(?:·[^\]]*)?\]" in inspect.getsource(bridge), \
        "guiweb 解析正则必须兼容·分档词后缀"


def test_terminal_reason_constants_single_source_of_truth():
    """新增终态 reason 类型时，kb_stale 与 _index_core 必须共用同一组 REASON_* 常量与
    _entry_converged 谓词，禁止走回各自手写字面量字符串对比的老路——否则两侧字符串
    静默失配会导致 kb_stale 永远判定未收敛，每轮误报 stale、无谓重建。
    """
    src_stale = inspect.getsource(index.kb_stale)
    src_core = inspect.getsource(index._index_core)
    for name in ("REASON_UNREADABLE", "REASON_EXTRACT_FAILED", "REASON_EMPTY",
                 "REASON_TBD", "REASON_SCANNED"):
        assert hasattr(index, name), f"缺少常量 {name}"
    # 常量值即持久化数据格式，改值等于毁掉既有 meta 的终态判定
    assert index.REASON_UNREADABLE == "unreadable"
    assert index.REASON_EXTRACT_FAILED == "extract-failed"
    assert index.REASON_EMPTY == "empty"
    assert index.REASON_TBD == "tbd"
    assert index.REASON_SCANNED == "scanned"
    assert "_entry_converged(" in src_stale, \
        "kb_stale 的终态收敛判定必须走 _entry_converged 单点谓词"
    # kb_stale 侧不应再出现三类文本终态的裸字面量对比
    for literal in ('"unreadable"', '"empty"', '"tbd"'):
        assert f"== {literal}" not in src_stale, \
            f"kb_stale 不得再用裸字面量 {literal} 判定终态收敛，须走 REASON_* 常量"
    # _index_core 落盘终态也必须用同一组常量，不得写回裸字面量
    for literal in ('"unreadable"', '"empty"', '"tbd"', '"extract-failed"'):
        assert f"_terminal_entry(st, bhash, {literal}" not in src_core, \
            f"_index_core 不得用裸字面量 {literal} 落终态，须走 REASON_* 常量"
    assert "REASON_UNREADABLE" in src_core and "REASON_EMPTY" in src_core \
        and "REASON_TBD" in src_core, "_index_core 落盘 reason 必须引用 REASON_* 常量"
    # _backend_changed 的重试判定同样走常量
    src_bc = inspect.getsource(index._backend_changed)
    assert "REASON_SCANNED" in src_bc and "REASON_EXTRACT_FAILED" in src_bc, \
        "_backend_changed 的重试 reason 集合必须引用 REASON_* 常量"
    # 谓词语义（与被替换掉的三处手写判断严格等价）
    assert index._entry_converged({"xfail": True, "reason": "tbd"}, index.REASON_TBD)
    assert not index._entry_converged({"xfail": True, "reason": "tbd"},
                                      index.REASON_EMPTY)
    assert not index._entry_converged({"reason": "tbd"}, index.REASON_TBD)
    assert not index._entry_converged(None, index.REASON_TBD)


# ---------- 问题32：停滞宽限机制（写侧） ----------

class _ProgressIso:
    """隔离 index 进度状态：清空内存表 + 全部落盘路径重定向临时目录。

    save/restore 模块全局（测试纪律），绝不碰真实 data/index_progress.json、
    真实 device_state.json。
    """

    _PATHS = ("DATA_DIR", "PROGRESS_FILE", "LOCK_FILE", "DEVICE_STATE_FILE")

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def __enter__(self):
        self._saved_progress = index._progress
        self._saved_paths = {n: getattr(index, n) for n in self._PATHS}
        index._progress = {}
        index.DATA_DIR = self.dir
        index.PROGRESS_FILE = self.dir / "index_progress.json"
        index.LOCK_FILE = self.dir / "index.lock"
        index.DEVICE_STATE_FILE = self.dir / "device_state.json"
        return self

    def __exit__(self, *exc):
        try:
            index._heartbeat_stop.set()  # 兜底掐掉本用例可能拉起的心跳线程
        except AttributeError:
            pass
        index._progress = self._saved_progress
        for n, v in self._saved_paths.items():
            setattr(index, n, v)
        self._td.cleanup()

    def file_bytes(self):
        f = index.PROGRESS_FILE
        return f.read_bytes() if f.exists() else None


def _running_p(**kw):
    """构造判定侧输入样本（不落盘，纯 dict）。"""
    now = time.time()
    base = dict(running=True, phase="scanning", pid=4242, updated_at=now,
                last_advance_at=now - 60, files_total=9, files_done=3)
    base.update(kw)
    return base


def test_stall_grace_cleared_by_normal_update():
    """G1①：任何不带 stall_grace_s 的普通进度事件都终止宽限（一次性豁免）。"""
    with _ProgressIso():
        index.update_progress(running=True, phase="scanning")
        index.update_progress(stall_grace_s=120, message="加载中...")
        assert "stall_grace_until" in index.read_progress()
        index.update_progress(files_done=1, message="推进")
        assert "stall_grace_until" not in index.read_progress()
        assert "stall_grace_until" not in index._progress


def test_stall_grace_no_cross_task_residue():
    """G1②：新任务 progress_start 必须清掉上一任务的宽限残留。"""
    with _ProgressIso():
        index.update_progress(running=True, phase="writing", stall_grace_s=180)
        assert "stall_grace_until" in index.read_progress()
        index.progress_start("scanning", 5)
        try:
            assert "stall_grace_until" not in index.read_progress(), \
                "跨任务残留会抑制下一任务早期的停滞判定"
            # 结构双保险：progress_start 显式 pop 存在，且 pop 不在持锁状态下
            # 调 update_progress（不可重入死锁）
            src = inspect.getsource(index.progress_start)
            assert "_progress.pop(\"stall_grace_until\", None)" in src
            assert src.index("_progress.pop") < src.index("update_progress(")
            seg = src[src.index("with _progress_lock:"):src.index("update_progress(")]
            assert "update_progress" not in seg
        finally:
            index.progress_finish("done", "t")


def test_stall_grace_merge_takes_max():
    """G2/红队B4：后写更短的宽限不得反向缩短已有宽限；clamp 上限生效。"""
    with _ProgressIso():
        index.update_progress(running=True, phase="embedding")
        index.update_progress(stall_grace_s=300)
        u1 = index.read_progress()["stall_grace_until"]
        index.update_progress(stall_grace_s=60)
        u2 = index.read_progress()["stall_grace_until"]
        assert u2 >= u1 - 1e-6, "max 合并被破坏（回退攻击面）"
        index.update_progress(stall_grace_s=10 ** 6)
        u3 = index.read_progress()["stall_grace_until"]
        assert u3 - time.time() <= index.STALL_GRACE_MAX_S + 1, "clamp 失效"


def test_stall_grace_guard_running_and_pid():
    """G3/红队B2 守卫四分支：空内存不写 / 异 pid 不写且文件字节不变 /
    同 pid 写入 ≈ now+s / 残留文件+空内存 → 原样（绝不接手复活）。"""
    with _ProgressIso() as iso:
        # 分支1：无运行中任务 → 不写、不产生进度文件
        index._stall_grace(60)
        assert not index.PROGRESS_FILE.exists() and index._progress == {}
        # 分支2：pid 非本进程 → 不写且 PROGRESS_FILE 字节不变
        index.update_progress(running=True, phase="scanning")
        index.update_progress(stall_grace_s=30)
        before = iso.file_bytes()
        index._progress = {"running": True, "pid": 999999}
        index._stall_grace(60)
        assert index._progress == {"running": True, "pid": 999999}
        assert iso.file_bytes() == before
        # 分支3：同 pid 且 running → 写入 ≈ now + s
        index._progress = {"running": True, "pid": os.getpid()}
        t0 = time.time()
        index._stall_grace(60)
        g = index.read_progress()["stall_grace_until"]
        assert t0 + 59 <= g <= t0 + 61.5, g - t0
        # 分支4：强杀残留文件 + 本进程内存为空 → 原样保留，绝不复活
        stale = {"running": True, "pid": 4242, "updated_at": 1.0}
        index.PROGRESS_FILE.write_text(json.dumps(stale), encoding="utf-8")
        index._progress = {}
        index._stall_grace(60)
        assert json.loads(index.PROGRESS_FILE.read_text(encoding="utf-8")) == stale


def test_stall_grace_helper_no_reentrant_deadlock():
    """红队B5/reliability B2：助手调用 update_progress 时锁必须已释放
    （行为断言）+ 调用点位于 with 块之外（结构断言）。"""
    captured = {}
    real = index.update_progress

    def probe(**fields):
        # 若助手在持锁状态调本函数（死锁形态），acquire(False) 必然失败；
        # 正确的两段式实现里锁已释放，acquire 应成功。
        captured["lock_free"] = index._progress_lock.acquire(blocking=False)
        if captured["lock_free"]:
            index._progress_lock.release()
        return real(**fields)

    with _ProgressIso():
        index.update_progress(running=True, phase="scanning")
        index.update_progress = probe
        try:
            index._stall_grace(60)
        finally:
            index.update_progress = real
        assert captured.get("lock_free") is True, \
            "update_progress 在持锁状态被调用 → 不可重入死锁"
    # 结构断言（AST 级，免疫注释/docstring 文本）：函数体形态必须是
    # docstring → With(_progress_lock) → 守卫 If ×2 → 尾部裸调用 update_progress；
    # 且 With 块体内绝不出现 update_progress 调用。
    import ast as _ast
    tree = _ast.parse(inspect.getsource(index._stall_grace))
    assert isinstance(tree.body[0], _ast.FunctionDef)
    body = tree.body[0].body  # [docstring, With, If, If, Expr(call)]
    assert isinstance(body[1], _ast.With), "段1（锁内快照）缺失"
    for node in _ast.walk(body[1]):
        if isinstance(node, _ast.Call) and \
                getattr(node.func, "id", "") == "update_progress":
            raise AssertionError("update_progress 不得在 with _progress_lock 块内被调用")
    tail = body[-1]
    assert isinstance(tail, _ast.Expr) and isinstance(tail.value, _ast.Call) \
        and getattr(tail.value.func, "id", "") == "update_progress", \
        "update_progress 调用必须位于全部守卫之后的函数尾部（锁外）"


def test_stall_grace_type_defense_and_clamp():
    """安全 S1/S2：非数值/≤0 一律不写（fail-closed）；判侧非法值照常告警。"""
    with _ProgressIso():
        index.update_progress(running=True, phase="scanning")
        for bad in ("300", -5, 0, None, True):
            index.update_progress(stall_grace_s=bad)
            assert "stall_grace_until" not in index.read_progress(), repr(bad)
    # 判侧 fail-closed：非法 stall_grace_until 视为无宽限 → 照常停滞告警
    for junk in ("abc", None, [], {}):
        txt = index.progress_text(_running_p(stall_grace_until=junk))
        assert "进度停滞" in txt, repr(junk)


def test_stall_grace_kwarg_never_persisted():
    """红队 B5 防呆：相对秒数 kwarg 绝不落盘，JSON 里只有绝对截止时间戳。"""
    with _ProgressIso():
        index.update_progress(running=True, stall_grace_s=120)
        raw = json.loads(index.PROGRESS_FILE.read_text(encoding="utf-8"))
        assert "stall_grace_s" not in raw
        assert isinstance(raw["stall_grace_until"], float)


# ---------- 问题32：停滞宽限机制（判定侧 / C2） ----------

def test_progress_text_grace_states():
    """C2 核心：正常运行不再被误报心跳停滞——宽限内信息行（含 PID+安静秒数+
    剩余秒数，无告警字样）/ 过期恢复告警 / 心跳冻结仍 DEAD 优先。"""
    now = time.time()
    # ① 进度已停 60s（> STALL_TIMEOUT）但宽限未过期 → 信息行而非告警
    t = index.progress_text(_running_p(stall_grace_until=now + 120))
    assert "宽限剩余" in t and "已安静 60s" in t and "PID 4242" in t, t
    assert "⚠" not in t and "停滞" not in t and "疑似卡死" not in t, t
    # ② 宽限过期 → 恢复停滞告警
    t2 = index.progress_text(_running_p(stall_grace_until=now - 5))
    assert "进度停滞" in t2, t2
    # ③ 宽限内但心跳冻结 > HEARTBEAT_TIMEOUT → DEAD 先于一切豁免
    t3 = index.progress_text(_running_p(stall_grace_until=now + 300,
                                        updated_at=now - 30))
    assert "疑似卡死" in t3 and "宽限" not in t3, t3
    # ④ PID 缺失时格式化容错（红team 补充项）
    t4 = index.progress_text(_running_p(pid=None, stall_grace_until=now + 120))
    assert "PID ?" in t4, t4


def test_progress_text_converting_whitelist_without_grace_field():
    """G8 升级过渡边界：converting 白名单无条件生效、不依赖宽限字段——
    旧版本代码读新进度文件同样豁免，任意方向混跑不劣于现状。"""
    txt = index.progress_text(_running_p(phase="converting",
                                         last_advance_at=time.time() - 999))
    assert "文档转换中" in txt, txt
    assert "⚠" not in txt and "进度停滞" not in txt, txt


def test_heartbeat_tick_preserves_grace_field():
    """reliability N3：心跳整表拷贝必须原样保留宽限字段——宽限在静默窗口内
    存活全靠它（显式锁定，防未来心跳重写丢字段）。"""
    with _ProgressIso():
        index.update_progress(running=True, phase="writing", stall_grace_s=180)
        before = index.read_progress()["stall_grace_until"]
        index._heartbeat_tick()
        p = index.read_progress()
        assert abs(p["stall_grace_until"] - before) < 1e-9
        assert p.get("running") is True


# ---------- 问题32 / C3：CUDA 冷却与切换路径的宽限埋点 ----------

def _make_fake_torch():
    """构造假 torch 模块（types.ModuleType）：只实现冷却/切换路径触达的最小表面。

    index.py 对 torch 全懒加载（函数内 import），注入 sys.modules 即生效，
    全程不碰真模型/真 GPU。
    """
    import types
    m = types.ModuleType("torch")

    class _Cuda:
        @staticmethod
        def is_available():
            return False

        @staticmethod
        def empty_cache():
            pass

        @staticmethod
        def mem_get_info():
            return (8 * 1024 ** 3, 16 * 1024 ** 3)

    m.__dict__["cuda"] = _Cuda
    m.__dict__["empty"] = lambda *a, **k: None
    return m


class _LoadSentinel(Exception):
    pass


def _sentinel_load(device):
    raise _LoadSentinel(device)


def _inject_fake_torch():
    """返回还原函数：注入假 torch，finally 里调用还原。"""
    real = sys.modules.get("torch")
    sys.modules["torch"] = _make_fake_torch()
    return lambda: (
        sys.modules.__setitem__("torch", real) if real is not None
        else sys.modules.pop("torch", None))


def test_cuda_instrument_switchback_entry_structure():
    """C3-1 埋点②结构：_stall_grace 写入位于 _try_switch_back_cuda 入口，
    先于 old=_model 与 _load_model("cuda")；注释声明覆盖回滚恢复全程。"""
    src = inspect.getsource(index._try_switch_back_cuda)
    i_grace = src.index("_stall_grace(")
    i_old = src.index("old = _model")
    i_load = src.index('_load_model("cuda")')
    assert i_grace < i_old < i_load, "埋点②必须在入口、先于释放与加载"
    assert "STALL_GRACE_MODEL_LOAD" in src, "埋点②须显式用 MODEL_LOAD 常量"
    assert "回滚" in src, "须注释说明宽限覆盖「切回失败回滚 CPU 恢复」全程"


def test_cuda_instrument_fallback_tail_marker_order():
    """C3-2 埋点③结构：fallback_to_cpu 收尾标记存在且在函数尾部；
    其静默重载发生在调用方（_encode 内 fallback 之后紧跟 get_model）。"""
    src = inspect.getsource(index.fallback_to_cpu)
    assert "_stall_grace(" in src
    i_tail = src.index('log("已切换到 CPU 模式")')
    i_grace = src.index("_stall_grace(")
    assert i_tail < i_grace, "埋点③是收尾标记，必须在尾部日志之后"
    assert "STALL_GRACE_MODEL_LOAD" in src
    enc = inspect.getsource(index._encode)
    i_fb = enc.index("fallback_to_cpu(")
    i_gm = enc.index("get_model()", i_fb)
    assert i_fb < i_gm, "降级后的静默重载必须发生在调用方"


def test_cuda_instrument_get_model_branches_structure():
    """C3-3 埋点①双分支结构：cuda/cpu 两路 _stall_grace 都在缓存未命中的实际
    加载分支内、各自先于 _load_model，且不在函数入口（防每批刷新静音看门狗）。"""
    src = inspect.getsource(index.get_model)
    assert src.count("_stall_grace(") == 2, "cuda/cpu 两路各恰好一个埋点"
    assert src.count("STALL_GRACE_MODEL_LOAD") == 2
    i_entry_guard = src.index("if _model is not None:")
    i_first = src.index("_stall_grace(")
    assert i_first > i_entry_guard, "埋点①不得放函数入口（否则每批刷新=永久静音）"
    i_cg = src.index("_stall_grace(")
    i_cl = src.index('_load_model("cuda")')
    i_pg = src.index("_stall_grace(", i_cg + 1)
    i_pl = src.index('_load_model("cpu")')
    assert i_cg < i_cl and i_pg < i_pl, "两路埋点都必须先于各自的加载调用"


def test_cuda_cooldown_sequence_clamp_and_max_merge():
    """C3-4 行为：冷却期内重复「降级→切回失败」序列下宽限 clamp≤600 且
    max 合并绝不回退（fake torch 注入 + 假加载抛哨兵，try/finally 还原）。"""
    real_torch = sys.modules.get("torch")
    saved = {n: getattr(index, n) for n in
             ("_model", "_device", "_cuda_cooldown_until", "_load_model")}
    restore_torch = _inject_fake_torch()
    index._load_model = _sentinel_load
    try:
        with _ProgressIso():
            index.update_progress(running=True, phase="embedding")
            prev = None
            for _ in range(3):
                index.fallback_to_cpu("test-slow-batch")       # 埋点③
                u_fb = index.read_progress().get("stall_grace_until")
                assert u_fb is not None, "降级收尾必须写宽限"
                assert u_fb - time.time() <= index.STALL_GRACE_MAX_S + 1
                try:
                    index.get_model()                          # 埋点①cpu 重写
                except _LoadSentinel:
                    pass
                u_gm = index.read_progress()["stall_grace_until"]
                assert u_gm >= u_fb - 1e-6, "重载路径不得缩短宽限"
                index._model, index._device = object(), "cpu"
                index._try_switch_back_cuda()                  # 埋点②→假加载失败回滚
                u_sb = index.read_progress()["stall_grace_until"]
                assert u_sb >= (prev or u_sb) - 1e-6, "序列整体不得回退"
                assert u_sb - time.time() <= index.STALL_GRACE_MAX_S + 1
                prev = u_sb
    finally:
        restore_torch()
        for n, v in saved.items():
            setattr(index, n, v)


def test_cuda_instruments_zero_write_without_running_task():
    """C3-5：无运行中任务时全部 CUDA 埋点零写入（守卫短路，不产生噪音进度，
    不接手他进程记录）；_load_model 若被守卫后的主流程触达属加载行为本身，
    用哨兵异常隔离。"""
    real_torch = sys.modules.get("torch")
    saved = {n: getattr(index, n) for n in
             ("_model", "_device", "_cuda_cooldown_until", "_load_model")}
    restore_torch = _inject_fake_torch()
    index._load_model = _sentinel_load
    try:
        with _ProgressIso() as iso:
            index.fallback_to_cpu("x")            # 埋点③守卫拦截
            assert index._progress == {} and iso.file_bytes() is None
            index._model, index._device = object(), "cpu"
            index._try_switch_back_cuda()         # 埋点②守卫拦截
            assert index._progress == {}, index._progress
            try:
                index.get_model()                 # 埋点①守卫拦截（加载尝试照常）
            except _LoadSentinel:
                pass
            raw = iso.file_bytes()
            assert raw is None or "stall_grace_until" not in json.loads(raw), \
                "无运行中任务时埋点必须零写入"
    finally:
        restore_torch()
        for n, v in saved.items():
            setattr(index, n, v)


def test_switchback_rollback_survives_report_device_crash():
    """B1（2026-08-26 council diff）：成功路径收尾代码（log/_report_device）抛
    异常时，except 回滚分支引用的名字必须恒已绑定。修复前 `del old` 先于这两句，
    任一抛异常即 UnboundLocalError：①掩盖原始异常；②_model 已指向新 CUDA 模型
    但回滚中断——长驻进程模型状态损坏（红线1同构：收尾代码自己抛异常击穿容错）。
    四断言：调用不外抛 / 原始哨兵异常被折叠进冷却诊断 / _model 身份恢复为原
    CPU 模型对象 / 冷却重新武装。monkeypatch 使加载成功、仅 _report_device 抛哨兵，
    全程不碰真模型/真 GPU。"""
    class _ReportCrash(Exception):
        pass

    cpu_model = object()   # 哨兵对象：身份可比对
    new_cuda = object()

    def fake_load(device):
        return (new_cuda, "cuda")          # 加载成功，故障注入在加载之后的收尾

    def crashing_report(device, note=""):
        raise _ReportCrash("report-crash-sentinel-B1")

    saved = {n: getattr(index, n) for n in
             ("_model", "_device", "_cuda_cooldown_until",
              "_load_model", "_report_device")}
    try:
        with _ProgressIso():
            index.update_progress(running=True, phase="embedding")
            index._model, index._device = cpu_model, "cpu"
            index._cuda_cooldown_until = 0.0
            index._load_model = fake_load
            index._report_device = crashing_report
            t0 = time.time()
            index._try_switch_back_cuda()  # 修复前此处 UnboundLocalError 外泄
            assert index._model is cpu_model, \
                "回滚必须恢复原 CPU 模型对象（状态不得停在半切换态）"
            assert index._device == "cpu", "设备必须回滚为 cpu"
            assert index._cuda_cooldown_until > t0, "冷却必须重新武装"
            ds = json.loads(index.DEVICE_STATE_FILE.read_text(encoding="utf-8"))
            assert "report-crash-sentinel-B1" in ds.get("reason", ""), ds
            assert "failed_at" in ds, "折叠进冷却诊断的必须是原始异常而非解绑定错误"
    finally:
        for n, v in saved.items():
            setattr(index, n, v)


def test_waiting_lock_sequence_with_grace():
    """C3-6/埋点④接线（reliability N3）：waiting-lock → write_lock → writing
    埋点顺序 + waiting-lock/writing 快照带宽限字段 + done 终态无宽限。

    隔离：落盘路径重定向临时目录 + 假编码器（numpy 零向量）+ 真 Chroma 仅落在
    临时目录——对齐 test_extractors 的 _IsoEnv 模式，不碰真实模型/库。
    Chroma 在 Windows 上持有句柄，目录用 mkdtemp + rmtree(ignore_errors)
    收尾（同 _IsoEnv.cleanup），绝不用 TemporaryDirectory 上下文硬删。
    """
    import shutil
    import numpy as np
    calls = []
    real_update = index.update_progress
    real_encode = index.encode_safe

    def spy(**fields):
        real_update(**fields)
        calls.append(dict(index._progress))

    def fake_encode(texts, batch_size=None):
        return np.zeros((len(texts), 8), dtype="float32")

    paths = ("DATA_DIR", "CHROMA_DIR", "LOCK_FILE", "PROGRESS_FILE",
             "DEVICE_STATE_FILE")
    saved_paths = {n: getattr(index, n) for n in paths}
    saved_progress = index._progress
    td = Path(tempfile.mkdtemp(prefix="rag-audit-wl-"))
    try:
        vault = td / "v"
        vault.mkdir()
        (vault / "a.md").write_text("# 标题\n\n正文内容足够成块。\n", encoding="utf-8")
        index.DATA_DIR = td / "data"
        index.CHROMA_DIR = td / "chroma"
        index.LOCK_FILE = td / "data" / "index.lock"
        index.PROGRESS_FILE = td / "data" / "index_progress.json"
        index.DEVICE_STATE_FILE = td / "data" / "device_state.json"
        index.encode_safe = fake_encode
        index.update_progress = spy
        try:
            index._index_core(str(vault), "col_audit_wl", td / "meta.json",
                              set(), set(), (), ["md"], 600, 200,
                              library_label="t", incremental=False, full=True)
        finally:
            index.update_progress = real_update
            index.encode_safe = real_encode
            index._progress = saved_progress
            for n, v in saved_paths.items():
                setattr(index, n, v)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    phases = [c.get("phase") for c in calls]
    assert "waiting-lock" in phases and "writing" in phases, phases
    i_wl = phases.index("waiting-lock")
    i_wr = phases.index("writing")
    assert i_wl == i_wr - 1, f"waiting-lock 必须紧邻 writing 之前：{phases}"
    assert calls[i_wl].get("stall_grace_until"), "waiting-lock 相位必须带宽限"
    assert calls[i_wr].get("stall_grace_until"), "writing 相位必须带宽限"
    assert calls[-1].get("phase") == "done"
    assert "stall_grace_until" not in calls[-1], "终态永无宽限"


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
