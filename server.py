"""server.py — MCP server，暴露 list_libraries / search_knowledge / reindex_knowledge / index_status 工具给 opencode。
用法：python server.py（stdio 模式，由 opencode 自动拉起）

关键设计（2026-08-07 修复，2026-08-11 多库化，2026-08-12 单例守卫）：
- 多库：注册表 data/libraries.json（library.py 管理）。search_knowledge 的
  libraries/exclude 做白名单减法选库（空=全部库）；list_libraries 先枚举后选库；
- 单例守卫（data/server.pid）：opencode 启动 MCP 时可能连续拉起多个实例（观察到
  双实例 = 双份模型常驻 ~4GB + 锁竞争），后启动的实例发现已有存活实例立即退出；
- reindex 在后台线程执行：reindex_knowledge 立即返回，进度经 data/index_progress.json
  实时落盘，AI 可随时调 index_status 查看进度 / ETA / 卡死判断；
- 心跳由独立线程每 5s 恒定写盘（与硬件性能无关），卡死判定 = 心跳停 >15s
  或 进度停滞 >25s（双重判定，抓批次内假活）；
- 索引过程中搜索不阻塞：用现有索引返回结果并附提示（首跑索引为空时例外，同步等待）；
- 写锁 60s 超时 + 持有者 PID 定位，杜绝"残留实例持锁 → 新实例无限死等"。
"""
import os
import threading
import time
from datetime import datetime

from mcp.server import MCPServer

from index import (HEARTBEAT_TIMEOUT, LockBusyError, index_library, kb_stale,
                   log, progress_text, read_progress)
from library import (effective_config, list_summary, load_registry, meta_path,
                     resolve_entries)
from retriever import hybrid_search_hyde, reset_bm25_index
from singleton import acquire_singleton

server = MCPServer("obsidian-rag", title="Obsidian RAG", version="0.2.1")

# 进程内后台索引状态（防重复启动；进度详情在 index_progress.json）
_background = {"thread": None, "pid": None}


def _index_running():
    """本进程（或残留进程）是否有索引任务在跑。

    进度文件里 running=True 但心跳已停（> HEARTBEAT_TIMEOUT，心跳由独立线程
    每 5s 恒定刷新，与硬件性能无关）→ 视为残留死进程的陈旧标志，不挡新任务。
    """
    if _background["thread"] is not None and _background["thread"].is_alive():
        return True
    p = read_progress()
    if p.get("running"):
        last = p.get("updated_at") or 0
        if time.time() - last <= HEARTBEAT_TIMEOUT:
            return True
        log("检测到陈旧 running 进度（心跳已停），视为残留标志，允许启动新任务")
    return False


def _start_background_index(libs, incremental=True):
    """启动后台索引线程（libs = effective_config 列表）；已在跑则跳过。返回 (是否已启动, 提示文本)。"""
    if _index_running():
        return False, "索引任务已在运行（见 index_status 进度）。"
    t = threading.Thread(target=_run_index, args=(libs, incremental), daemon=True,
                         name="bg-reindex")
    _background["thread"] = t
    _background["pid"] = os.getpid()
    t.start()
    names = "、".join(l["name"] for l in libs)
    return True, f"索引更新已在后台启动（库：{names}，PID {os.getpid()}），可随时用 index_status 查看进度。"


def _run_index(libs, incremental):
    """后台线程体：逐库跑索引 + 重建 BM25 缓存；单库失败不阻断其他库。"""
    for lib in libs:
        try:
            index_library(lib, incremental=incremental)
            reset_bm25_index()
            log(f"后台索引完成：{lib['name']}，BM25 缓存已重建")
        except LockBusyError as e:
            log(f"后台索引因锁繁忙放弃（{lib['name']}）：{e}")
        except Exception as e:
            log(f"后台索引失败（{lib['name']}）：{e}")


