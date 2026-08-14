"""library_registry_test.py — 多库注册表纯逻辑单元测试（不加载模型、不碰 Chroma）。

运行：cd obsidian-rag && .venv\\Scripts\\python tests\\library_registry_test.py
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library  # noqa: E402


def make_isolated(tmp):
    """把注册表/数据目录隔离到临时目录，并注入全局默认值。"""
    library.LIBRARIES_FILE = Path(tmp) / "libraries.json"
    library.DATA_DIR = Path(tmp)
    library.CFG = {
        "vault": "D:/_STOREROOM/lol/Obsidian Vault",
        "collection_name": "obsidian_kb",
        "exclude_dirs": [".obsidian", ".git"],
        "exclude_files": ["AGENTS.md"],
        "exclude_patterns": ["session-"],
        "chunk_char_limit": 1500,
        "short_doc_char_limit": 200,
    }


def test_migration_synthesizes_first_library():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        entries = library.load_registry(create=True)
        assert len(entries) == 1
        e = entries[0]
        assert e["name"] == "Obsidian Vault"
        assert e["path"] == "D:/_STOREROOM/lol/Obsidian Vault"
        assert e["collection"] == "obsidian_kb"
        cfg = library.effective_config(e)
        assert cfg["collection"] == "obsidian_kb"  # 迁移库沿用旧 collection，不派生
        assert cfg["extensions"] == ["md"]
        assert cfg["chunk_char_limit"] == 1500  # 继承全局
        assert cfg["exclude_dirs"] == [".obsidian", ".git"]
        assert library.LIBRARIES_FILE.exists()


def test_meta_rename_on_migration():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        legacy = Path(td) / "index_meta.json"
        legacy.write_text('{"x.md": {"chunks": 1}}', encoding="utf-8")
        library.load_registry(create=True)
        assert legacy.exists() is False
        renamed = library.meta_path("Obsidian Vault")
        assert renamed.exists()
        assert "x.md" in renamed.read_text(encoding="utf-8")


def test_collection_derivation():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        # 干净的 ASCII 系库名沿用历史派生结果（既有库的 collection 不能改名，
        # 否则已建索引被孤立）
        assert library.collection_for("Obsidian Vault") == "kb_obsidian_vault"
        assert library.collection_for("any", "custom_col") == "custom_col"

        # 2026-08-14（审计 F15）：旧规则把非 ASCII 全压成下划线并原样保留，
        # 结果 ①任意两个等长纯中文库名派生出同一个 collection ②名字以下划线
        # 结尾，不符合 Chroma 的 ^[a-zA-Z0-9]...[a-zA-Z0-9]$ 命名规则。
        # 新规则：这类退化名字追加库名 md5 前 8 位。
        assert library.collection_for("Rocketry 项目") == "kb_rocketry_" + \
            __import__("hashlib").md5("Rocketry 项目".encode("utf-8")).hexdigest()[:8]
        names = ["火箭笔记", "工程日志", "笔记", "日志"]
        cols = [library.collection_for(n) for n in names]
        assert len(set(cols)) == len(names), "等长中文库名仍然撞名：%s" % cols
        for c in cols:
            assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]", c), c


def test_add_and_validation():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])  # 干净注册表，避免触发迁移合成
        folder = Path(td) / "kb_alpha"
        folder.mkdir()
        e = library.add_library(str(folder))
        assert e["name"] == "kb_alpha"
        assert library.effective_config(e)["collection"] == "kb_kb_alpha"

        # 重名
        try:
            library.add_library(str(folder), name="kb_alpha")
            assert False, "应拒绝重名"
        except ValueError as ex:
            assert "已存在" in str(ex)
        # 重复路径（不同名）
        try:
            library.add_library(str(folder), name="kb_beta")
            assert False, "应拒绝重复路径"
        except ValueError as ex:
            assert "已注册" in str(ex)
        # 路径不存在
        try:
            library.add_library(str(Path(td) / "nope"), name="kb_gamma")
            assert False, "应拒绝不存在路径"
        except ValueError as ex:
            assert "不存在" in str(ex)
        # 非法库名（用存在的路径触发名校验）
        try:
            library.add_library(str(folder), name="a/b")
            assert False, "应拒绝非法字符"
        except ValueError as ex:
            assert "非法字符" in str(ex)


def test_config_set_unset_and_inherit():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])
        folder = Path(td) / "kb_alpha"
        folder.mkdir()
        library.add_library(str(folder))

        e = library.set_config("kb_alpha", "chunk_char_limit", "900")
        assert e["chunk_char_limit"] == 900
        assert library.effective_config(e)["chunk_char_limit"] == 900

        library.set_config("kb_alpha", "exclude_dirs", ".obsidian,TEMP")
        assert library.effective_config(library.load_registry()[0])["exclude_dirs"] == [".obsidian", "TEMP"]

        library.unset_config("kb_alpha", "chunk_char_limit")
        cfg = library.effective_config(library.load_registry()[0])
        assert cfg["chunk_char_limit"] == 1500  # 恢复继承全局

        try:
            library.set_config("kb_alpha", "model_name", "x")
            assert False, "应拒绝非法键"
        except ValueError as ex:
            assert "非法配置键" in str(ex)


def test_remove_keep_and_drop():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])
        folder = Path(td) / "kb_alpha"
        folder.mkdir()
        library.add_library(str(folder))
        library.remove_library("kb_alpha", drop=False)
        assert library.load_registry() == []  # 注销后注册表空
        # 重新注册同路径可恢复（collection 派生自名字，不变）
        e2 = library.add_library(str(folder))
        assert e2["name"] == "kb_alpha"
        try:
            library.remove_library("不存在", drop=True, yes=True)
            assert False, "应拒绝未知库"
        except ValueError as ex:
            assert "不存在" in str(ex)


def test_resolve_selection():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])
        for name in ("alpha", "beta", "gamma"):
            p = Path(td) / f"kb_{name}"
            p.mkdir()
            library.add_library(str(p), name=name)

        got = [e["name"] for e in library.resolve_entries()]
        assert got == ["alpha", "beta", "gamma"]
        got = [e["name"] for e in library.resolve_entries("alpha")]
        assert got == ["alpha"]
        got = [e["name"] for e in library.resolve_entries("alpha,beta")]
        assert got == ["alpha", "beta"]
        got = [e["name"] for e in library.resolve_entries("all")]
        assert got == ["alpha", "beta", "gamma"]
        got = [e["name"] for e in library.resolve_entries("", "gamma")]
        assert got == ["alpha", "beta"]
        got = [e["name"] for e in library.resolve_entries("alpha,beta", "beta")]
        assert got == ["alpha"]

        try:
            library.resolve_entries("", "alpha,beta,gamma")
            assert False, "应拒绝空集"
        except ValueError as ex:
            assert "为空" in str(ex)
        try:
            library.resolve_entries("hacker")
            assert False, "应拒绝未知库名"
        except ValueError as ex:
            assert "未知库名" in str(ex) and "alpha" in str(ex)


def test_empty_registry_message():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])
        try:
            library.resolve_entries()
            assert False, "空注册表应报错"
        except ValueError as ex:
            assert "没有已注册库" in str(ex)


def test_corrupt_registry_backed_up():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([{"name": "keep", "path": td}])
        library.LIBRARIES_FILE.write_text("{ 坏 json !!!", encoding="utf-8")
        assert library.load_registry() == []  # 损坏 → 空注册表
        assert library.LIBRARIES_FILE.with_suffix(".json.bak").exists()  # 已备份不覆盖
        library.save_registry([])
        assert "keep" not in library.LIBRARIES_FILE.read_text(encoding="utf-8")


def test_load_registry_skips_invalid_names():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([{"name": "../evil", "path": td}])
        assert library.load_registry() == []  # 手改的非法库名被跳过


def test_resolve_dedupe():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])
        for name in ("alpha", "beta"):
            p = Path(td) / f"kb_{name}"
            p.mkdir()
            library.add_library(str(p), name=name)
        got = [e["name"] for e in library.resolve_entries("alpha,alpha,beta")]
        assert got == ["alpha", "beta"]  # 重复名只处理一次


def test_collection_override_validation():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.save_registry([])
        folder = Path(td) / "kb_alpha"
        folder.mkdir()
        library.add_library(str(folder))
        for bad in ("x", "a" * 64, "bad name", "中文"):
            try:
                library.set_config("kb_alpha", "collection", bad)
                assert False, f"应拒绝非法 collection：{bad}"
            except ValueError:
                pass
        library.set_config("kb_alpha", "collection", "custom_ok")
        assert library.effective_config(library.load_registry()[0])["collection"] == "custom_ok"


def test_migration_empty_vault():
    with tempfile.TemporaryDirectory() as td:
        make_isolated(td)
        library.CFG["vault"] = ""
        assert library.load_registry(create=True) == []  # 空 vault 不合成
        assert library.LIBRARIES_FILE.exists()


def test_zip_slip_rejected():
    import importlib
    import io
    import zipfile
    imp_mod = importlib.import_module("import")
    _validate_zip_members = imp_mod._validate_zip_members
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok.md", "fine")
        zf.writestr("../evil.md", "bad")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        try:
            _validate_zip_members(zf)
            assert False, "应拒绝 ../ 条目"
        except ValueError:
            pass
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("/abs.md", "bad")
    buf2.seek(0)
    with zipfile.ZipFile(buf2) as zf:
        try:
            _validate_zip_members(zf)
            assert False, "应拒绝绝对路径条目"
        except ValueError:
            pass


def _run_all():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
