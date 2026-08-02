"""index.py — 扫描 Obsidian Vault，按标题切块，嵌入，存入 Chroma。"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

VAULT = r"D:\_STOREROOM\lol\Obsidian Vault"
DATA_DIR = Path(__file__).parent / "data"
CHROMA_DIR = DATA_DIR / "chroma"
INDEX_META = DATA_DIR / "index_meta.json"
MODEL_NAME = "BAAI/bge-m3"
EXCLUDE_DIRS = {".obsidian", ".smart-env", ".trash", ".git", "TEMP", "templates"}
# 结构类文件：纯链接清单/指令文件，非知识本体，排除以免污染检索
STRUCTURE_FILES = {"目录.md", "AGENTS.md", "LOG.md", "README.md"}
# AI 会话/临时文件模式
EXCLUDE_PATTERNS = ("session-", "会话", ".tmp")

_model = None


def log(*args):
    """所有进度信息输出到 stderr，避免污染 MCP stdio 协议。"""
    print(*args, file=sys.stderr)


def get_model():
    global _model
    if _model is None:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log(f"加载 embedding 模型（device={device}）...")
        _model = SentenceTransformer(MODEL_NAME, device=device)
    return _model


def split_by_headings(text):
    """按 Markdown 标题切块，每个 H1/H2/H3 起新块。返回 [(heading_path, content)]"""
    lines = text.splitlines()
    chunks = []
    current_heading = ""
    current_lines = []
    heading_re = re.compile(r"^(#{1,3})\s+(.+)$")

    def flush():
        if current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                chunks.append((current_heading, body))

    for line in lines:
        m = heading_re.match(line)
        if m:
            flush()
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return chunks


def extract_frontmatter(text):
    """提取 frontmatter 元数据，返回 dict 和去掉 frontmatter 的正文。"""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4 :]
            for line in fm.splitlines():
                m = re.match(r"^([\w]+):\s*(.*)$", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return meta, body


def load_meta():
    if INDEX_META.exists():
        return json.loads(INDEX_META.read_text(encoding="utf-8"))
    return {}


def save_meta(meta):
    DATA_DIR.mkdir(exist_ok=True)
    INDEX_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_md_files(vault):
    """收集应纳入索引的 .md 文件（与索引使用同一套过滤规则）。"""
    return [p for p in Path(vault).rglob("*.md")
            if not any(part in EXCLUDE_DIRS for part in p.parts)
            and p.name not in STRUCTURE_FILES
            and not p.name.startswith(EXCLUDE_PATTERNS)]


def kb_stale(vault):
    """指纹检查：扫描 Vault 与 index_meta.json 对比，返回 (是否过期, 统计)。

    只读、不加载模型、不嵌入。变更统计：changed=内容变化, added=新增, removed=已删。
    """
    meta = load_meta()
    if not meta:
        return True, {"changed": 0, "added": len(collect_md_files(vault)), "removed": 0}
    files = collect_md_files(vault)
    cur = {}
    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        content = fpath.read_text(encoding="utf-8", errors="replace")
        cur[rel] = hashlib.md5(content.encode("utf-8")).hexdigest()
    changed = sum(1 for rel, h in cur.items() if meta.get(rel, {}).get("hash") != h)
    added = len(set(cur) - set(meta))
    removed = len(set(meta) - set(cur))
    stale = bool(changed or added or removed)
    return stale, {"changed": changed, "added": added, "removed": removed}


def index_vault(vault, incremental=True, full=False):
    model = get_model()
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        name="obsidian_kb", metadata={"hnsw:space": "cosine"}
    )

    if full:
        log("全量重建：清空现有索引...")
        client.delete_collection("obsidian_kb")
        collection = client.get_or_create_collection(
            name="obsidian_kb", metadata={"hnsw:space": "cosine"}
        )
        INDEX_META.unlink(missing_ok=True)

    files = collect_md_files(vault)
    meta = load_meta()

    new_ids, new_texts, new_metas = [], [], []
    changed = 0
    unchanged = 0

    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        content = fpath.read_text(encoding="utf-8", errors="replace")
        fhash = hashlib.md5(content.encode("utf-8")).hexdigest()
        old = meta.get(rel)
        if incremental and old and old.get("hash") == fhash:
            unchanged += 1
            continue

        front, body = extract_frontmatter(content)
        chunks = split_by_headings(body) if len(body) > 200 else [(front.get("title", ""), body)]
        if not chunks:
            continue

        # 中文锚点：若文件标题/摘要含中文且正文以英文为主，则把中文元数据拼到块头，
        # 让中文查询能通过语义锚点命中英文内容（BGE-M3 多语言向量）。
        zh_anchor = ""
        zh_parts = [front.get("title", ""), front.get("summary", "")]
        zh_parts = [p for p in zh_parts if p and re.search(r"[\u4e00-\u9fff]", p)]
        if zh_parts:
            non_zh = re.sub(r"[\u4e00-\u9fff]", "", body)
            ascii_chars = sum(1 for c in non_zh if c.isascii() and (c.isalpha() or c.isdigit()))
            if ascii_chars >= max(len(non_zh) * 0.5, 40):
                zh_anchor = "【" + "；".join(zh_parts) + "】\n"

        for i, (heading, chunk_text) in enumerate(chunks):
            if len(chunk_text) > 1500:
                chunk_text = chunk_text[:1500]
            cid = f"{rel}::{i}"
            new_ids.append(cid)
            new_texts.append(zh_anchor + chunk_text)
            new_metas.append({
                "file": rel,
                "heading": heading,
                "title": front.get("title", ""),
                "tags": front.get("tags", ""),
                "chunk": str(i),
            })
        meta[rel] = {"hash": fhash, "chunks": len(chunks)}
        changed += 1

    if new_ids:
        log(f"嵌入 {len(new_ids)} 个新块（{changed} 个文件变更，{unchanged} 个未变）...")
        emb = model.encode(new_texts, normalize_embeddings=True, show_progress_bar=True)
        collection.upsert(ids=new_ids, embeddings=emb.tolist(), documents=new_texts, metadatas=new_metas)
    else:
        log(f"无变更（{unchanged} 个文件全部命中缓存）")

    valid_prefixes = {f"{rel}::" for rel in meta}
    all_ids = collection.get(include=[])["ids"]
    stale = [i for i in all_ids if not any(i.startswith(p) for p in valid_prefixes)]
    if stale:
        collection.delete(ids=stale)
        log(f"清理 {len(stale)} 个失效块")

    save_meta(meta)
    log(f"完成。Chroma 现有 {collection.count()} 个块。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--full", action="store_true", help="全量重建，忽略增量")
    args = ap.parse_args()
    index_vault(args.vault, incremental=not args.full, full=args.full)
