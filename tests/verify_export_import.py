"""verify_export_import.py — 导出/导入工具端到端验证（标准库，无 pytest）。

用法：python tests/verify_export_import.py [--vault <路径>]
覆盖：回归（import 链/指纹/索引幂等/检索）、导出、副本导入、覆盖、交互取消、
     损坏注入（sha256 / CRC 截断）、留 3 个、归档清理、中文文件名、两库向量一致。
隔离：默认连 tests/hidden-vault（固定隐藏测试库，平时不可见、不进注册表），
     全部落盘（chroma/meta/导出包/缓存/vault_export）重定向到 %TEMP%，真 data/
     与真 Vault 零触碰。--vault 可显式指定真实库做里程碑前手动演练。
性能：真模型只加载 1 次（0 节索引 embedding + 1 次真检索，进程内复用不重复
     加载）；导出/导入/损坏/留3个全部进程内直接调函数，零子进程；
     6 节两库对比走向量余弦（免模型）。
"""
import argparse
import contextlib
import gzip
import hashlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIDDEN_VAULT = ROOT / "tests" / "hidden-vault"
LIB_NAME = "hidden-test"
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []
QUERY = "免费证书"


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✔ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name} {detail}")


def section(t):
    print(f"\n=== {t} ===")


def count_chunks(chroma_dir, collection):
    import chromadb
    c = chromadb.PersistentClient(path=str(chroma_dir))
    return c.get_or_create_collection(
        collection, metadata={"hnsw:space": "cosine"}).count()


def get_vectors(chroma_dir, collection, ids):
    import chromadb
    import numpy as np
    c = chromadb.PersistentClient(path=str(chroma_dir))
    col = c.get_or_create_collection(
        collection, metadata={"hnsw:space": "cosine"})
    got = col.get(ids=ids, include=["embeddings"])
    emb = got["embeddings"]
    if emb is None:
        return got["ids"], []
    vecs = np.asarray(emb).tolist()
    return got["ids"], vecs


def run_import_main(argv, stdin_text=""):
    """进程内跑 import.py main：打 argv 补丁 + 抓 stderr + 接 SystemExit。返回 (code, err)。

    注意：绝不 reload(import)——reload 会重执行模块顶层，把 IsoDirs 已打到
    临时区的落盘常量（IMPORT_WORK_DIR/VAULT_EXPORT_DIR/ARCHIVE_DIR 等）重置
    回真实路径，导致真目录被污染。IsoDirs.apply() 已保证常量指向临时区。
    """
    imp = importlib.import_module("import")
    old_argv, old_stdin = sys.argv, sys.stdin
    buf = io.StringIO()
    sys.argv = ["import.py", *argv]
    sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stderr(buf):
            try:
                imp.main()
            except SystemExit as e:
                return (e.code or 0), buf.getvalue()
            return 0, buf.getvalue()
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin


