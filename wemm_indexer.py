"""wemm_indexer.py — WEMM 页级视觉导航索引（跑在项目 .venv）。

把每个 PDF 的每一页渲染成图，交给本机 WEMM 看图服务（wemm_server.py，全局
Python 跑）编码成「每页一个向量」，写入**独立于文字索引**的 Chroma collection
`<库collection>.wemm`，配套独立 meta `data/wemm_meta_<库>.json`。检索时告诉 AI
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
from library import effective_config, load_registry, resolve_entries
from index import (_terminal_entry, collect_md_files)

# 判空基准、终态与门禁要跟 index.py 保持一致语义（不能用私有名就本地重写同名谓词）
REASON_EXTRACT_FAILED = "extract-failed"
REASON_EMPTY = "empty"

WEMM_VERSION = 1          # 页级导航自己的版本号；升级后强制全量重建 WEMM 库
WEMM_META_BASENAME = "wemm_meta_{name}.json"
WEMM_RENDER_DPI = 60      # 页图渲染 DPI 默认档（config wemm_render_dpi 可调）
WEMM_UPSERT_BATCH = 1000  # 每批写库页数（Chroma 对单批有上限，分批避免大库直接炸批）

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


def _wemm_sig(model, dim, dpi):
    """WEMM 能力签名：模型/维度/渲染档位任一变化 = 旧页向量与当前配置不再同源，
    增量轮据此自动重渲染（成功条目携带 xsrc=签名，失败终态同样携带以便追溯）。"""
    return f"wemm:{model}:{dim}:{dpi}"


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


def release_server_after_run(url=None, log=None):
    """整轮结束、用完即卸（问题47）：显式 evict 看图服务，释放显存。

    调用方是多库驱动的整轮收尾（index.py main / server._run_index /
    wemm_indexer.main），finally 里调——正常结束调、被杀/异常也调。
    - 后端 off → 跳过（零开销，服务根本没起过）；
    - 服务没在跑 → evict 内部折叠 False，一句日志，无异常；
    - 本函数自身绝不抛异常（释放失败不能污染索引成果）。
    - 中途被杀没调到 → 服务端 idle 守护 5min 自卸兜底。
    与"抢占式 evict"（bge 加载前显存不足）是同一接口，语义一致。
    """
    _log = log or (lambda *a: None)
    try:
        from config import CFG
        if (CFG.get("wemm_backend") or "off") not in ("on", "local"):
            return False
        import gpu_arbiter
        ok = gpu_arbiter.evict_wemm(url or CFG.get("wemm_url"))
    except Exception:
        return False
    _log("WEMM 看图模型已卸载，显存已释放" if ok
         else "WEMM 服务未在运行，无需释放显存")
    return bool(ok)


def render_page_b64(pdf_path, page_idx, dpi=WEMM_RENDER_DPI, doc=None):
    """渲染 PDF 第 page_idx 页为 base64 PNG（内存中完成，不写盘）。失败抛异常。

    doc：调用方批量渲染时可传入已打开的 pymupdf 文档复用句柄（每页重开一次
    文件是纯浪费）；此时本函数不负责关闭它。
    """
    import pymupdf  # 项目 .venv 有
    if doc is not None:
        page = doc.load_page(page_idx)
        b = page.get_pixmap(dpi=dpi).tobytes("png")
    else:
        d = pymupdf.open(pdf_path)
        try:
            page = d.load_page(page_idx)
            b = page.get_pixmap(dpi=dpi).tobytes("png")
        finally:
            try:
                d.close()
            except Exception:
                pass
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
    model_id = str(CFG.get("wemm_model", ""))
    wsig = _wemm_sig(model_id, dim, dpi)
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
                             cfg["exclude_patterns"], ["pdf"],
                             selection=(cfg.get("selection_in"), cfg.get("selection_out")),
                             selection_default=cfg.get("selection_default", "follow"))

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
    pending_ok = {}    # rel -> 成功条目（写库成功后才并入 meta，防「meta 记了页数、库没写上」的假账）

    # 看图服务懒拉起（问题41）：首个真正需要渲染的文件才拉；全部命中快速路径
    # （无变更）时零拉起零开销。拉不起时该文件记失败终态，下轮自动重试。
    # 单轮单次（问题46）：同一次 index_wemm_library 内只尝试拉起一次——失败后
    server_state = {"ready": False, "tried": False}

    def _ensure_server_lazy():
        if server_state["ready"]:
            return True
        if server_state["tried"]:
            return False
        server_state["tried"] = True
        import gpu_arbiter
        ok, detail = gpu_arbiter.ensure_server(log=log)
        server_state["ready"] = ok
        if not ok:
            log(f"WEMM 看图服务不可用（{detail}）——相关文件记失败终态，下轮自动重试")
        return ok

    # progress 回调协议（问题47）：progress(msg, done, total)——文件级粒度
    # （用户拍板不到页）。单参数旧回调仍兼容（TypeError 回退），传 None 则零开销。
    def _progress(msg, done=None, total=None):
        if not progress:
            return
        try:
            progress(msg, done, total)
        except TypeError:
            progress(msg)

    for _fi, fpath in enumerate(files, 1):
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
        # 快速路径：size+mtime 未变且能力签名一致则免渲染免编码（与文字索引同指纹策略）。
        # 终态条目绝不走快速路径——失败文件（看图服务中途挂掉等）每轮都给重试机会，
        # 让「记入终态待重试」是真承诺而非死寂。
        if (not full and old and not _skipped(old) and old.get("xsrc") == wsig
                and old.get("size") == st.st_size
                and old.get("mtime") == st.st_mtime_ns):
            continue
        # 字节指纹（原始字节 MD5），与文字索引同源，避免重复读取
        bhash = _file_md5(fpath)
        if (not full and old and not _skipped(old) and old.get("hash") == bhash
                and old.get("xsrc") == wsig):
            continue

        # ---- 页面渲染 + 编码（视觉导航：扫描件/文字层 PDF 均可）----
        if not _ensure_server_lazy():
            meta[rel] = _terminal_entry(st, bhash, REASON_EXTRACT_FAILED, xsrc=wsig)
            continue
        import pymupdf
        try:
            doc = pymupdf.open(str(fpath))
        except Exception as e:
            log(f"{tag}WEMM 页提取失败，记入终态待重试：{rel}（{type(e).__name__}）")
            meta[rel] = _terminal_entry(st, bhash, REASON_EXTRACT_FAILED, xsrc=wsig)
            continue
        try:
            n_pages = doc.page_count
            if n_pages <= 0:
                meta[rel] = _terminal_entry(st, bhash, REASON_EMPTY)
                continue
            for i in range(n_pages):
                b64 = render_page_b64(str(fpath), i, dpi=dpi, doc=doc)
                vec = call_embed_image(b64, dim, url)
                page_batches.append((rel, i, vec))
            pending_ok[rel] = {"hash": bhash, "pages": n_pages, "size": st.st_size,
                               "mtime": st.st_mtime_ns, "tbd": False, "xsrc": wsig}
        except Exception as e:
            log(f"{tag}WEMM 页提取失败，记入终态待重试：{rel}（{type(e).__name__}）")
            meta[rel] = _terminal_entry(st, bhash, REASON_EXTRACT_FAILED, xsrc=wsig)
        finally:
            try:
                doc.close()
            except Exception:
                pass
        _progress(f"WEMM 页索引 {len(page_batches)} 页（{rel}）", _fi, len(files))

    # ---- 写库（含精确清理与终态对齐，镜像 index.py 管理模式）----
    if full:
        try:
            client.delete_collection(collection_name)
            collection = client.get_or_create_collection(
                name=collection_name, metadata={"hnsw:space": "cosine"})
        except Exception as e:
            log(f"{tag}WEMM 清库失败：{e}")

    # 组装向量与元数据写库：分批 upsert（Chroma 单批有上限，整库一把梭大库必炸）；
    # 全部批次成功才把成功条目并入 meta——写库失败时 meta 不记成功页数，下轮自动
    # 重渲染重写（宁可重编码一轮，不留「meta 记了页数、库没写上」的假账触发死循环）。
    if page_batches:
        ids = [f"{rel}::{i}" for rel, i, _ in page_batches]
        emb = [v for _, _, v in page_batches]
        metas = [{"file": r, "page": i, "abs_path": str(fpath_abs(r, vault)),
                  "library": cfg["name"]} for r, i, _ in page_batches]
        upsert_ok = True
        for s in range(0, len(ids), WEMM_UPSERT_BATCH):
            e = s + WEMM_UPSERT_BATCH
            try:
                collection.upsert(ids=ids[s:e], embeddings=emb[s:e],
                                  metadatas=metas[s:e])
            except Exception as ex:
                upsert_ok = False
                log(f"{tag}WEMM 写库失败（第 {s // WEMM_UPSERT_BATCH + 1} 批，"
                    f"本批 {min(WEMM_UPSERT_BATCH, len(ids) - s)} 页）：{ex}")
                break
        if upsert_ok:
            meta.update(pending_ok)
        else:
            log(f"{tag}WEMM 本轮写库未完成，成功条目不入 meta，下轮将重渲染重写")

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

    # 问题41：看图服务不再预检——index_wemm_library 内部懒拉起（有页要渲染
    # 才拉），全部命中快速路径时零拉起零开销

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

    try:
        for entry in entries:
            cfg = effective_config(entry)
            log(f"开始 WEMM 页索引库：{cfg['name']} → {cfg['path']}")
            try:
                index_wemm_library(cfg, backend=backend_cfg == "on", full=args.full,
                                   agent_allowed=None)
            except Exception as e:
                log(f"[{cfg['name']}] WEMM 索引失败（继续下一库）：{e}")
        log("全部 WEMM 索引任务完成。")
    finally:
        # 整轮结束用完即卸（问题47）：显存不留后台静默占用
        release_server_after_run(log=log)


if __name__ == "__main__":
    main()
