"""test_library_summary.py — 库简介（问题60）纯逻辑单元测试。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_library_summary.py
覆盖：library.get/set_library_summary（唯一写口 + 长度/合法性校验）/
library_summary.sample_representative_chunks（假向量最远点采样）/
content_fingerprint + is_stale（内容指纹翻转判定）/ call_llm（注入 fake
urlopen，不打真实网络）/ summary_gate（MCP 硬门禁：propose/apply/过期/
一次性/错码，结构对齐 test_selection.py 的 selection_gate 测试）。
不加载模型、不碰真实 Chroma、不写真实 libraries.json / config.json。
"""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import library  # noqa: E402
import library_summary  # noqa: E402
import summary_gate  # noqa: E402

PASS = 0
FAIL = 0


def _check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s" % name)
    else:
        FAIL += 1
        print("FAIL %s %s" % (name, detail))


def make_isolated(tmp):
    library.LIBRARIES_FILE = Path(tmp) / "libraries.json"
    library.DATA_DIR = Path(tmp)
    library.CFG = {
        "vault": "", "collection_name": "obsidian_kb",
        "exclude_dirs": [], "exclude_files": [], "exclude_patterns": [],
        "chunk_char_limit": 600, "short_doc_char_limit": 200,
    }


def register(tmp, name="t"):
    v = Path(tmp) / "vault"
    v.mkdir(parents=True, exist_ok=True)
    library.save_registry([library._blank_entry(name, str(v))])
    return name


# ---------------------------------------------------------------------------
# A. library.get_library_summary / set_library_summary：唯一写口
# ---------------------------------------------------------------------------
def test_get_set_library_summary():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        register(td)
        entry = library.load_registry()[0]
        blank = library.get_library_summary(entry)
        _check("summary: 空白态默认值",
               blank == {"text": "", "source": "none", "updated_at": None,
                        "fingerprint": None, "model": None})

        library.set_library_summary("t", "这是一段测试简介", source="ai",
                                    fingerprint="fp1", model="qwen")
        s = library.get_library_summary(library.load_registry()[0])
        _check("summary: ai 写入生效", s["text"] == "这是一段测试简介" and s["source"] == "ai")
        _check("summary: fingerprint/model 一并落盘",
               s["fingerprint"] == "fp1" and s["model"] == "qwen")
        _check("summary: updated_at 有时间戳", isinstance(s["updated_at"], float))

        library.set_library_summary("t", "用户手写覆盖", source="user")
        s2 = library.get_library_summary(library.load_registry()[0])
        _check("summary: user 写入覆盖 ai", s2["text"] == "用户手写覆盖" and s2["source"] == "user")

        for bad_source in ("none", "AI", "", None, 123):
            try:
                library.set_library_summary("t", "x", source=bad_source)
                _check("summary: 拒绝非法 source %r" % (bad_source,), False, "未抛异常")
            except ValueError:
                _check("summary: 拒绝非法 source %r" % (bad_source,), True)

        try:
            library.set_library_summary("t", "x" * (library.SUMMARY_MAX_CHARS + 1), source="ai")
            _check("summary: 超长拒绝", False, "未抛异常")
        except ValueError:
            _check("summary: 超长拒绝", True)

        try:
            library.set_library_summary("不存在的库", "x", source="ai")
            _check("summary: 库不存在拒绝", False, "未抛异常")
        except ValueError:
            _check("summary: 库不存在拒绝", True)

        # 清空简介（source 仍需合法，text 可为空串）
        library.set_library_summary("t", "", source="ai")
        s3 = library.get_library_summary(library.load_registry()[0])
        _check("summary: 空串清空生效", s3["text"] == "")

        # 读侧防御：手改成非法字典/非法 source 不炸
        entry2 = library.load_registry()[0]
        entry2["summary"] = {"source": "garbage", "text": "残留文本"}
        library.save_registry([entry2])
        s4 = library.get_library_summary(library.load_registry()[0])
        _check("summary: 非法 source 读侧兜底为 ai（有文本）", s4["source"] == "ai")
        entry3 = library.load_registry()[0]
        entry3["summary"] = "不是字典"
        library.save_registry([entry3])
        s5 = library.get_library_summary(library.load_registry()[0])
        _check("summary: summary 字段非 dict 读侧兜底空白", s5["source"] == "none" and s5["text"] == "")