class IsoDirs:
    """一整套隔离落盘：data/chroma/导出/缓存/vault_export/归档/注册表。"""

    def __init__(self, tag):
        self.root = Path(tempfile.mkdtemp(prefix=f"rag-verify-{tag}-"))
        self.data = self.root / "data"
        self.chroma = self.data / "chroma"
        self.export_dir = self.data / "export"
        self.cache = self.data / "extract_cache"
        self.vault_export = self.root / "vault_export"
        self.archive = self.data / "archive"
        self.work = self.data / "import_work"
        for d in (self.data, self.chroma, self.export_dir, self.cache,
                  self.vault_export, self.archive):
            d.mkdir(parents=True, exist_ok=True)
        self._saved = {}

    def apply(self):
        import index
        import library
        import export as exp
        imp = importlib.import_module("import")
        import extractors as ex
        import retriever
        S = self._saved
        S["index"] = {n: getattr(index, n) for n in
                      ("DATA_DIR", "CHROMA_DIR", "LOCK_FILE",
                       "PROGRESS_FILE", "DEVICE_STATE_FILE", "INDEX_META")}
        index.DATA_DIR = self.data
        index.CHROMA_DIR = self.chroma
        index.LOCK_FILE = self.data / "index.lock"
        index.PROGRESS_FILE = self.data / "index_progress.json"
        index.DEVICE_STATE_FILE = self.data / "device_state.json"
        index.INDEX_META = self.data / "index_meta.json"
        S["library"] = (library.LIBRARIES_FILE, library.DATA_DIR)
        library.LIBRARIES_FILE = self.data / "libraries.json"
        library.DATA_DIR = self.data
        S["export"] = (exp.CHROMA_DIR, exp.DATA_DIR, exp.EXPORT_DIR)
        exp.CHROMA_DIR = self.chroma
        exp.DATA_DIR = self.data
        exp.EXPORT_DIR = self.export_dir
        S["import"] = (imp.CHROMA_DIR, imp.DATA_DIR, imp.IMPORT_WORK_DIR,
                       imp.VAULT_EXPORT_DIR, imp.ARCHIVE_DIR)
        imp.CHROMA_DIR = self.chroma
        imp.DATA_DIR = self.data
        imp.IMPORT_WORK_DIR = self.work
        imp.VAULT_EXPORT_DIR = self.vault_export
        imp.ARCHIVE_DIR = self.archive
        ex.set_cache_dir(self.cache)
        # retriever.CHROMA_DIR 是 import 期从 index 绑定的值，打 index 补丁
        # 够不到它——必须同步改，否则单例会连真实库并建空 collection 污染真库。
        S["retriever_chroma"] = retriever.CHROMA_DIR
        retriever.CHROMA_DIR = self.chroma
        retriever._chroma_client = None

    def unapply(self):
        import index
        import library
        import export as exp
        imp = importlib.import_module("import")
        import extractors as ex
        import retriever
        S = self._saved
        if "index" in S:
            for n, v in S["index"].items():
                setattr(index, n, v)
        if "library" in S:
            library.LIBRARIES_FILE, library.DATA_DIR = S["library"]
        if "export" in S:
            exp.CHROMA_DIR, exp.DATA_DIR, exp.EXPORT_DIR = S["export"]
        if "import" in S:
            (imp.CHROMA_DIR, imp.DATA_DIR, imp.IMPORT_WORK_DIR,
             imp.VAULT_EXPORT_DIR, imp.ARCHIVE_DIR) = S["import"]
        ex.set_cache_dir(None)
        retriever.CHROMA_DIR = S["retriever_chroma"]
        retriever._chroma_client = None

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(HIDDEN_VAULT))
    args = ap.parse_args(argv)
    vault = str(Path(args.vault))
    if not Path(vault).is_dir():
        sys.path.insert(0, str(ROOT / "tests"))
        from make_hidden_vault import main as _gen
        _gen()
    assert Path(vault).is_dir(), f"库路径不存在：{vault}"

    import gpu_arbiter
    _real_ensure = gpu_arbiter.ensure_server
    gpu_arbiter.ensure_server = lambda log=None: (True, "ok(test-fake)")

    # 提取后端钉死确定性组合（导出/导入测的是搬运完整性，不是 OCR 后端——
    # 后端行为由 test_extractors 覆盖）。否则跟着用户本机 config 走云端/
    # 本地服务，又慢又不可复现。
    import config as _cfg
    _pin_keys = ("pdf_scan_backend", "pdf_text_backend")
    _pin_saved = {k: _cfg.CFG.get(k) for k in _pin_keys}
    _cfg.CFG["pdf_scan_backend"] = "none"
    _cfg.CFG["pdf_text_backend"] = "local"
    # 模型走本地缓存，跳过 huggingface 在线检查（更快更稳；换模型名需清缓存）。
    _offline_saved = {k: os.environ.get(k) for k in
                      ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    iso = IsoDirs("send")
    iso.apply()
    try:
        return _main_inner(vault, iso)
    finally:
        try:
            from index import release_model
            from retriever import release_reranker
            release_reranker()
            release_model()
        except Exception:
            pass
        for k, v in _pin_saved.items():
            if v is None:
                _cfg.CFG.pop(k, None)
            else:
                _cfg.CFG[k] = v
        for k, v in _offline_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        gpu_arbiter.ensure_server = _real_ensure
        iso.unapply()
        iso.cleanup()


def _main_inner(vault, iso):
    import library
    from library import effective_config, meta_path, save_registry
    # ---------- 0. 回归组（隔离隐藏库，真模型仅此 1 次加载） ----------
    section("0. 回归：改动后现有功能不坏（隔离隐藏库）")
    import index, retriever, server  # noqa
    check("import index/retriever/server 无异常", True)
    check("_IS_WINDOWS 判定正确", index._IS_WINDOWS == (os.name == "nt"))

    entry = {"name": LIB_NAME, "path": vault}
    save_registry([entry])
    cfg = effective_config(entry)
    check("隐藏库 collection 合法",
          cfg["collection"].replace("_", "a").replace("-", "a").replace(".", "a").isalnum(),
          cfg["collection"])

    from index import kb_stale, index_library, write_lock
    with write_lock():
        check("write_lock 可获取/释放", True)

    mp = meta_path(LIB_NAME)
    stale, stats = kb_stale(vault, mp, cfg["collection"], cfg["exclude_dirs"],
                            cfg["exclude_files"], cfg["exclude_patterns"],
                            cfg["extensions"])
    if stale:
        print(f"  隐藏库索引过期，先增量刷新 {stats}（本轮唯一一次真模型加载）...")
        index_library(cfg, incremental=True, wemm_sync=False)
        stale, stats = kb_stale(vault, mp, cfg["collection"], cfg["exclude_dirs"],
                                cfg["exclude_files"], cfg["exclude_patterns"],
                                cfg["extensions"])
    check("kb_stale 不 stale（预刷新）", not stale, str(stats))
    index_library(cfg, incremental=True, wemm_sync=False)
    stale2, _ = kb_stale(vault, mp, cfg["collection"], cfg["exclude_dirs"],
                         cfg["exclude_files"], cfg["exclude_patterns"],
                         cfg["extensions"])
    check("增量 index_library 幂等（跑后仍不 stale）", not stale2)

    r1 = retriever.hybrid_search(QUERY, top_k=3, include_body=False,
                                 libraries=LIB_NAME)
    check("hybrid_search 真检索命中隐藏库（本轮唯一一次真检索）",
          "[来源]" in r1 and "未找到" not in r1, r1[:80])

    # ---------- 1. 导出组（进程内，无子进程） ----------
    section("1. 导出")
    import export as exp
    pack = exp.export_library(cfg)
    check("export_library 成功回包", pack is not None and Path(pack).exists(),
          str(pack))
    if pack is None or not Path(pack).exists():
        print(f"\n结果：{len(PASS)} 通过 / {len(FAIL) + 1} 失败（导出失败，中止）")
        sys.exit(1)
    with zipfile.ZipFile(pack) as zf:
        names = set(zf.namelist())
    for member in ["payload.jsonl.gz", "index_meta.json", "manifest.json", "AI_GUIDE.md"]:
        check(f"包内含 {member}", member in names)
    vault_members = [n for n in names if n.startswith("vault/") and not n.endswith("/")]
    check(f"包内含源文件（{len(vault_members)} 个）", len(vault_members) > 0)

    with zipfile.ZipFile(pack) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        payload = zf.read("payload.jsonl.gz")
    check("manifest 字段齐全", all(k in manifest for k in
          ["chunk_count", "vault_file_count", "sha256", "vault_files", "exported_at"]))
    check("payload sha256 与 manifest 一致",
          hashlib.sha256(payload).hexdigest() == manifest["sha256"]["payload"])
    check("chunk_count == payload 行数",
          manifest["chunk_count"] == gzip.decompress(payload).count(b"\n"))
    with zipfile.ZipFile(pack) as zf:
        guide = zf.read("AI_GUIDE.md").decode("utf-8")
    check("AI_GUIDE 占位符无残留", "__" not in guide.replace("__import__", ""))
    check("AI_GUIDE 含本次导出信息", Path(pack).name in guide)
    zh_rel = [f["rel"] for f in manifest["vault_files"] if any(ord(c) > 127 for c in f["rel"])]
    check("vault 含中文文件名条目", len(zh_rel) > 0, str(zh_rel[:2]))

    # ---------- 2. 副本导入组（第二套隔离区 = 模拟接收端） ----------
    section("2. 副本导入（模拟接收端，第二隔离区）")
    iso2 = IsoDirs("recv")
    # 接收端注册表预置同名空库（对齐旧行为：migrate 合成的首个库恰好同名）
    iso2.apply()
    try:
        save_registry([{"name": manifest.get("library", LIB_NAME),
                        "path": str(iso2.root / "recv_vault")}])
        Path(iso2.root, "recv_vault").mkdir(exist_ok=True)
        code, err = run_import_main(["--yes", str(pack)])
        check("import.py 退出码 0", code == 0, err[-500:])
        check("输出含自检通过", "自检通过" in err, err[-300:])
        recv_col = manifest.get("collection", cfg["collection"])
        check("副本 chroma count == manifest",
              count_chunks(iso2.chroma, recv_col) == manifest["chunk_count"])
        check("副本 index_meta 与原库一致",
              (iso2.data / f"index_meta_{manifest.get('library', LIB_NAME)}.json")
              .read_text(encoding="utf-8") == mp.read_text(encoding="utf-8"))
        vf = [f["rel"] for f in manifest["vault_files"]]
        lib_vault = iso2.vault_export / manifest.get("library", LIB_NAME)
        disk = {p.relative_to(lib_vault).as_posix()
                for p in lib_vault.rglob("*") if p.is_file()}
        check("副本 vault_export 文件清单一致", disk == set(vf),
              f"磁盘独有 {sorted(disk - set(vf))[:3]} 清单独有 {sorted(set(vf) - disk)[:3]}")
        check("中文文件名往返无损", all((lib_vault / Path(rel)).exists() for rel in zh_rel[:3]))
        archived_pack = iso2.archive / Path(pack).name
        check("包已归档", archived_pack.exists())
        check("临时区已清理", not iso2.work.exists())
        check("bak 已删除",
              not (iso2.data / f"index_meta_{manifest.get('library', LIB_NAME)}.json.bak").exists())

        # ---------- 3. 覆盖 + 交互取消 ----------
        section("3. 覆盖与交互取消")
        code2, err2 = run_import_main(["--yes", str(archived_pack)])
        check("二次导入 --yes 覆盖成功", code2 == 0 and "覆盖" in err2, err2[-300:])
        code3, err3 = run_import_main([str(archived_pack)], stdin_text="")
        check("非 --yes + 无 stdin → 用户取消且退出非零",
              code3 != 0 and "用户取消" in err3, err3[-300:])
        check("取消后目标索引未变",
              count_chunks(iso2.chroma, recv_col) == manifest["chunk_count"])

        # ---------- 4. 损坏注入 ----------
        section("4. 损坏注入（sha256 / 截断）")
        bad1 = iso2.root / "bad-sha.zip"
        with zipfile.ZipFile(archived_pack) as zf:
            names_ = zf.namelist()
            data = {n: zf.read(n) for n in names_}
        orig = data["payload.jsonl.gz"]
        data["payload.jsonl.gz"] = orig[:-1] + (b"\x00" if orig[-1] != 0 else b"\x01")
        with zipfile.ZipFile(bad1, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for n in names_:
                zf.writestr(n, data[n])
        bcode, berr = run_import_main(["--yes", str(bad1)])
        check("payload 篡改 → 中止且退出非零", bcode != 0 and "损坏" in berr, berr[-300:])
        check("篡改中止后目标索引未变",
              count_chunks(iso2.chroma, recv_col) == manifest["chunk_count"])

        bad2 = iso2.root / "bad-trunc.zip"
        raw = archived_pack.read_bytes()
        bad2.write_bytes(raw[: len(raw) // 2])
        tcode, terr = run_import_main(["--yes", str(bad2)])
        check("截断包 → 中止且退出非零", tcode != 0 and "损坏" in terr.lower(), terr[-300:])
        check("截断中止后目标索引未变",
              count_chunks(iso2.chroma, recv_col) == manifest["chunk_count"])

        # ---------- 6. 两库向量一致（免模型：抽查余弦） ----------
        section("6. 两库向量一致（免模型抽查）")
        imp = importlib.import_module("import")
        rows = [json.loads(l) for l in
                gzip.decompress(payload).decode("utf-8").splitlines()][:3]
        ids = [r["id"] for r in rows]
        _, svecs = get_vectors(iso.chroma, cfg["collection"], ids)
        _, rvecs = get_vectors(iso2.chroma, recv_col, ids)
        expect = [(r["id"], r["embedding"]) for r in rows]
        cos_ok = (len(svecs) == len(rows) and len(rvecs) == len(rows) and all(
            imp.emb_consistent(g, e) for (_, e), g in zip(expect, list(rvecs))))
        check("两库抽查向量余弦一致", bool(cos_ok),
              f"send={len(svecs)} recv={len(rvecs)}")
    finally:
        iso2.unapply()
        iso2.cleanup()

    # ---------- 5. 只留最近 3 个（发送区导出目录，隔离） ----------
    section("5. 旧导出包只留 3 个")
    for i in range(5):
        (iso.export_dir / f"obsidian-rag-export-fake{i}.zip").write_bytes(b"not-a-zip")
    pack2 = exp.export_library(cfg)
    exp.prune_old_exports()
    check("fake 干扰下 export 仍成功", pack2 is not None and Path(pack2).exists())
    remaining = list(iso.export_dir.glob("obsidian-rag-export-*.zip"))
    check(f"导出目录包数 ≤3（实得 {len(remaining)}）", len(remaining) <= 3)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
