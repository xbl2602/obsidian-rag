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

from config import CFG
from extractors import BINARY_EXTS, TEXT_EXTS
from index import (HEARTBEAT_TIMEOUT, LockBusyError, collect_md_files,
                   index_library, kb_stale, log, progress_text, read_progress)
from library import (effective_config, list_summary, load_registry, meta_path,
                     resolve_entries, set_config)
from retriever import hybrid_search_hyde, reset_bm25_index
from singleton import acquire_singleton

server = MCPServer("obsidian-rag", title="Obsidian RAG", version="0.2.1")

# 进程内后台索引状态（防重复启动；进度详情在 index_progress.json）
_background = {"thread": None, "pid": None}


def _agent_allowed(cfg):
    """Agent 可处理的后缀集合 = 文本类恒可 ∪ 用户已批准的二进制格式。

    人机分权（2026-08-24）：extensions 是用户的格式开关，agent_formats 是
    用户对 Agent 的长期授权；未授权的二进制文件在 Agent 触发的索引中被冻结。
    """
    return set(TEXT_EXTS) | set(cfg.get("agent_formats") or [])


def _pending_formats(cfg):
    """已启用但未对 Agent 授权、且磁盘上确实存在文件的二进制格式 → {格式: 数量}。"""
    allowed = _agent_allowed(cfg)
    pend = {}
    for fmt in cfg["extensions"]:
        if fmt in allowed or fmt not in BINARY_EXTS:
            continue
        try:
            n = len(collect_md_files(cfg["path"], cfg["exclude_dirs"],
                                     cfg["exclude_files"], cfg["exclude_patterns"],
                                     [fmt]))
        except Exception:
            n = 0
        if n:
            pend[fmt] = n
    return pend


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
    """启动后台索引线程；已在跑则跳过。返回 (是否已启动, 提示文本)。

    lib dict 可携带 "_agent_allowed"（后缀集合）：Agent 路径的门禁集合，
    由 index_library 冻结未授权格式的文件；人类路径（GUI/CLI）不带此键 = 无限制。
    """
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
            index_library(lib, incremental=incremental,
                          agent_allowed=lib.get("_agent_allowed"))
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
        pending_total = {}
        for entry in load_registry():
            cfg = effective_config(entry)
            # Agent 门禁：自动同步只处理 文本类 + 已批准格式；未授权二进制文件冻结
            allowed = _agent_allowed(cfg)
            cfg["_agent_allowed"] = allowed
            pend = _pending_formats(cfg)
            for k, v in pend.items():
                pending_total[k] = pending_total.get(k, 0) + v
            try:
                stale, stats = kb_stale(cfg["path"], meta_path(cfg["name"]),
                                        cfg["collection"], cfg["exclude_dirs"],
                                        cfg["exclude_files"], cfg["exclude_patterns"],
                                        cfg["extensions"],
                                        tbd_ratio=CFG.get("tbd_exclude_ratio", 0.0),
                                        agent_allowed=allowed)
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
            if pending_total:
                detail = "、".join(f"{k}×{v}" for k, v in sorted(pending_total.items()))
                return (f"（另有未授权格式的文件暂不纳入索引：{detail}——"
                        f"经用户确认后可调 reindex_knowledge(allow_new_formats=true) 授权，"
                        f"或在 GUI 库管理中勾选；以下为现有检索结果）\n\n")
            return ""
        if pending_total:
            detail = "、".join(f"{k}×{v}" for k, v in sorted(pending_total.items()))
            parts.append(f"另有未授权格式文件暂不纳入（{detail}），待用户授权")
        summary = "、".join(parts) or "内容变化"

        if _index_running():
            return f"（检测到{summary}；索引更新已在后台进行，以下为现有索引结果）\n\n"

        if _chroma_is_empty():
            log("索引库为空，同步重建（首跑场景）...")
            for lib in stale_libs:
                index_library(lib, incremental=True,
                              agent_allowed=lib.get("_agent_allowed"))
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
    defaults = [n for n in (CFG.get("default_libraries") or [])
                if any(r["name"] == n for r in rows)]
    scope = "全部库" if not defaults else "、".join(defaults)
    lines = ["已注册知识库：",
             f"（默认检索范围：{scope}；libraries=\"all\" = 全部库，exclude=\"B\" = 反选）"]
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
    """语义搜索知识库（混合检索：向量语义 + 关键词）。query 为自然语言问题。库选择（先调 list_libraries 查看可用库名）：libraries 为空 = 默认库（配置 default_libraries，本机为 Obsidian Vault 单库，test/agents/skills 等非笔记库不参与）；"all" = 全部库；"A,B" 多库并查；exclude="B" = 全部库排除 B（反选）；最终范围 = (libraries 非空 ? libraries : 默认库) − exclude，未知名会报错并列出可用库。folder 可按库内子目录过滤（如 ROCKETRY 或 AI Knowledge System，须是完整目录名）。返回最相关的笔记段落与来源文件路径，来源行带 [库名/路径]、[块 k/N] 与 [置信度 x.xx] 位置标记；置信度低于阈值时标注（低置信度，仅供参考）或直接过滤（低于下限不输出，防止噪音被当真引用）。include_body=False 时只返回来源清单（文件名+标题+块位置，无正文），用于两阶段检索：先低成本枚举全量候选，再对命中少数精读。注意：会话首次调用或 Vault 变更后首次调用需加载模型并重建关键词索引，耗时数十秒属正常。"""
    try:
        note = ensure_fresh()
        # hybrid_search_hyde：hyde_enabled=false（默认）时就是普通 hybrid_search，
        # 零额外开销。2026-08-14 接线——此前 HyDE 整个特性没有任何调用方。
        # with_scores=True：MCP 输出置信度（2026-08-16 起，配合双阈值护栏）。
        return note + hybrid_search_hyde(query, top_k=top_k, libraries=libraries,
                                         exclude=exclude, folder=folder,
                                         include_body=include_body,
                                         defaults=CFG.get("default_libraries", []),
                                         with_scores=True)
    except Exception as e:
        import traceback
        log(f"search_knowledge 失败：{e}\n{traceback.format_exc()}")
        return f"（检索失败：{e}；请稍后重试或检查 Vault/索引状态）"