# ---------------------------------------------------------------------------
# B. content_fingerprint / is_stale
# ---------------------------------------------------------------------------
def test_content_fingerprint_and_stale():
    meta_v1 = {"a.md": {"hash": "h1"}, "b.md": {"hash": "h2"}}
    meta_v1_reorder = {"b.md": {"hash": "h2"}, "a.md": {"hash": "h1"}}
    meta_v2 = {"a.md": {"hash": "h1-changed"}, "b.md": {"hash": "h2"}}
    cfg = {"name": "t"}
    with patch.object(library_summary, "load_meta", return_value=meta_v1):
        fp1 = library_summary.content_fingerprint(cfg)
    with patch.object(library_summary, "load_meta", return_value=meta_v1_reorder):
        fp1b = library_summary.content_fingerprint(cfg)
    _check("fingerprint: 顺序无关，内容不变则指纹不变", fp1 == fp1b)
    with patch.object(library_summary, "load_meta", return_value=meta_v2):
        fp2 = library_summary.content_fingerprint(cfg)
    _check("fingerprint: 内容变化则指纹翻转", fp1 != fp2)

    with patch.object(library_summary, "load_meta", return_value=meta_v1):
        _check("stale: fingerprint 相符不算过时",
               not library_summary.is_stale(cfg, {"fingerprint": fp1}))
        _check("stale: fingerprint 不符即过时",
               library_summary.is_stale(cfg, {"fingerprint": "old-value"}))
    _check("stale: 无 fingerprint 记录不算过时（未生成过，非过时）",
           not library_summary.is_stale(cfg, {"fingerprint": None}))
    _check("stale: 缺 fingerprint 键同样不算过时", not library_summary.is_stale(cfg, {}))


# ---------------------------------------------------------------------------
# C. sample_representative_chunks：假向量最远点采样
# ---------------------------------------------------------------------------
class _FakeCollection:
    def __init__(self, docs, metas, embs):
        self._docs, self._metas, self._embs = docs, metas, embs

    def count(self):
        return len(self._docs)

    def get(self, include=None):
        return {"documents": self._docs, "metadatas": self._metas, "embeddings": self._embs}


def test_sample_representative_chunks():
    # 三个分得很开的簇，每簇 3 个几乎重合的点：k=3 应该每簇各选到一个代表
    # embeddings 故意用 np.array 而非 python list——真实 Chroma（1.5.x）就是返回
    # numpy 数组，`arr or []` 这类真值判断在此会直接抛
    # "truth value of an array... is ambiguous"，用 python list 测不出这个坑。
    docs, metas, embs = [], [], []
    clusters = [(0.0, 0.0), (50.0, 0.0), (0.0, 50.0)]
    for ci, (cx, cy) in enumerate(clusters):
        for j in range(3):
            docs.append(f"文本{ci}-{j}")
            metas.append({"file": f"c{ci}/f{j}.md", "heading": f"h{j}", "title": f"t{ci}"})
            embs.append([cx + j * 0.01, cy + j * 0.01])
    fake = _FakeCollection(docs, metas, np.array(embs))
    cfg = {"name": "t", "collection": "kb_t"}
    with patch.object(library_summary, "_chroma_collection", return_value=fake):
        rows = library_summary.sample_representative_chunks(cfg, k=3)
    _check("sample: 返回数量等于 k", len(rows) == 3, str(len(rows)))
    files = {r["file"].split("/")[0] for r in rows}
    _check("sample: 三个分散簇各取到一个代表（最远点采样覆盖分散区域）",
           files == {"c0", "c1", "c2"}, str(files))
    for r in rows:
        _check("sample: 行含 file/heading/title/text 字段",
               set(("file", "heading", "title", "text")) <= set(r), str(r))

    # k 大于样本数：不炸，返回全部
    small = _FakeCollection(docs[:2], metas[:2], np.array(embs[:2]))
    with patch.object(library_summary, "_chroma_collection", return_value=small):
        rows2 = library_summary.sample_representative_chunks(cfg, k=20)
    _check("sample: k>n 时返回全部且不重复", len(rows2) == 2, str(len(rows2)))

    # 空库：count()==0 直接返回 []（embeddings 空 numpy 数组同样不得触发真值判断）
    empty = _FakeCollection([], [], np.array([]))
    with patch.object(library_summary, "_chroma_collection", return_value=empty):
        rows3 = library_summary.sample_representative_chunks(cfg, k=10)
    _check("sample: 空库返回空列表", rows3 == [])

    # collection 不存在（未建索引）：异常被吞掉，返回 []
    def _boom(cfg):
        raise RuntimeError("collection 不存在")
    with patch.object(library_summary, "_chroma_collection", side_effect=_boom):
        rows4 = library_summary.sample_representative_chunks(cfg, k=10)
    _check("sample: collection 异常降级为空列表（不炸）", rows4 == [])


