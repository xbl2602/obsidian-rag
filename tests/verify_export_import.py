"""verify_export_import.py — 导出/导入工具端到端验证（标准库，无 pytest）。

用法：python tests/verify_export_import.py
覆盖：回归（import 链/指纹/索引幂等/检索）、导出、副本导入、覆盖、交互取消、
     损坏注入（sha256 / CRC 截断）、留 3 个、归档清理、中文文件名、端到端检索对比。
隔离：导出在真库只读（预刷新保证不 stale）；导入/损坏全部在 %TEMP% 仓库副本上执行，
     真 data/ 除导出与自动清理外零改动。
"""
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
VAULT = os.environ.get("OBSIDIAN_VAULT", r"D:\_STOREROOM\lol\Obsidian Vault")
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []
REPO_FILES = ["index.py", "retriever.py", "server.py", "export.py", "import.py", "AI_GUIDE.md"]


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✔ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name} {detail}")


def run(args, cwd=None, timeout=900, input_text=None):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([PY, "-X", "utf8", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, input=input_text)


def count_chunks(data_dir):
    import chromadb
    c = chromadb.PersistentClient(path=str(data_dir / "chroma"))
    return c.get_or_create_collection("obsidian_kb", metadata={"hnsw:space": "cosine"}).count()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 20) or b""
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def latest_export():
    packs = sorted((ROOT / "data" / "export").glob("obsidian-rag-export-*.zip"),
                   key=lambda p: p.stat().st_mtime)
    assert packs, "没有找到导出包"
    return packs[-1]


def make_repo_copy():
    """%TEMP% 下建仓库副本（代码 + 空 data/），模拟接收端。"""
    tmp = Path(tempfile.mkdtemp(prefix="rag-verify-"))
    for name in REPO_FILES:
        shutil.copy2(ROOT / name, tmp / name)
    (tmp / "data").mkdir()
    return tmp


def section(t):
    print(f"\n=== {t} ===")