@server.tool()
def reindex_knowledge(library: str = "", allow_new_formats: bool = False) -> str:
    """增量重建索引（扫描库文件，只对内容变化的文件重新嵌入）。library 为空或 "all" = 全部注册库，否则为单个库名（须在 list_libraries 中可见）。后台执行、立即返回；用 index_status 查看实时进度。

    人机分权：默认只索引文本类（md/txt）+ 用户已批准的格式；库中启用了
    pdf/docx 但尚未批准时，这些文件的改动会被冻结并在返回信息中列出数量——
    须先向用户确认，用户同意后携带 allow_new_formats=true 再次调用即完成
    一次性长期授权（持久化到注册表，之后无需再确认；用户可随时在 GUI 取消）。"""
    try:
        libs = []
        approved = {}
        waiting = {}
        if library in ("", "all"):
            entries = load_registry()
        else:
            entries = resolve_entries(library, "")
        for e in entries:
            cfg = effective_config(e)
            pend = _pending_formats(cfg)
            if allow_new_formats and pend:
                # 用户已确认：把本次涉及的新格式写入长期授权（持久化到注册表）
                merged = sorted(set(cfg.get("agent_formats") or []) | set(pend))
                set_config(cfg["name"], "agent_formats", ",".join(merged))
                cfg = effective_config(next(
                    x for x in load_registry() if x["name"] == cfg["name"]))
                for k, v in pend.items():
                    approved[k] = approved.get(k, 0) + v
            else:
                for k, v in pend.items():
                    waiting[k] = waiting.get(k, 0) + v
            cfg["_agent_allowed"] = _agent_allowed(cfg)
            libs.append(cfg)
        if not libs:
            return "（没有已注册的库。请先用 library.py add <路径> 注册。）"
        started, note = _start_background_index(libs)
        msg = "已开始后台重建索引。" if started else "未启动新任务。"
        msg += note
        if approved:
            detail = "、".join(f"{k}×{v}" for k, v in sorted(approved.items()))
            msg += (f"\n✅ 经用户确认，已授权 Agent 索引格式并纳入本次任务：{detail}"
                    f"（长期有效，GUI 可取消）。")
        if waiting:
            detail = "、".join(f"{k}×{v}" for k, v in sorted(waiting.items()))
            msg += (f"\n⚠ 以下格式尚未获用户授权，本次不纳入：{detail}。"
                    f"如需纳入请先向用户确认，得到同意后携带 allow_new_formats=true 重试"
                    f"（将持久化授权）；或由用户在 GUI 库管理中勾选。")
        return msg
    except ValueError as e:
        return f"（{e}）"
    except Exception as e:
        import traceback
        log(f"reindex_knowledge 失败：{e}\n{traceback.format_exc()}")
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
