"""server.py — MCP server，暴露 search_knowledge / reindex_knowledge / index_status 工具给 opencode。
用法：python server.py（stdio 模式，由 opencode 自动拉起）

关键设计（2026-08-07 修复）：
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

from mcp.server import MCPServer

from index import (VAULT, COLLECTION_NAME, HEARTBEAT_TIMEOUT, LockBusyError, index_vault,
                   kb_stale, log, progress_text, read_progress)
from retriever import hybrid_search, reset_bm25_index

server = MCPServer("obsidian-rag", title="Obsidian RAG", version="0.1.1")

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


def _start_background_index(vault, incremental=True):
    """启动后台索引线程；已在跑则跳过。返回 (是否已启动, 提示文本)。"""
    if _index_running():
        return False, "索引任务已在运行（见 index_status 进度）。"
    t = threading.Thread(target=_run_index, args=(vault, incremental), daemon=True,
                         name="bg-reindex")
    _background["thread"] = t
    _background["pid"] = os.getpid()
    t.start()
    return True, f"索引更新已在后台启动（进程 PID {os.getpid()}），可随时用 index_status 查看进度。"


def _run_index(vault, incremental):
    """后台线程体：跑索引 + 重建 BM25 缓存；任何异常落进度文件为 error 态。"""
    try:
        index_vault(vault, incremental=incremental)
        reset_bm25_index()
        log("后台索引完成，BM25 缓存已重建")
    except LockBusyError as e:
        log(f"后台索引因锁繁忙放弃：{e}")
    except Exception as e:
        log(f"后台索引失败：{e}")


def _chroma_is_empty():
    """Chroma 是否为空库（首跑场景需同步等待索引完成）。"""
    try:
        from index import _chroma_count
        return _chroma_count() == 0
    except Exception:
        return True


def ensure_fresh():
    """指纹检查：Vault 有变化则自动增量重建索引，返回提示文本。

    2026-08-07 改：索引更新转入后台——搜索不等待（旧索引先出结果），
    避免"索引几分钟 + MCP 30s 超时"导致的假死。唯一例外：索引库为空
    （首跑/刚清库）时同步等待，否则无结果可出。

    任何一步失败都降级为"用旧索引检索 + 提示"，绝不让检索整体失败。
    """
    try:
        stale, stats = kb_stale(VAULT)
        if not stale:
            return ""
        parts = []
        if stats["changed"]:
            parts.append(f"{stats['changed']} 个文件变更")
        if stats["added"]:
            parts.append(f"新增 {stats['added']} 个文件")
        if stats["removed"]:
            parts.append(f"删除 {stats['removed']} 个文件")
        summary = "、".join(parts) or "内容变化"

        if _index_running():
            return f"（检测到{summary}；索引更新已在后台进行，以下为现有索引结果）\n\n"

        if _chroma_is_empty():
            log("索引库为空，同步重建（首跑场景）...")
            index_vault(VAULT, incremental=True)
            reset_bm25_index()
            return f"（检测到{summary}，索引已更新）\n\n"

        started, note = _start_background_index(VAULT)
        if started:
            return f"（检测到{summary}；{note}以下为现有索引结果，稍后检索会自动使用新索引）\n\n"
        return f"（检测到{summary}；{note}）\n\n"
    except Exception as e:
        log(f"自动同步索引失败（使用旧索引继续）：{e}")
        return f"（检测到 Vault 变化，但自动更新索引失败：{e}；以下为旧索引结果）\n\n"


@server.tool()
def search_knowledge(query: str, top_k: int = None, folder: str = "", include_body: bool = True) -> str:
    """语义搜索 Obsidian 知识库（混合检索：向量语义 + 关键词）。query 为自然语言问题；folder 可按 Vault 内子目录过滤（如 ROCKETRY 或 AI Knowledge System，边界校验的前缀匹配，须是完整目录名）；返回最相关的笔记段落与来源文件路径，来源行带 [块 k/N] 位置标记。include_body=False 时只返回来源清单（文件名+标题+块位置，无正文），用于两阶段检索：先低成本枚举全量候选，再对命中少数精读。注意：会话首次调用或 Vault 变更后首次调用需加载模型并重建关键词索引，耗时数十秒属正常。"""
    try:
        note = ensure_fresh()
        return note + hybrid_search(query, top_k=top_k, folder=folder, include_body=include_body)
    except Exception as e:
        log(f"search_knowledge 失败：{e}")
        return f"（检索失败：{e}；请稍后重试或检查 Vault/索引状态）"


@server.tool()
def reindex_knowledge() -> str:
    """增量重建索引（扫描 Vault，只对内容变化的文件重新嵌入）。后台执行、立即返回；
    用 index_status 查看实时进度（阶段/文件数/块数/ETA/卡死判断）。一般无需手动调用：
    每次搜索前自动检测 Vault 变化并增量同步；仅在自动同步失败提示、或用户明确要求立即刷新时使用。"""
    try:
        started, note = _start_background_index(VAULT)
        if started:
            return "已开始后台重建索引。" + note
        return "未启动新任务。" + note
    except Exception as e:
        log(f"reindex_knowledge 失败：{e}")
        return f"（重建索引失败：{e}；旧索引保持可用）"


@server.tool()
def index_status() -> str:
    """查看索引进度：阶段、文件/块处理数、耗时、预计剩余时间、心跳状态。
    双重判定：①心跳停止 >15s（独立线程每 5s 恒定刷新，与硬件性能无关）→ 疑似卡死；
    ②心跳正常但进度 >25s 未推进 → 疑似批次内卡死（假活）。均附处置建议。
    适用于：reindex_knowledge 或自动同步启动后轮询；索引长时间无响应时判断卡死。"""
    p = read_progress()
    return progress_text(p)


if __name__ == "__main__":
    server.run("stdio")
