"""test_config_editor.py — config_editor.py 纯逻辑单元测试（不写真实配置文件）。

验证：定位赋值行、值类型序列化、注释保留、批量写回回滚。
运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_config_editor.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui"))

import config_editor as ce  # noqa: E402

SAMPLE = """// 注释行（含 "vault": 字样也不该被误改）
{
  // 知识库
  "vault": "D:\\\\_STOREROOM\\\\lol\\\\Obsidian Vault",
  "exclude_dirs": [".obsidian", "TEMP"],
  "default_top_k": 5,
  "fusion_dense_weight": 0.6,
  "bm25_k1": 1.5,
  "truncate_mark": "… [截断]",   // 行尾注释
}
"""


def test_replace_value_keeps_comments():
    out = ce._replace_value(SAMPLE, "default_top_k", "8")
    assert "// 注释行（含 \"vault\": 字样也不该被误改）" in out
    assert '"vault": "D:\\\\_STOREROOM\\\\lol\\\\Obsidian Vault"' in out
    assert '"default_top_k": 8' in out
    assert "// 行尾注释" in out


def test_replace_value_not_matching_key():
    out = ce._replace_value(SAMPLE, "no_such_key", "1")
    assert out is SAMPLE


def test_value_to_json_types():
    assert ce._value_to_json("BAAI/bge-m3", "str") == '"BAAI/bge-m3"'
    assert ce._value_to_json("1500", "int") == "1500"
    assert ce._value_to_json("1.5", "float") == "1.5"
    assert ce._value_to_json(".obsidian, TEMP", "list") == '["_obsidian", "TEMP"]'.replace("_", ".")
    assert ce._value_to_json("  , ", "list") == "[]"
    try:
        ce._value_to_json("abc", "int")
        raise AssertionError("int 校验应抛错")
    except ValueError:
        pass


def test_apply_updates_all_or_nothing():
    text = ce.load_raw()
    if not text:
        pass  # 无配置文件时跳过（测试全程只读）
    from unittest.mock import patch
    with patch.object(ce, "load_raw", return_value=SAMPLE), \
         patch.object(ce, "CONFIG_PATH", Path(tempfile.gettempdir()) / "never_write.json"):
        errs = ce.apply_updates({
            "default_top_k": ("int", "8"),
            "bogus_key": ("int", "1"),     # 故意失败 → 整体回滚
        })
        assert "bogus_key" in errs
        # 全量失败时不应写入真实文件（patch 了路径，最坏也只是写进 /tmp）
        assert not (Path(tempfile.gettempdir()) / "never_write.json").exists()


def test_groups_cover_all_defaults():
    """GROUPS 应覆盖 config.py DEFAULTS 的全部键（遗漏即设置页不可改）。"""
    from config import DEFAULTS
    missing = set(DEFAULTS) - set(ce.ALL_KEYS)
    assert not missing, "设置页遗漏配置项: %s" % missing


def test_groups_structure_valid():
    """GROUPS 升级为 dict 后的结构契约（2026-08-26 问题31）：
    必备键齐全、level 只取两值、常用组排在开发者组之前、键不跨组重复。
    """
    seen = set()
    levels = []
    for g in ce.GROUPS:
        for required in ("title", "level", "icon", "desc", "fields"):
            assert required in g, "分组缺字段 %s: %r" % (required, g.get("title"))
        assert g["level"] in ("basic", "advanced"), \
            "level 非法: %r" % g["level"]
        levels.append(g["level"])
        for key, kind in g["fields"]:
            assert key not in seen, "配置项重复分组: %s" % key
            assert kind in ("str", "int", "float", "bool", "list"), \
                "kind 非法: %s %s" % (key, kind)
            seen.add(key)
    assert levels.index("advanced") == len(levels) - sum(
        1 for x in levels if x == "advanced"), \
        "常用组必须整体排在开发者组之前: %r" % levels
    assert "basic" in levels and "advanced" in levels, "两级分组缺一"


def test_field_meta_complete():
    """每个暴露的配置项都要有中文标签与说明；FIELD_META 不得含幽灵键。

    这是"用户咋知道有什么模型和如何选"问题的兜底：新键漏写 meta 时
    GUI 回退显示裸 key 且无提示——静态断言让它在测试期就红。
    """
    for key in ce.ALL_KEYS:
        meta = ce.FIELD_META.get(key)
        assert meta is not None, "FIELD_META 缺少键: %s" % key
        assert isinstance(meta.get("label"), str) and meta["label"], \
            "%s 缺中文标签" % key
        assert isinstance(meta.get("hint"), str) and meta["hint"], \
            "%s 缺一句话说明" % key
    ghost = set(ce.FIELD_META) - set(ce.ALL_KEYS)
    assert not ghost, "FIELD_META 含设置页没有的键: %s" % ghost


def test_choice_fields_match_defaults():
    """枚举字段的选项集必须包含出厂默认值；云端选项必须存在且拼写正确。

    下拉渲染的是 choices 的 key——若 DEFAULTS 值不在其中，GUI 打开时
    会回退成"当前配置值"假选项甚至取首个选项，保存即静默改写配置。
    """
    scan = dict(ce.FIELD_META["pdf_scan_backend"]["choices"])
    text = dict(ce.FIELD_META["pdf_text_backend"]["choices"])
    from config import DEFAULTS
    assert DEFAULTS["pdf_scan_backend"] in scan, "scan 默认值不在选项集"
    assert DEFAULTS["pdf_text_backend"] in text, "text 默认值不在选项集"
    assert scan.get("mineru-cloud"), "扫描件云端 OCR 选项缺失"
    assert text.get("mineru-cloud"), "文字层云端选项缺失"


def test_structural_keys_marked_rebuild():
    """结构类配置必须标 rebuild=True（GUI 据此打 ⟳ 提醒需全量重建）。"""
    for key in ("vault", "model_name", "collection_name",
                "chunk_char_limit", "short_doc_char_limit",
                "exclude_dirs", "exclude_files", "exclude_patterns",
                "tbd_exclude_ratio"):
        assert ce.FIELD_META[key].get("rebuild") is True, \
            "%s 应标记 rebuild=True" % key


def test_kind_of():
    assert ce.kind_of("default_top_k") == "int"
    assert ce.kind_of("vault") == "str"
    assert ce.kind_of("fusion_dense_weight") == "float"
    assert ce.kind_of("exclude_dirs") == "list"
    assert ce.kind_of("nope") is None


def test_replace_value_preserves_comma_and_inline_comment():
    """行尾逗号与行内 // 注释必须原样保留。

    2026-09-12 修：旧实现按整行 rstrip 判断逗号，行尾有 // 注释时 rstrip 落在
    注释文字上 → 误判"无逗号" → 连逗号一起吞掉，JSON 断裂、整个 config.json
    静默回退默认值（用户实测：真实设置全被忽略）。
    """
    out = ce._replace_value(
        '  "confidence_warn_threshold": 0.3,  // 低置信标注\n',
        "confidence_warn_threshold", "0.4")
    assert '"confidence_warn_threshold": 0.4,' in out
    assert "// 低置信标注" in out, "行内注释必须保留"
    # URL 字符串值（含 //）不得被误当注释破坏
    out = ce._replace_value(
        '  "hyde_llm_url": "http://localhost:1234/v1/chat/completions",\n',
        "hyde_llm_url", '"http://x/y"')
    assert out.rstrip("\n").endswith('"http://x/y",'), out
    # 数组值（内部含逗号）整体替换、尾逗号保留
    out = ce._replace_value(
        '  "exclude_dirs": [".obsidian", "TEMP"],\n', "exclude_dirs", '["a", "b"]')
    assert '"exclude_dirs": ["a", "b"],' in out


def test_replace_value_keeps_config_valid():
    """对真实模板做几轮替换后仍能解析（注释/尾逗号容错后）——防再写出坏配置。"""
    from config import (CONFIG_TEMPLATE, _strip_json_comments,
                        _strip_trailing_commas)
    text = CONFIG_TEMPLATE
    text = ce._replace_value(text, "confidence_drop_threshold", "0.1")
    text = ce._replace_value(text, "exclude_dirs", '["x"]')
    text = ce._replace_value(text, "hyde_llm_url", '"http://h/i"')
    parsed = json.loads(_strip_trailing_commas(_strip_json_comments(text)))
    assert parsed["confidence_drop_threshold"] == 0.1
    assert parsed["exclude_dirs"] == ["x"]
    assert parsed["hyde_llm_url"] == "http://h/i"


def test_apply_updates_rejects_invalid_json():
    """替换若产出坏 JSON，写盘前必须中止且不改动原文件。"""
    from unittest.mock import patch
    broken = '{\n  "default_top_k": 5\n  "exclude_files": ["x"],\n}\n'
    target = Path(tempfile.gettempdir()) / "never_write2.json"
    with patch.object(ce, "load_raw", return_value=broken), \
         patch.object(ce, "CONFIG_PATH", target):
        errs = ce.apply_updates({"default_top_k": ("int", "8")})
        assert "__file__" in errs, "坏 JSON 必须被拒绝：%r" % errs
        assert not target.exists()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS  %s" % name)
            except AssertionError as e:
                failures += 1
                print("FAIL  %s: %s" % (name, e))
    print("\n%d failures" % failures)
    sys.exit(1 if failures else 0)