def _chroma_is_empty():
    """全部注册库的 Chroma 是否为空（首跑场景需同步等待索引完成）。"""
    try:
        import chromadb
        from index import CHROMA_DIR
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        for e in load_registry():
            try:
                col = client.get_collection(effective_config(e)["collection"])
            except Exception:
                continue  # collection 未创建 = 空
            if col.count() != 0:
                return False
        return True
    except Exception:
        return True


def ensure_fresh():
    """指纹检查：各注册库有变化则自动增量重建索引，返回提示文本。

    2026-08-07 改：索引更新转入后台——搜索不等待（旧索引先出结果），
    避免"索引几分钟 + MCP 30s 超时"导致的假死。唯一例外：索引库为空
    （首跑/刚清库）时同步等待，否则无结果可出。

    任何一步失败都降级为"用旧索引检索 + 提示"，绝不让检索整体失败。
    """
    try:
        stale_libs = []
        parts = []
        for entry in load_registry():
            cfg = effective_config(entry)
            try:
                stale, stats = kb_stale(cfg["path"], meta_path(cfg["name"]),
                                        cfg["collection"], cfg["exclude_dirs"],
                                        cfg["exclude_files"], cfg["exclude_patterns"],
                                        cfg["extensions"])
            except Exception as e:
                log(f"指纹检查失败（{cfg['name']}）：{e}")
                stale, stats = True, {}
            if not stale:
                continue
            if stats.get("missing"):
                parts.append(f"{cfg['name']} 路径不存在，跳过自动同步（保留旧索引）")
                continue
            if stats.get("emptied"):
                # 目录还在但一个文件都扫不到：几乎总是"源文件没放回去"而不是
                # "用户真的删光了"。此时同步 = 清空该库索引，代价不可逆，故跳过。
                # （典型场景：import.py --create 建了空目录，见 index.kb_stale）
                parts.append(f"{cfg['name']} 目录为空（扫不到任何文件），"
                             f"跳过自动同步以免清空索引；确认源文件已放回该路径后可手动重建")
                continue
            if stats.get("version_upgrade"):
                parts.append(f"{cfg['name']} 切块逻辑版本升级，需重建索引")
                stale_libs.append(cfg)
                continue
            if stats.get("changed"):
                parts.append(f"{cfg['name']} {stats['changed']} 个文件变更")
            if stats.get("added"):
                parts.append(f"{cfg['name']} 新增 {stats['added']} 个文件")
            if stats.get("removed"):
                parts.append(f"{cfg['name']} 删除 {stats['removed']} 个文件")
            stale_libs.append(cfg)
        if not stale_libs:
            return ""
        summary = "、".join(parts) or "内容变化"

        if _index_running():
            return f"（检测到{summary}；索引更新已在后台进行，以下为现有索引结果）\n\n"

        if _chroma_is_empty():
            log("索引库为空，同步重建（首跑场景）...")
            for lib in stale_libs:
                index_library(lib, incremental=True)
            reset_bm25_index()
            return f"（检测到{summary}，索引已更新）\n\n"

        started, note = _start_background_index(stale_libs)
        if started:
            return f"（检测到{summary}；{note}以下为现有索引结果，稍后检索会自动使用新索引）\n\n"
        return f"（检测到{summary}；{note}）\n\n"
    except Exception as e:
        log(f"自动同步索引失败（使用旧索引继续）：{e}")
        return f"（检测到 Vault 变化，但自动更新索引失败：{e}；以下为旧索引结果）\n\n"


@server.tool()
def list_libraries() -> str:
    """列出全部已注册知识库（库名/路径/块数/最近索引/独立配置覆盖），供 search_knowledge 的 libraries/exclude 参数选库。库名是唯一标识：未知库名会被拒绝并提示可用库。"""
    try:
        rows = list_summary()
    except Exception as e:
        log(f"list_libraries 失败：{e}")
        return f"（获取库列表失败：{e}）"
    if not rows:
        return "（当前没有已注册库。请用 CLI：python library.py add <路径> 注册。）"
    lines = ["已注册知识库："]
    lines.append(f"  {'库名':<22}{'块数':>7}  最近索引  路径")
    for r in rows:
        blocks = str(r["blocks"]) if r["blocks"] >= 0 else "?"
        last = "从未" if not r["last_indexed"] else \
            datetime.fromtimestamp(r["last_indexed"]).strftime("%Y-%m-%d %H:%M")
        over = f"（覆盖：{r['overrides']}）" if r["overrides"] else ""
        lines.append(f"  {r['name']:<22}{blocks:>7}  {last:<17}{r['path']} {over}")
    return "\n".join(lines)


