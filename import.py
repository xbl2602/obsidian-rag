"""import.py — 从导出包恢复 RAG 索引（纯向量重插，不重算嵌入，不加载模型）。

用法：python import.py 包.zip [--yes]
  --yes：跳过"覆盖现有索引"交互确认（AI 非交互环境必加，详见 AI_GUIDE.md）

流程（两阶段，防半成品）：
  1) 解压到 data/import_work/ → CRC + sha256 全量校验（任一失败即中止，目标数据零改动）
  2) 交互确认 → 备份 index_meta → 持锁重建 Chroma → count 校验 + 抽查 embedding
     余弦一致性 → 原子写回 index_meta → 源文件落位 vault_export/ → 包移入
     data/archive/ → 清理临时区
重跑幂等：中途失败可原样重跑。
"""
import argparse
import gzip
import hashlib
import json
import random
import shutil
import sys
import zipfile
from pathlib import Path

import chromadb
import numpy as np

from config import CFG
from index import CHROMA_DIR, COLLECTION_NAME, DATA_DIR, VAULT, save_meta, write_lock

UPSERT_BATCH = CFG["import_upsert_batch"]
IMPORT_WORK_DIR = DATA_DIR / "import_work"
VAULT_EXPORT_DIR = Path(__file__).parent / "vault_export"
ARCHIVE_DIR = DATA_DIR / "archive"


def log(*args):
    print(*args, file=sys.stderr)


def abort(msg):
    log(f"导入中止：{msg}")
    log("目标数据未被改动。")
    sys.exit(1)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20) or b""
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def unpack(package):
    """解压到临时区；任何 zip 损坏（含 CRC）即中止。"""
    if IMPORT_WORK_DIR.exists():
        shutil.rmtree(IMPORT_WORK_DIR)
    try:
        IMPORT_WORK_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package) as zf:
            zf.extractall(IMPORT_WORK_DIR)
    except zipfile.BadZipFile as e:
        abort(f"包无法解压或 CRC 校验失败（可能传输损坏）：{e}")
    except OSError as e:
        abort(f"解压时 IO 错误（检查磁盘空间与权限）：{e}")


def verify_manifest(manifest):
    """sha256 全量校验；任一失败即中止（此时目标数据零改动）。"""
    errors = []

    def check(path, expect, label):
        if not path.exists():
            errors.append(f"缺少文件：{label}")
            return
        got = sha256_file(path)
        if got != expect:
            errors.append(f"{label} sha256 不匹配（期望 {expect}，实得 {got}）")

    check(IMPORT_WORK_DIR / "payload.jsonl.gz", manifest["sha256"]["payload"], "payload.jsonl.gz")
    check(IMPORT_WORK_DIR / "index_meta.json", manifest["sha256"]["index_meta"], "index_meta.json")
    for f in manifest.get("vault_files", []):
        check(IMPORT_WORK_DIR / "vault" / f["rel"], f["sha256"], f"vault/{f['rel']}")

    if len(manifest.get("vault_files", [])) != manifest.get("vault_file_count"):
        errors.append("vault_files 数量与 manifest 记录不一致")
    if errors:
        for e in errors:
            log(f"  ✗ {e}")
        abort("完整性校验失败，包已损坏。请重新下载/传输后重试。")


def existing_chunks():
    """现有索引块数；无法读取视为异常并中止（避免误判覆盖）。"""
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        col = client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
        return col.count()
    except Exception as e:
        abort(f"无法读取现有索引（{e}）。请先备份或清理 data/chroma 后重试。")


