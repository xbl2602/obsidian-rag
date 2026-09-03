"""wemm_indexer.py — WEMM 页级视觉导航索引（跑在项目 .venv）。

把每个 PDF 的每一页渲染成图，交给本机 WEMM 看图服务（wemm_server.py，全局
Python 跑）编码成「每页一个向量」，写入**独立于文字索引**的 Chroma collection
`wemm_<库collection>`，配套独立 meta `data/wemm_meta_<库>.json`。检索时告诉 AI
内容在「哪份 PDF 的哪一页」。

与文字索引（index.py/bge-m3）彻底分离（架构红线）：
  - 独立 collection / 独立 meta / 独立版本号 / 独立自愈，绝不混向量空间；
  - 成功路径统一 current_wemm_rels.add(rel)，漏加 = 条目被裁剪、页被当幽灵清理；
  - 二分终态（统一终态红线）：不产向量的 PDF 必须落持久化终态，防每轮误判 stale；
  - Agent 门禁（红线6/7）：未授权文件在 stat 前冻结——零渲染、零编码、零 I/O，
    条目与页原样保留、绝不裁剪。

用法（需先启动 wemm_server.py，见其文件头）：
    .venv\\Scripts\\python wemm_indexer.py [--library <名>|all] [--full] [--backend on|off]

只处理 PDF（页渲染仅对 PDF 有意义）。md/txt/docx 不属于页级导航范畴。
WEMM 看图不依赖文字层，因此「扫描件 PDF」同样可页级导航——这正是它相对
bge-m3 文字索引的核心价值。
"""
import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import chromadb
import index as _index_mod
from library import effective_config, load_registry, resolve_entries, meta_path as lib_meta_path
from index import (_UNREADABLE, _terminal_entry, collect_md_files)

# 判空基准、终态与门禁要跟 index.py 保持一致语义（不能用私有名就本地重写同名谓词）
REASON_UNREADABLE = "unreadable"
REASON_EXTRACT_FAILED = "extract-failed"
REASON_EMPTY = "empty"
REASON_NOT_PDF = "not-pdf"

WEMM_VERSION = 1          # 页级导航自己的版本号；升级后强制全量重建 WEMM 库
WEMM_META_BASENAME = "wemm_meta_{name}.json"
WEMM_RENDER_DPI = 60      # 页图渲染 DPI 默认档（config wemm_render_dpi 可调；改后需 --full 重建）
WEMM_PAGE_SUFFIX = "wemm"  # 页级 collection 相对文字 collection 的后缀字段