@server.tool()
def search_knowledge(query: str, top_k: int = None, libraries: str = "", exclude: str = "",
                     folder: str = "", include_body: bool = True) -> str:
    """语义搜索知识库（混合检索：向量语义 + 关键词）。query 为自然语言问题。库选择（先调 list_libraries 查看可用库名）：libraries 为空 = 全部库；"A" 只搜单库；"A,B" 多库并查；exclude="B" = 全部库排除 B（反选）；最终范围 = (libraries 非空 ? libraries : 全部) − exclude，未知名会报错并列出可用库。folder 可按库内子目录过滤（如 ROCKETRY 或 AI Knowledge System，须是完整目录名）。返回最相关的笔记段落与来源文件路径，来源行带 [库名/路径] 与 [块 k/N] 位置标记。include_body=False 时只返回来源清单（文件名+标题+块位置，无正文），用于两阶段检索：先低成本枚举全量候选，再对命中少数精读。注意：会话首次调用或 Vault 变更后首次调用需加载模型并重建关键词索引，耗时数十秒属正常。"""
    try:
        note = ensure_fresh()
        # hybrid_search_hyde：hyde_enabled=false（默认）时就是普通 hybrid_search，
        # 零额外开销。2026-08-14 接线——此前 HyDE 整个特性没有任何调用方。
        return note + hybrid_search_hyde(query, top_k=top_k, libraries=libraries,
                                         exclude=exclude, folder=folder,
                                         include_body=include_body)
    except Exception as e:
        log(f"search_knowledge 失败：{e}")
        return f"（检索失败：{e}；请稍后重试或检查 Vault/索引状态）"


@server.tool()
def reindex_knowledge(library: str = "") -> str:
    """增量重建索引（扫描库文件，只对内容变化的文件重新嵌入）。library 为空或 "all" = 全部注册库，否则为单个库名（须在 list_libraries 中可见）。后台执行、立即返回；用 index_status 查看实时进度（阶段/文件数/块数/ETA/卡死判断）。一般无需手动调用：每次搜索前自动检测各库变化并增量同步；仅在自动同步失败提示、或用户明确要求立即刷新时使用。"""
    try:
        if library in ("", "all"):
            libs = [effective_config(e) for e in load_registry()]
        else:
            libs = [effective_config(e) for e in resolve_entries(library, "")]
        if not libs:
            return "（没有已注册的库。请先用 library.py add <路径> 注册。）"
        started, note = _start_background_index(libs)
        if started:
            return "已开始后台重建索引。" + note
        return "未启动新任务。" + note
    except ValueError as e:
        return f"（{e}）"
    except Exception as e:
        log(f"reindex_knowledge 失败：{e}")
        return f"（重建索引失败：{e}；旧索引保持可用）"


@server.tool()
def index_status() -> str:
    """查看索引进度：阶段、库、文件/块处理数、耗时、预计剩余时间、心跳状态。
    双重判定：①心跳停止 >15s（独立线程每 5s 恒定刷新，与硬件性能无关）→ 疑似卡死；
    ②心跳正常但进度 >25s 未推进 → 疑似批次内卡死（假活）。均附处置建议。
    适用于：reindex_knowledge 或自动同步启动后轮询；索引长时间无响应时判断卡死。"""
    p = read_progress()
    return progress_text(p)


if __name__ == "__main__":
    acquire_singleton()
    server.run("stdio")