# ---------------------------------------------------------------------------
# D. call_llm：注入 fake urlopen，不打真实网络（同 MinerU 云端测试手法）
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_llm_fake_http():
    def fake_ok(req, timeout=30):
        fake_ok.captured = req
        return _FakeResp({"choices": [{"message": {"content": "  生成的简介文本  "}}]})

    with patch("urllib.request.urlopen", fake_ok):
        text = library_summary.call_llm("prompt", url="http://fake/v1/chat", model="m")
    _check("call_llm: 成功解析并去除首尾空白", text == "生成的简介文本", repr(text))
    _check("call_llm: 无 api_key 不带 Authorization 头",
           fake_ok.captured.get_header("Authorization") is None)

    def fake_with_key(req, timeout=30):
        fake_with_key.captured = req
        return _FakeResp({"choices": [{"message": {"content": "x"}}]})

    with patch("urllib.request.urlopen", fake_with_key):
        library_summary.call_llm("prompt", url="http://fake", model="m", api_key="sk-abc123")
    _check("call_llm: 云端 api_key 走 Authorization: Bearer",
           fake_with_key.captured.get_header("Authorization") == "Bearer sk-abc123")

    def fake_fail(req, timeout=30):
        raise OSError("连接失败（服务未启动）")

    with patch("urllib.request.urlopen", fake_fail):
        text2 = library_summary.call_llm("prompt", url="http://fake", model="m")
    _check("call_llm: 调用失败降级返回空串（不抛异常）", text2 == "")

    def fake_empty(req, timeout=30):
        return _FakeResp({"choices": [{"message": {"content": "   "}}]})

    with patch("urllib.request.urlopen", fake_empty):
        text3 = library_summary.call_llm("prompt", url="http://fake", model="m")
    _check("call_llm: 空白内容规整为空串", text3 == "")

    # 思考型模型（如 Qwen3）：thinking 阶段没走完就撞 max_tokens，content 为空、
    # reasoning_content 非空——必须原样报告为空串，绝不能把思考过程当简介返回
    # （2026-09-16 真实用户实测：LM Studio + qwen3.6-35b-a3b 复现过这个形态）
    def fake_reasoning_only(req, timeout=30):
        return _FakeResp({"choices": [{"message": {
            "content": "", "reasoning_content": "Here's a thinking process: ...(被截断)"}}]})

    with patch("urllib.request.urlopen", fake_reasoning_only):
        text4 = library_summary.call_llm("prompt", url="http://fake", model="m")
    _check("call_llm: 只有 reasoning_content 时不把思考过程当简介，返回空串",
           text4 == "", repr(text4))