# 远程编码可通过 HTTP 向 wemm_server 请求；测试注入假编码器覆盖这两个模块级函数。
def call_embed_image(b64_image: str, dim: int, url: str) -> list:
    """向 wemm_server 请求「一幅页图 → 512 维向量」。返回归一化向量列表。"""
    payload = json.dumps({"type": "image", "content": b64_image, "dim": dim}).encode("utf-8")
    req = urllib.request.Request(url + "/embed", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok") or "embedding" not in data:
        raise RuntimeError(data.get("error") or "wemm 服务返回异常")
    return data["embedding"]


def call_embed_text(text: str, dim: int, url: str) -> list:
    """向 wemm_server 请求「一段文字 → 512 维向量」（检索时编码查询用）。"""
    payload = json.dumps({"type": "text", "content": text, "dim": dim}).encode("utf-8")
    req = urllib.request.Request(url + "/embed", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok") or "embedding" not in data:
        raise RuntimeError(data.get("error") or "wemm 服务返回异常")
    return data["embedding"]


def _skipped(info):
    """单点谓词：该 WEMM 条目是否为「不产向量的持久化终态」。"""
    return isinstance(info, dict) and bool(info.get("tbd") or info.get("xfail"))


def wemm_collection(coll: str) -> str:
    """WEMM 页级 collection = 文字 collection + .wemm，绝不与文字库混名。"""
    return f"{coll}.wemm"


def wemm_meta_path(name: str) -> Path:
    """本库页级导航自己的 meta（与文字索引 meta 独立）。"""
    return _data_dir() / WEMM_META_BASENAME.format(name=name)


def _data_dir():
    # 惰性读 index.DATA_DIR（而非 import 期快照）：测试可 patch 重定向落盘路径
    return _index_mod.DATA_DIR


def _chroma_dir():
    return _index_mod.CHROMA_DIR


def load_wemm_meta(path):
    if Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def save_wemm_meta(meta, path):
    _data_dir().mkdir(exist_ok=True)
    data = dict(meta)
    data["_version"] = WEMM_VERSION
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def render_page_b64(pdf_path, page_idx, dpi=WEMM_RENDER_DPI):
    """渲染 PDF 第 page_idx 页为 base64 PNG（内存中完成，不写盘）。失败抛异常。"""
    import pymupdf  # 项目 .venv 有
    doc = pymupdf.open(pdf_path)
    try:
        page = doc.load_page(page_idx)
        pix = page.get_pixmap(dpi=dpi)
        b = pix.tobytes("png")
    finally:
        doc.close()
    return base64.b64encode(b).decode("ascii")


def page_count(pdf_path):
    import pymupdf
    doc = pymupdf.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def index_wemm_library(cfg, backend=True, full=False, agent_allowed=None,
                       url=None, model=None, dim=None, progress=None):
    """对一个注册库做 WEMM 页级索引（增量自愈）。

    cfg = effective_config(lib)。backend=False 或 wemm_backend=off 时直接返回
    （调用方负责本函数之外的门禁判定）。
    agent_allowed：None = 无限制；后缀集合 = 仅处理这些格式的 PDF，其余冻结（红线6）。
    返回统计 dict。
    """
    from config import CFG
    if not backend:
        return {}
    url = url or CFG.get("wemm_url")
    dim = dim or int(CFG.get("wemm_dim", 512))
    if not url:
        log("wemm_url 未配置，跳过 WEMM 索引")
        return {}
    url = str(url).rstrip("/")
    dpi = int(CFG.get("wemm_render_dpi", WEMM_RENDER_DPI))
    tag = f"[{cfg['name']}] "
    data_dir = _data_dir()
    data_dir.mkdir(exist_ok=True)
    collection_name = wemm_collection(cfg["collection"])
    meta_file = wemm_meta_path(cfg["name"])

    vault = Path(cfg["path"])
    if not vault.is_dir():
        log(f"{tag}库路径不存在，跳过 WEMM 索引（保留现有索引）：{vault}")
        return {}

    # 只收 PDF（页级导航仅对 PDF 有意义）
    files = collect_md_files(vault, cfg["exclude_dirs"], cfg["exclude_files"],
                             cfg["exclude_patterns"], ["pdf"])

    try:
        client = chromadb.PersistentClient(path=str(_chroma_dir()))
        collection = client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"})
    except Exception as e:
        log(f"{tag}WEMM Chroma 打开失败：{e}")
        return {"error": str(e)}

    meta = {} if full else load_wemm_meta(meta_file)
    if not full and meta.pop("_version", 1) != WEMM_VERSION:
        log(f"{tag}WEMM 页级版本升级（v{WEMM_VERSION}），强制全量重建")
        meta = {}
    # 一致性自愈（红线4 镜像）：meta 期望页数 ≠ Chroma 实际 → 转全量重建
    if not full and meta:
        _expected = sum(e.get("pages", 0) for e in meta.values()
                        if isinstance(e, dict) and not _skipped(e))
        try:
            _actual = collection.count()
        except Exception:
            _actual = -1
        if _actual != _expected:
            log(f"{tag}WEMM 一致性校验失败：meta 期望 {_expected} 页 vs Chroma 实际 "
                f"{_actual} 页，自动转全量重建")
            meta = {}

    current_wemm_rels = set()
    page_batches = []  # (rel, page_idx, vec)

    def _progress(msg):
        if progress:
            progress(msg)

    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        # Agent 门禁冻结（红线6/7）：未授权格式零渲染零编码零 I/O
        if agent_allowed is not None and "pdf" not in agent_allowed:
            if meta.get(rel) is not None:
                current_wemm_rels.add(rel)
            continue
        try:
            st = fpath.stat()
        except OSError as e:
            log(f"{tag}无法获取状态，本轮跳过（下轮重试）：{rel}（{e}）")
            continue
        old = meta.get(rel)
        current_wemm_rels.add(rel)
        # 快速路径：size+mtime 未变则免渲染免编码（与文字索引同指纹策略）
        if (not full and old and old.get("size") == st.st_size
                and old.get("mtime") == st.st_mtime_ns):
            continue
        # 字节指纹（原始字节 MD5），与文字索引同源，避免重复读取
        bhash = _file_md5(fpath)
        if not full and old and not _skipped(old) and old.get("hash") == bhash:
            continue

        # ---- 页面渲染 + 编码（视觉导航：扫描件/文字层 PDF 均可）----
        try:
            n_pages = page_count(fpath)
            if n_pages <= 0:
                meta[rel] = _terminal_entry(st, bhash, REASON_EMPTY)
                continue
            for i in range(n_pages):
                b64 = render_page_b64(str(fpath), i, dpi=dpi)
                vec = call_embed_image(b64, dim, url)
                page_batches.append((rel, i, vec))
            meta[rel] = {"hash": bhash, "pages": n_pages, "size": st.st_size,
                         "mtime": st.st_mtime_ns, "tbd": False}
        except Exception as e:
            log(f"{tag}WEMM 页提取失败，记入终态待重试：{rel}（{type(e).__name__}）")
            meta[rel] = _terminal_entry(st, bhash, REASON_EXTRACT_FAILED)
        _progress(f"WEMM 页索引 {len(page_batches)} 页（{rel}）")

    # ---- 写库（含精确清理与终态对齐，镜像 index.py 管理模式）----
    if full:
        try:
            client.delete_collection(collection_name)
            collection = client.get_or_create_collection(
                name=collection_name, metadata={"hnsw:space": "cosine"})
        except Exception as e:
            log(f"{tag}WEMM 清库失败：{e}")

    # 组装向量与元数据写库
    if page_batches:
        ids = [f"{rel}::{i}" for rel, i, _ in page_batches]
        emb = [v for _, _, v in page_batches]
        metas = [{"file": r, "page": i, "abs_path": str(fpath_abs(r, vault)),
                  "library": cfg["name"]} for r, i, _ in page_batches]
        try:
            collection.upsert(ids=ids, embeddings=emb, metadatas=metas)
        except Exception as e:
            log(f"{tag}WEMM 写库失败：{e}")

    # 精确清理：有效页 id = 本轮真实存在的文件（current_wemm_rels）按记录页数生成；
    # 磁盘上已删除的文件的页向量与幽灵页在此清除（裁剪必须在清理前完成，否则
    # 待删文件的页会被 valid 误保——镜像 index.py 管理模式）。
    valid = set()
    for rel, info in meta.items():
        if rel not in current_wemm_rels:
            continue
        if isinstance(info, dict):
            for i in range(info.get("pages", 0)):
                valid.add(f"{rel}::{i}")
    try:
        all_ids = collection.get(include=[])["ids"]
        stale = [i for i in all_ids if i not in valid]
        if stale:
            collection.delete(ids=stale)
    except Exception as e:
        log(f"{tag}WEMM 清理失败：{e}")

    # 裁剪 meta：移除磁盘上已不存在的文件条目
    meta = {rel: info for rel, info in meta.items() if rel in current_wemm_rels}
    save_wemm_meta(meta, meta_file)
    try:
        final_count = collection.count()
    except Exception:
        final_count = -1
    log(f"{tag}WEMM 完成：Chroma {final_count} 页，meta {len([k for k in meta if isinstance(meta[k], dict)])} 文件")
    return {"pages": final_count, "files": len([k for k in meta if isinstance(meta[k], dict)])}


def fpath_abs(rel, vault):
    return str(vault / rel)


def log(*args):
    print("[wemm-indexer]", *args, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", default="all", help="库名（或 all=全部注册库）")
    ap.add_argument("--full", action="store_true", help="全量重建 WEMM 页库")
    ap.add_argument("--backend", default=None,
                    help="on/off（缺省读 config wemm_backend）")
    args = ap.parse_args()

    from config import CFG
    backend_cfg = (args.backend if args.backend is not None
                   else CFG.get("wemm_backend", "off"))
    if backend_cfg not in ("on", "local"):
        log("WEMM 后端未开启（wemm_backend=off），跳过。需改 config 或 --backend on")
        return
    if backend_cfg == "local":
        backend_cfg = "on"

    try:
        if args.library in ("", "all"):
            entries = load_registry()
        else:
            entries = resolve_entries(args.library, "")
    except ValueError as e:
        log(f"错误：{e}")
        sys.exit(1)
    if not entries:
        log("错误：没有已注册的库。请先用 library.py add <路径> 注册。")
        sys.exit(1)

    for entry in entries:
        cfg = effective_config(entry)
        log(f"开始 WEMM 页索引库：{cfg['name']} → {cfg['path']}")
        try:
            index_wemm_library(cfg, backend=backend_cfg == "on", full=args.full,
                               agent_allowed=None)
        except Exception as e:
            log(f"[{cfg['name']}] WEMM 索引失败（继续下一库）：{e}")
    log("全部 WEMM 索引任务完成。")


if __name__ == "__main__":
    main()
