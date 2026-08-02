"""index.py — 扫描 Obsidian Vault，按标题切块，嵌入，存入 Chroma。"""
import argparse
import hashlib
import json
import msvcrt
import os
import re
import sys
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

VAULT = r"D:\_STOREROOM\lol\Obsidian Vault"
DATA_DIR = Path(__file__).parent / "data"
CHROMA_DIR = DATA_DIR / "chroma"
INDEX_META = DATA_DIR / "index_meta.json"
LOCK_FILE = DATA_DIR / "index.lock"
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


def split_paragraphs(text):
    """按空行切段落，去首尾空白。返回段落列表。"""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(text, max_len=1500):
    """按句子边界切块，永不从句子中间剪断。

    边界：中文标点（。！？）后；英文 .!? 后必须紧跟空格 + 大写字母或数字
    （避免 Mr./e.g./3.14 等缩写/小数被误切）。常见缩写先保护再切。
    单句本身超过 max_len 时宁长勿断（整句保留），避免语义截断。
    """
    ABBR = {"mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.", "etc.", "vs.", "st.", "no.", "al."}
    for a in list(ABBR) + [a.capitalize() for a in ABBR]:
        text = text.replace(" " + a, " " + a.replace(".", "\x00"))
    parts = re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+(?=[A-Z0-9])", text)
    parts = [p.replace("\x00", ".") for p in parts]
    chunks = []
    cur = ""
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if cur and len(cur) + len(s) > max_len:
            chunks.append(cur)
            cur = ""
        cur += s
        cur += " "  # 还原被 \s+ 消费的分隔空格，避免 "dollars.This" 粘连
    if cur:
        chunks.append(cur.strip())
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
    """原子写：先写临时文件再 os.replace，防止写入中断损坏 meta。"""
    DATA_DIR.mkdir(exist_ok=True)
    tmp = INDEX_META.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INDEX_META)


def collect_md_files(vault):
    """收集应纳入索引的 .md 文件（与索引使用同一套过滤规则）。"""
    return [p for p in Path(vault).rglob("*.md")
            if not any(part in EXCLUDE_DIRS for part in p.parts)
            and p.name not in STRUCTURE_FILES
            and not p.name.startswith(EXCLUDE_PATTERNS)]


def make_anchor(front, body):
    """中文锚点：文件 title/summary 含中文且正文以英文为主时，生成中文锚点文本。

    仅用于该文件的首块（i==0），避免锚点词频在多块间虚增、降低块级区分度。
    """
    zh_parts = [front.get("title", ""), front.get("summary", "")]
    zh_parts = [p for p in zh_parts if p and re.search(r"[\u4e00-\u9fff]", p)]
    if not zh_parts:
        return ""
    non_zh = re.sub(r"[\u4e00-\u9fff]", "", body)
    ascii_chars = sum(1 for c in non_zh if c.isascii() and (c.isalpha() or c.isdigit()))
    if ascii_chars < max(len(non_zh) * 0.5, 40):
        return ""
    return "【" + "；".join(zh_parts) + "】\n"


def kb_stale(vault):
    """指纹检查：先比 mtime+size（快速路径），变化才读全文 MD5。

    只读、不加载模型、不嵌入。返回 (是否过期, 统计)。
    """
    meta = load_meta()
    if not meta:
        return True, {"changed": 0, "added": len(collect_md_files(vault)), "removed": 0}
    files = collect_md_files(vault)
    seen = set()
    changed = 0
    added = 0
    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        seen.add(rel)
        st = fpath.stat()
        entry = meta.get(rel)
        if entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime_ns:
            continue
        content = fpath.read_text(encoding="utf-8", errors="replace")
        h = hashlib.md5(content.encode("utf-8")).hexdigest()
        if entry and entry.get("hash") == h:
            continue
        if entry:
            changed += 1
        else:
            added += 1
    removed = len(set(meta) - seen)
    stale = bool(changed or added or removed)
    return stale, {"changed": changed, "added": added, "removed": removed}


def write_lock():
    """进程级文件锁（Windows msvcrt），串行化 Chroma 写操作，防并发损坏。"""
    import contextlib

    @contextlib.contextmanager
    def _lock():
        DATA_DIR.mkdir(exist_ok=True)
        f = open(LOCK_FILE, "a+b")
        try:
            f.seek(0, 2)
            if f.tell() == 0:
                f.write(b"0")
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                f.close()

    return _lock()