def main():
    # ---------- 0. 回归组（真库） ----------
    section("0. 回归：改动后现有功能不坏")
    import index, retriever, server  # noqa
    check("import index/retriever/server 无异常", True)
    check("_IS_WINDOWS 判定正确", index._IS_WINDOWS == (os.name == "nt"))

    from index import kb_stale, index_vault, write_lock
    with write_lock():
        check("write_lock 可获取/释放", True)

    stale, stats = kb_stale(VAULT)
    if stale:
        print("  索引过期，先增量刷新（可触发模型加载，等待数秒~数十秒）...")
        index_vault(VAULT, incremental=True)
        stale, stats = kb_stale(VAULT)
    check("kb_stale 不 stale（预刷新）", not stale, str(stats))
    index_vault(VAULT, incremental=True)
    stale2, _ = kb_stale(VAULT)
    check("增量 index_vault 幂等（跑后仍不 stale）", not stale2)

    r1 = retriever.hybrid_search("免费证书", top_k=3, include_body=False)
    check("hybrid_search 正常返回（真库，首查加载模型）",
          "[来源]" in r1 and "未找到" not in r1, r1[:80])

    # ---------- 1. 导出组 ----------
    section("1. 导出")
    rp = run(["export.py", "--vault", VAULT])
    check("export.py 退出码 0", rp.returncode == 0, rp.stderr[-300:])
    pack = latest_export()
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
    check("AI_GUIDE 含本次导出信息", pack.name in guide)
    zh_rel = [f["rel"] for f in manifest["vault_files"] if any(ord(c) > 127 for c in f["rel"])]
    check("vault 含中文文件名条目", len(zh_rel) > 0, str(zh_rel[:2]))

    # ---------- 2. 副本导入组 ----------
    section("2. 副本导入（模拟接收端）")
    copy = make_repo_copy()
    ip = run(["import.py", "--yes", str(pack)], cwd=copy)
    check("import.py 退出码 0", ip.returncode == 0, ip.stderr[-500:])
    check("输出含自检通过", "自检通过" in ip.stderr, ip.stderr[-300:])
    check("副本 chroma count == manifest", count_chunks(copy / "data") == manifest["chunk_count"])
    check("副本 index_meta 与原库一致",
          (copy / "data" / "index_meta.json").read_text(encoding="utf-8")
          == (ROOT / "data" / "index_meta.json").read_text(encoding="utf-8"))
    vf = [f["rel"] for f in manifest["vault_files"]]
    check("副本 vault_export 文件数一致",
          len(list((copy / "vault_export").rglob("*.md"))) == len(vf))
    check("中文文件名往返无损", all((copy / "vault_export" / Path(rel)).exists() for rel in zh_rel[:3]))
    check("包已归档", (copy / "data" / "archive" / pack.name).exists())
    check("临时区已清理", not (copy / "data" / "import_work").exists())
    check("bak 已删除", not (copy / "data" / "index_meta.json.bak").exists())
    archived_pack = copy / "data" / "archive" / pack.name  # 包已移入副本归档（接收方行为）

    # ---------- 3. 覆盖 + 交互取消 ----------
    section("3. 覆盖与交互取消")
    ip2 = run(["import.py", "--yes", str(archived_pack)], cwd=copy)
    check("二次导入 --yes 覆盖成功", ip2.returncode == 0 and "覆盖" in ip2.stderr)
    ip3 = run(["import.py", str(archived_pack)], cwd=copy)  # stdin 关闭 → EOFError → 取消
    check("非 --yes + 无 stdin → 用户取消且退出非零",
          ip3.returncode != 0 and "用户取消" in ip3.stderr)
    check("取消后目标索引未变", count_chunks(copy / "data") == manifest["chunk_count"])

    # ---------- 4. 损坏注入 ----------
    section("4. 损坏注入（sha256 / 截断）")
    bad1 = copy / "bad-sha.zip"
    with zipfile.ZipFile(archived_pack) as zf:
        names_ = zf.namelist()
        data = {n: zf.read(n) for n in names_}
    orig = data["payload.jsonl.gz"]
    data["payload.jsonl.gz"] = orig[:-1] + (b"\x00" if orig[-1] != 0 else b"\x01")
    with zipfile.ZipFile(bad1, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for n in names_:
            zf.writestr(n, data[n])
    bp = run(["import.py", "--yes", str(bad1)], cwd=copy)
    check("payload 篡改 → 中止且退出非零", bp.returncode != 0 and "损坏" in bp.stderr)
    check("篡改中止后目标索引未变", count_chunks(copy / "data") == manifest["chunk_count"])

    bad2 = copy / "bad-trunc.zip"
    raw = archived_pack.read_bytes()
    bad2.write_bytes(raw[: len(raw) // 2])
    bt = run(["import.py", "--yes", str(bad2)], cwd=copy)
    check("截断包 → 中止且退出非零", bt.returncode != 0 and "损坏" in bt.stderr.lower())
    check("截断中止后目标索引未变", count_chunks(copy / "data") == manifest["chunk_count"])

    # ---------- 5. 只留最近 3 个 ----------
    section("5. 旧导出包只留 3 个")
    export_dir = ROOT / "data" / "export"
    for i in range(5):
        fake = export_dir / f"obsidian-rag-export-fake{i}.zip"
        fake.write_bytes(b"not-a-zip")
    rp2 = run(["export.py", "--vault", VAULT])
    check("fake 干扰下 export 仍成功", rp2.returncode == 0, rp2.stderr[-300:])
    remaining = list(export_dir.glob("obsidian-rag-export-*.zip"))
    check(f"导出目录包数 ≤3（实得 {len(remaining)}）", len(remaining) <= 3)

    # ---------- 6. 端到端检索对比（真库 vs 副本） ----------
    section("6. 端到端检索对比")
    query = "免费证书"
    r_true = run(["-c", f"from retriever import hybrid_search; print(hybrid_search({query!r}, top_k=3, include_body=False))"],
                 cwd=ROOT)
    r_copy = run(["-c", f"from retriever import hybrid_search; print(hybrid_search({query!r}, top_k=3, include_body=False))"],
                 cwd=copy)
    check("真库检索可运行", r_true.returncode == 0, r_true.stderr[-300:])
    check("副本检索可运行", r_copy.returncode == 0, r_copy.stderr[-300:])
    check("两库 top-3 结果完全一致", r_true.stdout == r_copy.stdout,
          f"\n真库: {r_true.stdout[:200]}\n副本: {r_copy.stdout[:200]}")

    shutil.rmtree(copy, ignore_errors=True)
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