# ---------------------------------------------------------------------------
# E. summary_gate：MCP 硬门禁（结构对齐 test_selection.test_selection_gate）
# ---------------------------------------------------------------------------
def test_summary_gate():
    with tempfile.TemporaryDirectory() as td:
        summary_gate.PENDING_FILE = Path(td) / "sum_pending.json"
        summary_gate.DATA_DIR = Path(td)

        # 非法提案：空文本 / 超长文本拒绝，不落盘
        for bad_text, why in (("", "空文本"), ("   ", "纯空白"),
                              ("x" * (library.SUMMARY_MAX_CHARS + 1), "超长")):
            try:
                summary_gate.make_proposal("t", bad_text)
                _check("gate: 拒绝（%s）" % why, False, "未抛异常")
            except summary_gate.GateError:
                _check("gate: 拒绝（%s）" % why, True)

        # propose：提案落盘、返回 diff + 6 位码、盘上只有哈希
        pid, code, diff = summary_gate.make_proposal("t", "AI 想写的新简介")
        _check("gate: 提案号格式", pid.startswith("sum-"))
        _check("gate: 确认码 6 位数字", len(code) == 6 and code.isdigit())
        _check("gate: diff 含新简介文本", "AI 想写的新简介" in diff)
        pend = json.loads(summary_gate.PENDING_FILE.read_text(encoding="utf-8"))
        _check("gate: 盘上只存哈希不存明文码",
               pend["code_sha256"] != code and len(pend["code_sha256"]) == 64)

        # apply：错码拒绝（可重试）
        try:
            summary_gate.consume_proposal("t", pid, "000000" if code != "000000" else "111111")
            _check("gate: 错码拒绝", False)
        except summary_gate.GateError:
            _check("gate: 错码拒绝", True)
        # apply：库名不匹配
        try:
            summary_gate.consume_proposal("其他库", pid, code)
            _check("gate: 库名不匹配拒绝", False)
        except summary_gate.GateError:
            _check("gate: 库名不匹配拒绝", True)
        # apply：正确 → 一次性消费，返回待写文本
        text = summary_gate.consume_proposal("t", pid, code)
        _check("gate: 正确码放行并返回待写文本", text == "AI 想写的新简介")
        # apply：重放拒绝
        try:
            summary_gate.consume_proposal("t", pid, code)
            _check("gate: 一次性消费（重放拒绝）", False)
        except summary_gate.GateError:
            _check("gate: 一次性消费（重放拒绝）", True)
        # apply：过期
        pid2, code2, _ = summary_gate.make_proposal("t", "另一段候选简介")
        pend2 = json.loads(summary_gate.PENDING_FILE.read_text(encoding="utf-8"))
        pend2["created"] = time.time() - summary_gate.TTL_S - 5
        summary_gate._pending_save(pend2)
        try:
            summary_gate.consume_proposal("t", pid2, code2)
            _check("gate: 过期拒绝", False)
        except summary_gate.GateError:
            _check("gate: 过期拒绝", True)


# ---------------------------------------------------------------------------
# F. 端到端语义：source=none/ai 直接写不需要门禁；source=user 才需要
# ---------------------------------------------------------------------------
def test_write_semantics_lock_only_when_user():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        register(td)
        # 初始 source=none：直接写（调用方不经过 gate，模拟 server.propose_library_summary
        # 的"非 user 直接写"分支）
        entry = library.load_registry()[0]
        _check("semantics: 初始态非 user，判定应直接写",
               library.get_library_summary(entry)["source"] != "user")
        library.set_library_summary("t", "第一次生成", source="ai")
        # 再次生成（仍非 user）：照常直接覆盖，不需要提案
        library.set_library_summary("t", "第二次生成覆盖", source="ai")
        s = library.get_library_summary(library.load_registry()[0])
        _check("semantics: 连续 ai 写入无需门禁", s["text"] == "第二次生成覆盖")

        # 用户手写后，覆盖判定应转为"需要门禁"
        library.set_library_summary("t", "用户亲自写的", source="user")
        entry2 = library.load_registry()[0]
        _check("semantics: 用户写入后判定需要门禁",
               library.get_library_summary(entry2)["source"] == "user")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global FAIL
            FAIL += 1
            import traceback
            print("FAIL %s 异常：%r" % (fn.__name__, e))
            traceback.print_exc()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
