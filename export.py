"""export.py — 导出全部 RAG 数据为单个 zip 包（可移植格式，接收端秒级恢复，不重算嵌入）。

用法：python export.py [--vault 路径]
输出：data/export/obsidian-rag-export-YYYYMMDD-HHMMSS.zip，内含：
  - payload.jsonl.gz  全部切块 {id, text, embedding, meta}（JSON + gzip）
  - vault/            源笔记文件（相对路径，与索引同一套过滤规则）
  - index_meta.json   文件指纹（接收端恢复后自动同步不误判）
  - manifest.json     完整性清单（payload/源文件逐个 sha256）
  - AI_GUIDE.md       AI 自动执行引导（已注入本次导出信息）
接收端：python import.py 包.zip

设计约束：
- 全程不加载 embedding 模型（除非检测到索引过期需自动刷新）
- 读取与刷新均持 index.lock，防与并发索引交错
- 写完对交付物自校验（解压比对 sha256），失败不留包
- 只保留最近 KEEP_EXPORTS 个导出包
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import chromadb
import numpy as np

from config import CFG
from index import (CHROMA_DIR, COLLECTION_NAME, DATA_DIR, VAULT, collect_md_files,
                   index_vault, kb_stale, log, write_lock)

MODEL_NAME = CFG["model_name"]
EXPORT_DIR = DATA_DIR / "export"
GUIDE_FILE = Path(__file__).parent / "AI_GUIDE.md"
KEEP_EXPORTS = CFG["keep_exports"]
EXPORT_PREFIX = "obsidian-rag-export-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20) or b""
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def refresh_if_stale(vault):
    """索引过期则自动增量刷新；刷新失败中止导出（不交付过期数据）。"""
    stale, stats = kb_stale(vault)
    if not stale:
        return
    log("检测到索引过期，自动增量刷新...")
    try:
        index_vault(vault, incremental=True)
    except Exception as e:
        log(f"自动刷新失败：{e}")
        log("导出中止：避免交付过期数据。请排查原因后重试。")
        sys.exit(1)
    log("刷新完成，继续导出")


def read_all_chunks():
    """持锁读取全部切块。返回 (ids, texts, metas, emb_lists)。"""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )
    with write_lock():
        got = collection.get(include=["embeddings", "documents", "metadatas"])
    ids = got["ids"]
    texts = got["documents"] or []
    metas = got["metadatas"] or []
    emb_data = got["embeddings"]
    emb_lists = [np.asarray(e, dtype=np.float64).tolist()
                 for e in (emb_data if emb_data is not None else [])]
    return ids, texts, metas, emb_lists


def write_payload_gz(rows, fobj):
    """逐行写 payload（gzip mtime=0 保证确定性）。"""
    with gzip.GzipFile(fileobj=fobj, mode="wb", mtime=0) as gz:
        for rec in rows:
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            gz.write(line.encode("utf-8"))


def build_manifest(package, chunk_count, emb_dim, payload_sha, meta_sha, vault_files):
    return {
        "package": package,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model_name": MODEL_NAME,
        "chromadb_version": chromadb.__version__,
        "python_version": sys.version.split()[0],
        "chunk_count": chunk_count,
        "embedding_dim": emb_dim,
        "vault_file_count": len(vault_files),
        "vault_total_bytes": sum(f["size"] for f in vault_files),
        "sha256": {"payload": payload_sha, "index_meta": meta_sha},
        "vault_files": vault_files,
    }


def inject_guide(info):
    """AI_GUIDE.md 模板注入本次导出信息，返回 bytes。"""
    text = GUIDE_FILE.read_text(encoding="utf-8")
    for key, val in info.items():
        text = text.replace(key, str(val))
    return text.encode("utf-8")


def write_zip(out_path, payload_buf, meta_bytes, vault_sources, manifest, guide_bytes):
    """原子写 zip：先写 .tmp 再 os.replace。"""
    tmp = out_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("payload.jsonl.gz", payload_buf.getvalue())
        zf.writestr("index_meta.json", meta_bytes)
        for rel, fpath in vault_sources:
            zf.write(fpath, arcname=f"vault/{rel}")
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("AI_GUIDE.md", guide_bytes)
    os.replace(tmp, out_path)


def verify_package(zip_path, manifest):
    """把包当接收方完整校验（解压触发 CRC + 逐文件 sha256 比对）。失败返回错误列表。"""
    verify_dir = EXPORT_DIR / f".verify_{datetime.now().strftime('%H%M%S%f')}"
    verify_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                target = verify_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(zf.read(name))
                except Exception as e:
                    errors.append(f"{name} 解压失败（CRC 校验未通过）：{e}")

        def check(rel, expect, label):
            p = verify_dir / rel
            if not p.exists():
                errors.append(f"缺少文件：{label}")
                return
            if sha256_file(p) != expect:
                errors.append(f"{label} sha256 与 manifest 不匹配")

        check("payload.jsonl.gz", manifest["sha256"]["payload"], "payload.jsonl.gz")
        check("index_meta.json", manifest["sha256"]["index_meta"], "index_meta.json")
        for f in manifest["vault_files"]:
            check(f"vault/{f['rel']}", f["sha256"], f"vault/{f['rel']}")
        if not (verify_dir / "AI_GUIDE.md").exists():
            errors.append("缺少 AI_GUIDE.md")
    finally:
        shutil.rmtree(verify_dir, ignore_errors=True)
    return errors


def prune_old_exports():
    """data/export 内只保留最近 KEEP_EXPORTS 个导出包（按修改时间）。"""
    packs = sorted(EXPORT_DIR.glob(EXPORT_PREFIX + "*.zip"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in packs[KEEP_EXPORTS:]:
        p.unlink(missing_ok=True)
        log(f"清理旧导出包：{p.name}")


def main():
    ap = argparse.ArgumentParser(
        description="导出全部 RAG 数据为可移植 zip 包（接收端用 import.py 秒级恢复，不重算嵌入）")
    ap.add_argument("--vault", default=VAULT,
                    help="Vault 路径（默认取环境变量 OBSIDIAN_VAULT 或 index.VAULT）")
    args = ap.parse_args()

    vault = args.vault
    if not Path(vault).is_dir():
        log(f"错误：Vault 路径不存在：{vault}")
        log("提示：可用 --vault 指定，或设置环境变量 OBSIDIAN_VAULT。")
        sys.exit(1)

    refresh_if_stale(vault)

    ids, texts, metas, emb_lists = read_all_chunks()
    if not ids:
        log("错误：索引为空（0 块），无数据可导出。")
        sys.exit(1)
    if not (DATA_DIR / "index_meta.json").exists():
        log("错误：index_meta.json 不存在，无法导出。")
        sys.exit(1)

    meta_bytes = (DATA_DIR / "index_meta.json").read_bytes()
    vault_sources = [
        (str(p.relative_to(vault)).replace("\\", "/"), p)
        for p in collect_md_files(vault)
    ]
    emb_dim = len(emb_lists[0])
    vault_files = [
        {"rel": rel, "sha256": sha256_file(fpath), "size": fpath.stat().st_size}
        for rel, fpath in vault_sources
    ]

    package = EXPORT_PREFIX + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip"
    out_path = EXPORT_DIR / package
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    rows = [
        {"id": i, "text": t, "embedding": e, "meta": m}
        for i, t, e, m in zip(ids, texts, emb_lists, metas)
    ]
    payload_buf = io.BytesIO()
    write_payload_gz(rows, payload_buf)
    payload_sha = sha256_bytes(payload_buf.getvalue())

    manifest = build_manifest(package, len(ids), emb_dim, payload_sha,
                              sha256_bytes(meta_bytes), vault_files)
    guide_bytes = inject_guide({
        "__PACKAGE__": out_path.name,
        "__EXPORTED_AT__": manifest["exported_at"],
        "__CHUNK_COUNT__": manifest["chunk_count"],
        "__FILE_COUNT__": manifest["vault_file_count"],
        "__EMBED_DIM__": manifest["embedding_dim"],
    })
    write_zip(out_path, payload_buf, meta_bytes, vault_sources, manifest, guide_bytes)

    errors = verify_package(out_path, manifest)
    if errors:
        out_path.unlink(missing_ok=True)
        log("导出自校验失败，包已删除，请重试：")
        for e in errors:
            log(f"  ✗ {e}")
        sys.exit(1)

    prune_old_exports()

    size_mb = out_path.stat().st_size / 1024 / 1024
    log(f"导出完成：{out_path}")
    log(f"大小 {size_mb:.1f} MB | 块数 {len(ids)} | 源文件 {len(vault_sources)} | 向量维度 {emb_dim}")
    log("=" * 60)
    log("接收端（Linux/Windows 通用）三步：")
    log("  1. 获取源码并安装依赖（pip install -r requirements.txt）")
    log(f"  2. python import.py --yes \"{out_path.name}\"")
    log("  3. export OBSIDIAN_VAULT=<仓库>/vault_export 后启动 server.py")
    log("完整指引与失败处理见包内 AI_GUIDE.md")


if __name__ == "__main__":
    main()