def confirm_overwrite(n, yes):
    if n == 0:
        return
    if yes:
        log(f"目标已有 {n} 块索引，--yes 已指定，直接覆盖。")
        return
    try:
        ans = input(f"目标已有 {n} 块索引，导入将覆盖这些数据。继续？[y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans != "y":
        abort("用户取消。")


def emb_consistent(got, expect, tol=1 - 1e-6):
    """向量一致性：验证检索等价性（余弦相似度 ≈1）。

    chroma 新版本对 cosine 空间做归一化 + float32 存储（原库可能为 float64 原样），
    逐位比较不可靠；余弦相似度对尺度/精度变化不变，是检索排名的本质判据。
    """
    g = np.asarray(got, dtype=np.float64)
    e = np.asarray(expect, dtype=np.float64)
    cos = float(np.dot(g, e) / (np.linalg.norm(g) * np.linalg.norm(e) + 1e-300))
    return cos >= tol


def rebuild(manifest):
    """持锁重建 Chroma 并自检（count + 抽查 3 块 embedding 余弦一致性）。"""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    with write_lock():
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
        col = client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

        ids, texts, embs, metas = [], [], [], []
        n = 0
        sample_pool = []  # (id, embedding) 引用池，重建后随机抽 3 条做余弦一致性校验
        with gzip.open(IMPORT_WORK_DIR / "payload.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                ids.append(rec["id"])
                texts.append(rec["text"])
                metas.append(rec["meta"])
                embs.append(rec["embedding"])
                sample_pool.append((rec["id"], rec["embedding"]))
                n += 1
                if len(ids) >= UPSERT_BATCH:
                    col.upsert(ids=ids, embeddings=embs, documents=texts, metadatas=metas)
                    ids, texts, embs, metas = [], [], [], []
        if ids:
            col.upsert(ids=ids, embeddings=embs, documents=texts, metadatas=metas)

        got_count = col.count()
        if got_count != n:
            raise RuntimeError(f"重建后 count={got_count}，期望 {n}")

        for cid, expect in random.Random(7).sample(sample_pool, min(3, len(sample_pool))):
            found = col.get(ids=[cid], include=["embeddings"])["embeddings"]
            if found is None or len(found) == 0:
                raise RuntimeError(f"自检失败：块 {cid} 未在重建后的库中找到")
            if not emb_consistent(found[0], expect):
                raise RuntimeError(f"自检失败：块 {cid} embedding 与包内不一致")
    return client, col, n


def restore_meta(manifest):
    """原子写回 index_meta.json（复用 index.save_meta）。"""
    meta = json.loads((IMPORT_WORK_DIR / "index_meta.json").read_text(encoding="utf-8"))
    save_meta(meta)
    log(f"index_meta.json 已恢复（{len(meta)} 个文件指纹）")


def place_vault():
    """源文件落位 vault_export/（先清旧目录再移动）。"""
    if VAULT_EXPORT_DIR.exists():
        shutil.rmtree(VAULT_EXPORT_DIR)
    src = IMPORT_WORK_DIR / "vault"
    if src.exists():
        shutil.move(str(src), str(VAULT_EXPORT_DIR))
        log(f"源文件已解压到：{VAULT_EXPORT_DIR}")


def main():
    ap = argparse.ArgumentParser(
        description="从导出包恢复 RAG 索引（不重算嵌入）。AI 非交互执行请加 --yes。")
    ap.add_argument("package", help="导出包路径（obsidian-rag-export-*.zip）")
    ap.add_argument("--yes", action="store_true", help="跳过覆盖确认（非交互/AI 自动执行必加）")
    args = ap.parse_args()

    package = Path(args.package)
    if not package.is_file():
        abort(f"包不存在：{package}")

    unpack(package)
    manifest_path = IMPORT_WORK_DIR / "manifest.json"
    if not manifest_path.exists():
        abort("包内缺少 manifest.json，无法校验。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    log(f"包信息：{manifest.get('package')} | 导出于 {manifest.get('exported_at')} "
        f"| 块数 {manifest.get('chunk_count')} | 源文件 {manifest.get('vault_file_count')}")

    verify_manifest(manifest)

    n_existing = existing_chunks()
    confirm_overwrite(n_existing, args.yes)

    meta_backup = DATA_DIR / "index_meta.json.bak"
    if (DATA_DIR / "index_meta.json").exists():
        shutil.copy2(DATA_DIR / "index_meta.json", meta_backup)

    try:
        client, col, n = rebuild(manifest)
        restore_meta(manifest)
    except Exception as e:
        if meta_backup.exists():
            shutil.move(str(meta_backup), DATA_DIR / "index_meta.json")
        log(f"重建失败：{e}")
        log("已恢复原有 index_meta.json；可修复问题后原样重跑导入（幂等）。")
        sys.exit(1)
    else:
        meta_backup.unlink(missing_ok=True)

    place_vault()

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived = ARCHIVE_DIR / package.name
    shutil.move(str(package), str(archived))
    shutil.rmtree(IMPORT_WORK_DIR, ignore_errors=True)

    log("=" * 60)
    log(f"导入完成：Chroma {n} 块，自检通过（count 一致 + 抽查 embedding 余弦一致）")
    log(f"包已归档：{archived}")
    log(f"源文件位置：{VAULT_EXPORT_DIR}")
    log("下一步（重要）：")
    log(f"  1. export OBSIDIAN_VAULT=\"{VAULT_EXPORT_DIR}\"")
    if not Path(VAULT).exists():
        log("  2. ⚠ 当前 index.VAULT 指向的路径不存在，若不设置 OBSIDIAN_VAULT，")
        log("     server 自动同步会判定“文件全删”并清空刚导入的索引！")
    log("  3. 启动检索：python server.py（详细步骤见包内 AI_GUIDE.md）")


if __name__ == "__main__":
    main()
