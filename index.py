"""index.py — 扫描 Obsidian Vault，按标题切块，嵌入，存入 Chroma。"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import chromadb

# 跨平台文件锁：Windows 用 msvcrt（字节范围锁），Linux/macOS 用 fcntl（flock）。
# 按平台函数内局部导入：Linux 上 import index 不触碰 msvcrt，反之亦然。
_IS_WINDOWS = os.name == "nt"

# 环境变量覆盖 Vault 路径（接收端机器用 OBSIDIAN_VAULT 指向导入解压出的 vault 目录）
VAULT = os.environ.get("OBSIDIAN_VAULT", r"D:\_STOREROOM\lol\Obsidian Vault")
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


def log(*args):
    """所有进度信息输出到 stderr，避免污染 MCP stdio 协议。"""
    print(*args, file=sys.stderr)


_model = None
_device = None  # 当前模型所在设备："cuda" / "cpu" / None
DEVICE_STATE_FILE = DATA_DIR / "device_state.json"
CUDA_COOLDOWN_SECONDS = 300  # CUDA 失败后冷却 5 分钟，到期轻量探测自动重试
_cuda_cooldown_until = 0.0   # 进程内冷却截止时间戳


def _write_device_state(state):
    """原子写设备状态（同 save_meta 策略，仅作诊断记录，不做门禁）。"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        tmp = DEVICE_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DEVICE_STATE_FILE)
    except Exception as e:
        log(f"写入设备状态失败（忽略）：{e}")


def _cuda_probe():
    """轻量探测 GPU 可用性：分配 64MB 显存成功即视为可用（毫秒级，失败返回 False）。

    探测只分配小显存，自身不会 OOM 崩溃；模型加载若仍失败由 encode_safe 兜底。
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        t = torch.empty(64 * 1024 * 1024, dtype=torch.uint8, device="cuda")
        del t
        return True
    except Exception:
        return False


def _cuda_ready():
    """冷却期内直接用 CPU；到期后轻量探测，可用才尝试 CUDA。

    无硬性窗口：显存恢复后最多等一个冷却期（5 分钟）自动切回。
    """
    global _cuda_cooldown_until
    if time.time() < _cuda_cooldown_until:
        return False
    if not _cuda_probe():
        _cuda_cooldown_until = time.time() + CUDA_COOLDOWN_SECONDS
        return False
    return True


def _cooldown_cuda(reason):
    """CUDA 失败：进入 5 分钟冷却 + 诊断落盘（到期自动探测重试，非硬性窗口）。"""
    global _cuda_cooldown_until
    _cuda_cooldown_until = time.time() + CUDA_COOLDOWN_SECONDS
    _write_device_state({"device": "cuda", "failed_at": time.time(), "reason": reason[:200]})
    log(f"CUDA 失败，进入 {CUDA_COOLDOWN_SECONDS}s 冷却（到期自动探测重试）：{reason}")


def _build_model(device, fp16=False):
    """构建模型实例；import 在函数内（延迟加载，import 本身可能因内存不足失败）。"""
    from sentence_transformers import SentenceTransformer
    if fp16:
        return SentenceTransformer(MODEL_NAME, device=device,
                                   model_kwargs={"torch_dtype": "float16"})
    return SentenceTransformer(MODEL_NAME, device=device)


def _load_model(device):
    """按 device 加载模型。CPU 优先 fp16 减半内存（P6），失败回退 fp32。"""
    if device == "cpu":
        try:
            return _build_model("cpu", fp16=True), "cpu"
        except Exception as e:
            log(f"CPU fp16 加载失败，回退 fp32：{e}")
    return _build_model(device), device


def _try_switch_back_cuda():
    """CPU 模型中且探测到显存恢复：释放 CPU 模型 → 加载 CUDA；失败回滚并冷却。"""
    global _model, _device
    old = _model
    try:
        _model = None  # 先释放 CPU 模型，避免新旧模型双份内存峰值
        new, _ = _load_model("cuda")
        _model, _device = new, "cuda"
        del old
        log("显存已恢复，自动切回 CUDA")
    except Exception as e:
        _model, _device = old, "cpu"  # 回滚，继续用 CPU
        _cooldown_cuda(str(e))
        log(f"切回 CUDA 失败，保持 CPU：{e}")


def get_model():
    """懒加载 embedding 模型。CUDA 失败自动降级 CPU（含 import torch 失败）。

    显存恢复后自动切回：CPU 模型缓存期间每次调用先做毫秒级 GPU 探测，
    通过即切换——无硬性等待窗口，最多一个冷却期粒度。
    """
    global _model, _device
    if _model is not None:
        if _device == "cpu" and _cuda_ready():
            _try_switch_back_cuda()
        return _model
    if _cuda_ready():
        try:
            log("加载 embedding 模型（device=cuda）...")
            _model, _device = _load_model("cuda")
            return _model
        except Exception as e:
            log(f"CUDA 初始化失败（{e}），降级 CPU")
            _cooldown_cuda(str(e))
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
    log("加载 embedding 模型（device=cpu）...")
    _model, _device = _load_model("cpu")
    return _model


def _is_memory_error(e):
    """是否内存/显存不足类错误（WinError 1455 页面文件不足、CUDA OOM、MemoryError）。"""
    text = str(e).lower()
    return isinstance(e, MemoryError) or any(k in text for k in (
        "out of memory", "cuda oom", "paging file", "1455", "commitment limit"))


def encode_safe(texts, batch_size=32):
    """带 CUDA→CPU 自动降级的编码入口（索引与检索共用）。

    CUDA 内存不足（OOM/页面文件不足）→ 标记失败、卸载模型、切 CPU 重试一次；
    仍失败（CPU 内存也不足）才抛出，由上层降级提示。
    """
    try:
        return get_model().encode(texts, normalize_embeddings=True,
                                  batch_size=batch_size, show_progress_bar=False)
    except (RuntimeError, OSError, MemoryError) as e:
        if _is_memory_error(e) and _device == "cuda":
            log(f"CUDA 编码内存不足（{e}），自动降级 CPU 重试...")
            fallback_to_cpu(str(e))
            return get_model().encode(texts, normalize_embeddings=True,
                                      batch_size=batch_size, show_progress_bar=False)
        raise


def fallback_to_cpu(reason=""):
    """卸载当前模型并切 CPU（同进程内生效，无需重启）。"""
    global _model, _device
    _cooldown_cuda(reason or "未知原因")
    try:
        import torch
        if _model is not None:
            _model = None
            torch.cuda.empty_cache()
    except Exception as e:
        log(f"卸载 CUDA 模型失败（忽略）：{e}")
    _device = None
    log("已切换到 CPU 模式")


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


def _chroma_count():
    """Chroma 实际块数（毫秒级，不加载模型）。失败返回 None（库损坏等）。"""
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        return client.get_or_create_collection(
            name="obsidian_kb", metadata={"hnsw:space": "cosine"}
        ).count()
    except Exception as e:
        log(f"Chroma count 失败（忽略）：{e}")
        return None


def kb_stale(vault):
    """指纹检查：先比 mtime+size（快速路径），变化才读全文 MD5。

    只读、不加载模型、不嵌入。返回 (是否过期, 统计)。
    额外校验 Chroma↔meta 一致性（崩溃自愈）：块数不符（如 --full 中途被杀
    导致 0 块）也视为过期，触发重建。
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

    expected = sum(info.get("chunks", 0) for info in meta.values())
    actual = _chroma_count()
    if actual is not None and actual != expected:
        log(f"索引一致性校验失败：Chroma {actual} 块 vs meta {expected} 块，触发重建")
        stale = True

    return stale, {"changed": changed, "added": added, "removed": removed}


