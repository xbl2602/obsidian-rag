"""test_config_editor.py — config_editor.py 纯逻辑单元测试（不写真实配置文件）。

验证：定位赋值行、值类型序列化、注释保留、批量写回回滚。
运行：cd obsidian-rag && .venv\\Scripts\\python tests\\test_config_editor.py
"""
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


def test_kind_of():
    assert ce.kind_of("default_top_k") == "int"
    assert ce.kind_of("vault") == "str"
    assert ce.kind_of("fusion_dense_weight") == "float"
    assert ce.kind_of("exclude_dirs") == "list"
    assert ce.kind_of("nope") is None


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