def index_vault(vault, incremental=True, full=False):
    model = get_model()
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        name="obsidian_kb", metadata={"hnsw:space": "cosine"}
    )

    meta = {} if full else load_meta()
    files = collect_md_files(vault)
    current_rels = {str(f.relative_to(vault)).replace("\\", "/") for f in files}

    new_ids, new_texts, new_metas = [], [], []
    changed = 0
    unchanged = 0

    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        st = fpath.stat()
        old = meta.get(rel)
        # 快速路径：size+mtime 未变则免读全文（与 kb_stale 同一指纹策略）
        if incremental and old and old.get("size") == st.st_size and old.get("mtime") == st.st_mtime_ns:
            unchanged += 1
            continue
        content = fpath.read_text(encoding="utf-8", errors="replace")
        fhash = hashlib.md5(content.encode("utf-8")).hexdigest()
        if incremental and old and old.get("hash") == fhash:
            unchanged += 1
            continue

        front, body = extract_frontmatter(content)
        # 两级切块：标题切 → 超长块降级段落切 → 超长段落降级句子切（永不剪断句子）
        if len(body) <= 200:
            chunks = [(front.get("title", ""), body)]
        else:
            chunks = []
            for heading, text in split_by_headings(body):
                if len(text) <= 1500:
                    chunks.append((heading, text))
                else:
                    # 每个段落独立降级：段落超长才句子切，短段落保持完整
                    for p in split_paragraphs(text):
                        if len(p) <= 1500:
                            chunks.append((heading, p))
                        else:
                            for s in split_sentences(p):
                                chunks.append((heading, s))
        anchor = make_anchor(front, body) if chunks else ""

        for i, (heading, chunk_text) in enumerate(chunks):
            cid = f"{rel}::{i}"
            new_ids.append(cid)
            # 锚点只加首块：既有中文检索锚点，又不虚增全文件词频
            new_texts.append(anchor + chunk_text if i == 0 else chunk_text)
            new_metas.append({
                "file": rel,
                "heading": heading,
                "title": front.get("title", ""),
                "tags": front.get("tags", ""),
                "chunk": str(i),
                "anchor": anchor if i == 0 else "",
            })
        meta[rel] = {"hash": fhash, "chunks": len(chunks), "size": st.st_size, "mtime": st.st_mtime_ns}
        changed += 1

    # 裁剪 meta：移除磁盘上已不存在的文件条目（Bug1 关键一步），
    # 这样下方 valid 集合不含已删文件，其块会在清理阶段被 collection.delete。
    meta = {rel: info for rel, info in meta.items() if rel in current_rels}

    # 嵌入在锁外完成（最耗时：全量 ~28s），锁内只做毫秒级写操作。
    # 注意：模型不可重入，单进程内编码与写入必须串行，故先编码后持锁。
    emb = None
    if new_ids:
        log(f"嵌入 {len(new_ids)} 个新块（{changed} 个文件变更，{unchanged} 个未变）...")
        emb = model.encode(new_texts, normalize_embeddings=True, show_progress_bar=True)

    # 写锁包住全部写操作（delete/upsert/清理/save_meta）
    with write_lock():
        if full:
            log("全量重建：清空旧库后写入...")
            client.delete_collection("obsidian_kb")
            collection = client.get_or_create_collection(
                name="obsidian_kb", metadata={"hnsw:space": "cosine"}
            )

        if emb is not None:
            collection.upsert(ids=new_ids, embeddings=emb.tolist(), documents=new_texts, metadatas=new_metas)
        else:
            log(f"无变更（{unchanged} 个文件全部命中缓存）")

        # 精确清理：有效 id = meta 中每个文件按记录的块数生成。
        # 已删文件（不在 meta）与幽灵块（块数变少后超出 chunks 的旧 id）都会被清除。
        valid = set()
        for rel, info in meta.items():
            for i in range(info.get("chunks", 0)):
                valid.add(f"{rel}::{i}")
        all_ids = collection.get(include=[])["ids"]
        stale = [i for i in all_ids if i not in valid]
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