def _lock_acquire(f):
    """按平台获取文件锁：Windows msvcrt 字节锁 / Linux fcntl flock。"""
    if _IS_WINDOWS:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _lock_release(f):
    """按平台释放文件锁（与 _lock_acquire 严格对称）。"""
    if _IS_WINDOWS:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def write_lock():
    """进程级文件锁（Windows msvcrt / Linux fcntl），串行化 Chroma 写操作，防并发损坏。"""
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
            _lock_acquire(f)
            yield
        finally:
            try:
                _lock_release(f)
            finally:
                f.close()

    return _lock()


def index_vault(vault, incremental=True, full=False):
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        name="obsidian_kb", metadata={"hnsw:space": "cosine"}
    )

    meta = {} if full else load_meta()
    # 崩溃自愈（P7）：meta 有数据但 Chroma 空（如 --full 中途被杀在清库窗口）
    # → 增量指纹全命中时 new_ids 为空，普通增量路径无法补数据，需强制全量重嵌。
    if not full and meta and collection.count() == 0:
        log(f"检测到索引库为空（meta 记录 {sum(i.get('chunks', 0) for i in meta.values())} 块），自动全量重建")
        meta = {}
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
    # encode_safe 自动处理 CUDA 内存不足 → CPU 降级。
    emb = None
    if new_ids:
        log(f"嵌入 {len(new_ids)} 个新块（{changed} 个文件变更，{unchanged} 个未变）...")
        emb = encode_safe(new_texts)

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
