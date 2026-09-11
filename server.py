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
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from mcp.server import MCPServer

from config import CFG, DATA_DIR
from config import reload_config
from dedup import (DEFAULT_THRESHOLD as DEDUP_THRESHOLD, find_duplicates as dedup_find,
                   format_report as dedup_format_report)
from extractors import BINARY_EXTS, TEXT_EXTS, current_backend_sig
from index import _backend_changed, release_model
from retriever import release_reranker

from index import (HEARTBEAT_TIMEOUT, LockBusyError, collect_md_files,
                   index_library, kb_stale, load_meta, log, progress_text,
                   prune_unreferenced_data, read_progress, resolve_note_relations)
from library import (effective_config, list_summary, load_registry, meta_path,
                     norm_sel_path, resolve_entries, resolve_selection,
                     set_config, set_selection)
from retriever import hybrid_search_hyde, reset_bm25_index
import selection_gate
from singleton import acquire_singleton
from wemm_retriever import wemm_search
import gpu_arbiter

server = MCPServer("obsidian-rag", title="Obsidian RAG", version="0.2.1")

# 进程内后台索引状态（防重复启动；进度详情在 index_progress.json）
_background = {"thread": None, "pid": None}

# GPU 活动时间戳与空闲卸载（问题41：bge-m3 "完工即卸"——10 分钟无检索/索引
# 活动就释放常驻模型给 WEMM/其他用途让路；下次检索 get_model 懒加载回来）
_GPU_IDLE_UNLOAD_S = 600.0
_gpu_activity = {"ts": time.time()}


def _touch_gpu_activity():
    _gpu_activity["ts"] = time.time()


def _gpu_idle_unload_daemon():
    while True:
        time.sleep(60)
        try:
            if time.time() - _gpu_activity["ts"] < _GPU_IDLE_UNLOAD_S:
                continue
            import index as _idx
            from retriever import _reranker as _rr  # noqa: 惰性快照判定是否有货
            has_model = _idx._model is not None
            has_rr = _rr is not None
            if not has_model and not has_rr:
                continue
            release_reranker()
            release_model()
            log("GPU 空闲 %.0f 秒，已释放 bge-m3/reranker 常驻显存（下次检索自动懒加载）"
                % _GPU_IDLE_UNLOAD_S)
        except Exception as e:
            log(f"GPU 空闲卸载检查失败（忽略）：{type(e).__name__}: {e}")


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
                                     [fmt],
                                     selection=(cfg.get("selection_in"), cfg.get("selection_out")),
                                     selection_default=cfg.get("selection_default", "follow")))
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
    """后台线程体：逐库跑索引 + 重建 BM25 缓存；单库失败不阻断其他库。

    整轮全部成功后才做全局回收（prune_unreferenced_data）：任何一库失败说明
    meta 可能不完整（在途/半截），宁可不回收也不误删仍在用的缓存与集合。
    """
    _touch_gpu_activity()
    had_error = False
    try:
        for lib in libs:
            try:
                index_library(lib, incremental=incremental,
                              agent_allowed=lib.get("_agent_allowed"))
                reset_bm25_index()
                log(f"后台索引完成：{lib['name']}，BM25 缓存已重建")
            except LockBusyError as e:
                had_error = True
                log(f"后台索引因锁繁忙放弃（{lib['name']}）：{e}")
            except Exception as e:
                had_error = True
                log(f"后台索引失败（{lib['name']}）：{e}")
    finally:
        if not had_error:
            try:
                prune_unreferenced_data(log=log)
            except Exception as e:
                log(f"全局回收失败（忽略）：{e}")
        # 整轮结束用完即卸（问题47）：长驻 MCP 进程更要保证不留显存占用
        try:
            from wemm_indexer import release_server_after_run
            release_server_after_run(log=log)
        except Exception:
            pass


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
        _touch_gpu_activity()
        reload_config()  # 长驻进程的 CFG 是 import 快照；任务边界现读，让中途改的配置生效
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
                                        agent_allowed=allowed,
                                        selection=(cfg.get("selection_in"), cfg.get("selection_out")),
                                        selection_default=cfg.get("selection_default", "follow"))
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
            # wemm_sync=False：首跑同步发生在一次搜索调用里，页级导航建库可能
            # 数十分钟——绝不能阻塞搜索；页库交给下一轮常规索引自动补
            for lib in stale_libs:
                index_library(lib, incremental=True,
                              agent_allowed=lib.get("_agent_allowed"),
                              wemm_sync=False)
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


# ---------------------------------------------------------------------------
# 库内路径级勾选（问题44）：Agent 经 MCP 提议，硬编码两段式确认门禁。
# 门禁数据面在 selection_gate.py（可独立单测），此处只是 MCP 薄封装。
# ---------------------------------------------------------------------------
SELECTION_TTL_S = selection_gate.TTL_S


@server.tool()
def get_selection(library: str) -> str:
    """查看某库的路径级勾选状态（只读）：显式勾选/排除清单 + 各自数量。
    用于提议变更前了解现状。library 为确切库名（见 list_libraries）。"""
    try:
        e = resolve_entries(library, "")[0]
        cfg = effective_config(e)
        sin, sout = cfg.get("selection_in") or [], cfg.get("selection_out") or []
        lines = [f"库「{e['name']}」勾选状态（未列出的文件 = 中性，按格式开关与"
                 f"默认归属 {cfg.get('selection_default', 'follow')} 判定）："]
        lines.append(f"显式勾选 {len(sin)} 项：")
        lines += [f"  + {p}" for p in sin] or ["  （无）"]
        lines.append(f"显式排除 {len(sout)} 项：")
        lines += [f"  - {p}" for p in sout] or ["  （无）"]
        return '\n'.join(lines)
    except ValueError as err:
        return f"（{err}）"
    except Exception as err:
        log(f"get_selection 失败：{err}")
        return f"（查询失败：{err}）"


@server.tool()
def propose_selection_changes(library: str, changes: list) -> str:
    """提议库内文件/文件夹级勾选变更（in=纳入建库 / out=排除 / neutral=恢复跟随格式）。
    被排除的文件将从整个知识库流程中消失：不扫描、不嵌入、不 OCR、不建页级导航。

    ⚠ 硬性确认门禁：本工具绝不直接生效。它只生成一份待确认提案并返回变更清单
    与 6 位确认码；你必须把变更清单完整展示给用户、得到用户明确同意后，才能
    携带 proposal_id 与确认码调用 apply_selection_changes。未经用户同意就调用
    apply 是严重违规。提案 10 分钟后过期。"""
    try:
        reload_config()
        e = resolve_entries(library, "")[0]
        cfg = effective_config(e)
        try:
            proposal_id, code, diff = selection_gate.make_proposal(e["name"], cfg, changes)
        except selection_gate.GateError as err:
            return f"（{err}）"
        log(f"勾选变更提案已生成（库={library}，提案={proposal_id}，"
            f"{len(changes or [])} 项）——等待用户确认")
        return diff
    except ValueError as err:
        return f"（{err}）"
    except Exception as err:
        log(f"propose_selection_changes 失败：{err}")
        return f"（提案失败：{err}）"


@server.tool()
def apply_selection_changes(library: str, proposal_id: str, confirmation_code: str) -> str:
    """应用已获用户确认的勾选变更提案。只有 propose_selection_changes 返回的
    提案号 + 用户看到的确认码二者匹配、且未过期时才会生效——这是硬编码门禁，
    无任何配置可绕过。生效后下一轮索引自动应用（新排除文件的旧块与页向量会被清理）。"""
    try:
        try:
            changes = selection_gate.consume_proposal(library, proposal_id,
                                                      confirmation_code)
        except selection_gate.GateError as err:
            log(f"AUDIT 勾选提案被拒（库={library}，提案={proposal_id}）：{err}")
            return f"（{err}）"
        entry = set_selection(library, changes)
        ch_text = "；".join(f"{c['path']}→{c['action']}" for c in changes)
        log(f"AUDIT 勾选变更已生效（库={library}，提案={proposal_id}）：{ch_text}")
        n_in, n_out = len(entry.get("selection_in") or []), len(entry.get("selection_out") or [])
        return (f"✅ 勾选变更已生效（经用户确认）：{ch_text}。"
                f"当前显式勾选 {n_in} 项、显式排除 {n_out} 项。"
                f"下一轮索引自动应用；可调用 reindex_knowledge 立即执行。")
    except ValueError as err:
        return f"（{err}）"
    except Exception as err:
        log(f"apply_selection_changes 失败：{err}")
        return f"（应用失败：{err}）"


@server.tool()
def note_relations(path: str, library: str = "") -> str:
    """查询某篇笔记的双链关系（出链=本文链接到谁、入链=谁链接到本文），基于 Obsidian
    [[wiki链接]] 语法。这是独立于 search_knowledge 的关系查询，不参与语义检索排序，
    用于在搜到一篇笔记后"顺着链接找相关笔记"。

    path：笔记的库内相对路径（如 "20-Projects/机器.md"）或不含扩展名的标题（如
    "机器"，Obsidian 双链引用同款写法）——先按路径精确匹配，找不到再按标题匹配。
    library：库名，为空则用默认库（同 search_knowledge 语义）；只能定位单库，
    不支持 "all"，因为一篇笔记只会存在于一个库。标题在库内重名时任取其一，
    与 Obsidian 自身处理同名笔记的方式一样存在歧义。"""
    try:
        entries = resolve_entries(library, "", defaults=CFG.get("default_libraries", []))
    except ValueError as e:
        return f"（{e}）"
    if len(entries) > 1:
        names = "、".join(e["name"] for e in entries)
        return f"（library 需指定单个库，当前默认解析出多个：{names}；请显式传 library 参数指定其一）"
    cfg = effective_config(entries[0])
    result = resolve_note_relations(meta_path(cfg["name"]), path)
    if not result["resolved"]:
        return f"（在库「{cfg['name']}」中找不到笔记 \"{path}\"；path 支持库内相对路径或不含扩展名的标题）"
    lines = [f"「{cfg['name']}/{result['file']}」的双链关系："]
    lines.append("出链（本文链接到）：" + ("、".join(result["outlinks"]) if result["outlinks"] else "（无）"))
    lines.append("入链（谁链接到本文）：" + ("、".join(result["inlinks"]) if result["inlinks"] else "（无）"))
    return "\n".join(lines)


@server.tool()
def search_knowledge(query: str, top_k: int = None, libraries: str = "", exclude: str = "",
                     folder: str = "", include_body: bool = True) -> str:
    """语义搜索知识库（混合检索：向量语义 + 关键词）。query 为自然语言问题。库选择（先调 list_libraries 查看可用库名）：libraries 为空 = 默认库（配置 default_libraries，本机为 Obsidian Vault 单库，test/agents/skills 等非笔记库不参与）；"all" = 全部库；"A,B" 多库并查；exclude="B" = 全部库排除 B（反选）；最终范围 = (libraries 非空 ? libraries : 默认库) − exclude，未知名会报错并列出可用库。folder 可按库内子目录过滤（如 ROCKETRY 或 AI Knowledge System，须是完整目录名）。返回最相关的笔记段落与来源文件路径，来源行带 [库名/路径]、[块 k/N] 与 [置信度 x.xx·分档词] 位置标记。置信度解读（重要，2026-09-11 问题54 起为**真分尺度**）：数值 = 重排器判定的"该块与查询相关的概率"（0~1）——<0.30 弱相关（会被标注"低置信度，仅供参考"），0.30~0.75 中相关，≥0.75 高相关；0.5 = 重排器"无法判断"。同一次查询内分数越高越相关且排序越靠前，但跨查询比较分数没有意义。优先引用排序靠前且高相关的结果；弱相关档对口语化 query 也可能是有效命中，作为依据引用前应向用户核实。include_body=False 时只返回来源清单（文件名+标题+块位置，无正文），用于两阶段检索：先低成本枚举全量候选，再对命中少数精读。

正文前的"（…）"行是**系统给你的建议/提示，不是检索结果**：正文行只以 [来源] 开头。建议是系统根据这一批结果的形态自动给出的下一步动作，请照做，常见的几种：多条高置信命中 → 说明该主题内容集中，把 top_k 调大（如 15~20）能拿到更多小节；只有 1 条可信 → 只引用那一条，其余用 read_document 读整篇补上下文；命中含非笔记库（agents/skills）→ 那不是用户的笔记，只要笔记请传 libraries；同名不同目录 → 那是**两篇不同笔记**（标题一样、内容不同），引用与打开用完整路径区分；整批相关度偏低 → 换用笔记里的原始术语重搜，或先 include_body=false 枚举候选文件。

用法剧本：① 想读全文/看上下文 → read_document（PDF/Word 给已提取的 Markdown 全文）；② 要看图、表格、扫描页 → navigate_knowledge（页级视觉导航）；③ 想摸清有哪些相关文件 → include_body=false 拿清单再挑 1~3 条精读；④ 命中好但嫌少 → 调大 top_k；嫌噪音多 → 收窄 folder 或 libraries；⑤ 想知道某篇连到哪些笔记 → note_relations。注意：会话首次调用或 Vault 变更后首次调用需加载模型并重建关键词索引，耗时数十秒属正常。"""
    try:
        _touch_gpu_activity()
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
        _touch_gpu_activity()
        reload_config()  # 现读配置：用户中途补的 OCR Key / 切的后端必须被本轮索引看到
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


@server.tool()
def navigate_knowledge(query: str, top_k: int = 5, libraries: str = "",
                       exclude: str = "") -> str:
    """页级视觉导航（WEMM 看图嵌入）：给定一句话，找出"哪个 PDF 的哪一页"最符合。
    这是独立于 search_knowledge 的第二套检索——用图片层嵌入（不需要 PDF 有文字层，
    扫描件也能导航），返回 库/[相对路径]（绝对路径）第N页 + 相似度分，按分降序。
    适合给有看图能力的模型：拿到绝对路径与页码后直读原 PDF 对应页。

    libraries/exclude 语义同 search_knowledge：为空=默认库，"all"=全部库，
    "A,B" 多库并查，exclude="B" 反选。先调 list_libraries 查看库名。
    需要已开启 wemm_backend（Config 视觉导航）并已跑过 WEMM 页索引。"""
    try:
        cfg_backend, wemm_url, _ = _wemm_cfg()
        if cfg_backend == "off":
            return ("（WEMM 视觉导航未开启：Config→视觉导航（WEMM）将 wemm_backend"
                    " 设为 on/local，然后重新调用本工具——看图服务会按需自动拉起，"
                    "页索引请跑 python wemm_indexer.py --backend on。注意："
                    "reindex_knowledge 只重建文字索引，不建 WEMM 页库。）")
        # 问题41 显存互斥：本进程的 bge-m3/reranker 对页级导航毫无用处，先释放
        # 给 WEMM 让路（下次文字检索懒加载回来），再按需拉起看图服务
        try:
            release_reranker()
            release_model()
            ok, detail = gpu_arbiter.ensure_server()
            if not ok:
                return f"（看图服务拉起失败：{detail}）"
        except Exception as e:
            log(f"navigate 前的 GPU 让路/拉起失败（忽略，按原路径继续）：{e}")
        entries = resolve_entries(libraries, exclude,
                                  defaults=CFG.get("default_libraries", []))
        names = [e["name"] for e in entries]
        results, err = wemm_search(query, libraries=names, top_k=top_k)
        if not results:
            head = f"（{err}）" if err else "（WEMM 页索引为空或未命中。"
            if not err:
                head += ("先在命令行运行 python wemm_indexer.py --backend on 建 WEMM"
                         " 页索引（reindex_knowledge 不建页库），或换用"
                         " search_knowledge 文本检索。）")
            return head
        lines = [f"页级导航（{query!r}）—— 最相关的 PDF 与页码："]
        for lib, rel, abs_path, page, score in results:
            lines.append(f"  [{lib}] {rel}（{abs_path}）第{page + 1}页  相似度 {score:.3f}")
        if err:
            lines.append(f"（部分库查询异常：{err}）")
        return "\n".join(lines)
    except ValueError as e:
        return f"（{e}）"
    except Exception as e:
        import traceback
        log(f"navigate_knowledge 失败：{e}\n{traceback.format_exc()}")
        return f"（页级导航失败：{e}）"


def _read_source_text(cfg, rel, abs_path):
    """read_document 读正文：文本类直接读源文件；pdf/docx 只读既有提取缓存。
    返回 (正文|None, route_or_reason)。命中时第二项是产出路由（local /
    mineru-text / ocr:mineru-cloud / ocr:mineru-local / 源文件），失败时是原因。
    """
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if ext in ("md", "txt", "markdown"):
        try:
            return Path(abs_path).read_text(encoding="utf-8", errors="replace"), "源文件"
        except OSError:
            return None, "读取失败"
    if ext in ("pdf", "docx"):
        from extractors import read_cached_markdown
        md, route = read_cached_markdown(abs_path)
        if md is None:
            return None, "未提取" if route == "not-cached" else route
        return md, route
    return None, "不支持该格式"


_ROUTE_LABELS = {
    "源文件": "源文件直读",
    "local": "本地提取",
    "mineru-text": "MinerU 云端（文字层版面识别）",
    "ocr:mineru-cloud": "MinerU 云端 OCR（扫描件）",
    "ocr:mineru-local": "MinerU 本地 OCR（扫描件）",
}


@server.tool()
def read_document(library: str, path: str) -> str:
    """读取某文档的完整正文 + 给出源文件绝对路径。用于检索命中后精读全部内容：
    纯文本模型用这里拿正文（pdf/docx 返回其已提取的 Markdown 全文），有看图能力的
    模型可直接拿绝对路径去读原 PDF 对应页。返回抬头带 字数 与 产出方式，正文
    不截断（本工具的定位就是交付全文，长讲义也不会缺尾巴）。

    library：单个库名（同 search_knowledge 语义，不支持 "all"）。path：库内相对路径
    或不含扩展名的标题（如 "ManometerEquation"）。pdf/docx 只交付"已索引/已缓存的
    Markdown"——若该文件尚未被提取过，会提示先索引，绝不后台触发扫描件 OCR 或云端
    调用。"""
    try:
        entries = resolve_entries(library, "", defaults=CFG.get("default_libraries", []))
    except ValueError as e:
        return f"（{e}）"
    if len(entries) > 1:
        names = "、".join(e["name"] for e in entries)
        return f"（library 需指定单个库，当前默认解析出多个：{names}；请显式传 library 参数指定其一）"
    cfg = effective_config(entries[0])
    # 在文字索引 meta 里定位文件（库内相对路径 或 不含扩展名标题）
    meta = load_meta(meta_path(cfg["name"]))
    rel = None
    if path in meta:
        rel = path
    else:
        for k in meta:
            if k.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() == path.lower().lstrip("./\\"):
                rel = k
                break
    if rel is None:
        return (f"（在库「{cfg['name']}」中找不到 \"{path}\"。先用 search_knowledge 或"
                f" navigate_knowledge 找到源文件，再看库内的相对路径。）")
    # 问题44：被用户显式排除的文件对 RAG 系统完全不存在——read_document 也拒绝
    # （只拦显式取消；中性但格式未启用的文件不作拦，那只是过滤默认而非排除决定）
    if resolve_selection(cfg.get("selection_in"), cfg.get("selection_out"), rel) == "out":
        return (f"（「{cfg['name']}/{rel}」已被用户排除出知识库（勾选范围），"
                f"RAG 流程不可访问。如确需读取请让用户在 GUI 库管理→勾选范围中恢复。）")
    abs_path = os.path.normpath(os.path.join(cfg["path"], rel))
    text, route = _read_source_text(cfg, rel, abs_path)
    if text is None:
        head = f"「{cfg['name']}/{rel}」\n源文件绝对路径：{abs_path}\n"
        return head + (f"（该文档正文不可用：{route}。若是 pdf/docx，请先索引该文件"
                       f"生成 Markdown，再重试。）")
    how = _ROUTE_LABELS.get(route, route)
    head = (f"「{cfg['name']}/{rel}」\n源文件绝对路径：{abs_path}\n"
            f"字数：{len(text)}　产出方式：{how}\n")
    return f"{head}\n{text}"


@server.tool()
def find_duplicates(library: str = "", threshold: float = None) -> str:
    """近似文档去重（只读建议，绝不删除/移动文件）：找出库内"内容几乎相同"的重复
    文档（同一课件多份拷贝、docx 及其转出的 PDF、重复讲义），返回重复组与相似度，
    供你决定是否清理/合并，避免检索反复命中同一段内容。比较的是提取出的文字内容
    （文本级 MinHash+LSH），不产生向量、不改索引、不后台触发 OCR/云端（pdf/docx
    只读已有提取缓存，未提取的文件会跳过并计数）。

    library：库名列表（逗号分隔）或 "all"（默认=全部注册库）。threshold：相似度
    阈值（0~1，默认 0.8），越高越严格。"""
    try:
        thr = threshold if threshold is not None else DEDUP_THRESHOLD
        if not (0.0 < thr <= 1.0):
            return f"（threshold 必须在 (0, 1] 区间，收到 {thr}。）"
        reload_config()
        if library in ("", "all"):
            entries = load_registry()
        else:
            entries = resolve_entries(library, "")
        if not entries:
            return "（没有已注册的库。）"
        blocks = []
        for e in entries:
            cfg = effective_config(e)
            clusters, stats = dedup_find(cfg, threshold=thr)
            blocks.append(dedup_format_report(cfg, clusters, stats, thr))
        return "\n\n".join(blocks)
    except Exception as exc:
        import traceback
        log(f"find_duplicates 失败：{exc}\n{traceback.format_exc()}")
        return f"（近似去重失败：{exc}）"


def _dedup_report(name, clusters, stats, threshold):
    """（已由 dedup.format_report 取代，保留薄壳防外部引用；勿新增调用。）"""
    return dedup_format_report({"name": name}, clusters, stats, threshold)


# 终态 reason → 人类可读解释（索引失败溯源用；与 index.py REASON_* 常量同串）
_TERMINAL_LABELS = {
    "unreadable": "无法读取（疑似损坏/无权限，指纹两轮才判稳）",
    "extract-failed": "提取失败（损坏/加密/云端失败，可按需重试）",
    "empty": "空内容（打开正常但没有可提取的正文）",
    "tbd": "命中 [TBD] 占位（正文还没写完，写完会自动转正）",
    "scanned": "扫描件待 OCR（后端 none 时跳过；开启 MinerU 云端后会自动重试）",
    "not-pdf": "非 PDF（仅 WEMM 页库语境）",
}


@server.tool()
def index_failures(library: str = "", include_ok: bool = False) -> str:
    """索引失败溯源（只读诊断）：逐文件列出库内"没转成/没索引上"的文档及原因。

    解决"什么文件转不到、为什么、该不该修"：读每个库的文字索引 meta（index_meta_*.json），
    把落成终态（unreadable / extract-failed / empty / tbd / scanned）的文件按原因分组列出，
    每行给 相对路径 + 原因含义 + 是否会自动重试。你看一眼就知道哪份课件没进库、卡在哪。

    library：库名（或逗号分隔多库，"all"=全部；缺省=全部）。include_ok=True 时连同正常索引
    的文件计数一起列出（便于看出总数 vs 失败数）。只读，绝不触发重新索引或模型加载。"""
    try:
        if library in ("", "all"):
            entries = load_registry()
        else:
            entries = resolve_entries(library, "")
        if not entries:
            return "（没有已注册的库。）"
        sig = current_backend_sig()
        blocks = []
        for e in entries:
            cfg = effective_config(e)
            meta = load_meta(meta_path(cfg["name"]))
            reasons = {}
            ok = 0
            for rel, info in meta.items():
                if not isinstance(info, dict):
                    continue
                r = info.get("reason")
                if r:
                    # 与 index._backend_changed 同一谓词：签名不符（含旧条目缺
                    # xsrc）= 下轮真的会重试，别给 AI 一个与实际行为相反的结论
                    will_retry = _backend_changed(info, sig)
                    reasons.setdefault(r, []).append((rel, bool(will_retry)))
                elif info.get("xfail") or info.get("tbd"):
                    r2 = info.get("reason") or "unknown"
                    reasons.setdefault(r2, []).append((rel, False))
                else:
                    ok += 1
            blocks.append(_failures_report(cfg["name"], reasons, ok, sig, include_ok))
        return "\n\n".join(blocks)
    except Exception as exc:
        import traceback
        log(f"index_failures 失败：{exc}\n{traceback.format_exc()}")
        return f"（索引失败溯源失败：{exc}）"


def _failures_report(name, reasons, ok, sig, include_ok):
    lines = [f"库「{name}」索引失败溯源（当前 OCR 能力签名：{sig}）："]
    if not reasons:
        lines.append("  ✔ 没有落成败终态的文件（全部成功索引）。")
        if include_ok:
            lines.append(f"  正常索引文件：{ok} 份。")
        return "\n".join(lines)
    for r in ("unreadable", "extract-failed", "empty", "tbd", "scanned", "not-pdf"):
        items = reasons.pop(r, None)
        if not items:
            continue
        label = _TERMINAL_LABELS.get(r, r)
        lines.append(f"  〔{r}〕{len(items)} 份 —— {label}")
        for rel, will_retry in items:
            mark = "（〆 下轮将自动重试转正）" if will_retry else ""
            lines.append(f"      · {rel}{mark}")
    # 非常规 reason（如空串折叠出的 unknown）也要露面，绝不让文件从报告中无声消失
    for r, items in reasons.items():
        lines.append(f"  〔{r}〕{len(items)} 份 —— 未知终态（请带此输出反馈排查）")
        for rel, will_retry in items:
            lines.append(f"      · {rel}")
    if include_ok:
        lines.append(f"  正常索引文件：{ok} 份（不在上述失败清单里）")
    lines.append("  提示：unreadable/extract-failed 可删掉该文件重试或修复源文件；"
                 "scanned 需在 Config 开启 MinerU 云端 OCR 才会自动转正。")
    return "\n".join(lines)


def _wemm_cfg():
    """读 WEMM 相关配置的当前值（reload_config 现读，不用进程启动时的快照）。

    长驻 MCP server 的 CFG 是 import 时加载的副本；用户中途在 Config 里开关了
    wemm_backend / 改 DPI 后，这里必须拿到最新值，否则导航/状态会一直停在旧开关上。
    reload_config 原地更新共享 CFG——随后 wemm_search 等内部 `from config import CFG`
    的读取也拿到同一份新值，外层放行、内层拒绝的自相矛盾不再可能。
    """
    reload_config()
    return (CFG.get("wemm_backend", "off"),
            CFG.get("wemm_url"),
            CFG.get("wemm_render_dpi"))


@server.tool()
def wemm_status() -> str:
    """WEMM 页级视觉导航状态（用户可确认手段）：看它到底建没建、生效没生效。

    读每个库的 WEMM 页库 meta（wemm_meta_*.json）+ 配置后报答：
      后端开关（wemm_backend off/on-local）、是否已跑过页索引（几个 PDF、几页向量）、
      当前索引的 PDF 里有哪些失败（未渲染成功）、看图服务是否存活。
    用这个一眼确认"WEMM 到底能不能用"，而不是靠猜测。只读，不启动服务、不加载模型。"""
    try:
        from wemm_indexer import load_wemm_meta, wemm_meta_path
        from wemm_retriever import health
        cfg_backend, wemm_url, wemm_dpi = _wemm_cfg()
        dpi_txt = f"，渲染分辨率 {wemm_dpi} DPI" if wemm_dpi else ""
        lines = [f"WEMM 页级视觉导航状态（wemm_backend={cfg_backend}{dpi_txt}）："]
        if cfg_backend not in ("on", "local"):
            lines.append("  ⚠ 后端未开启（off）——navigate_knowledge 不可用。"
                         "Config→视觉导航（WEMM）设为 on/local 后重新查询即可"
                         "（服务按需自动拉起）；跑一次索引会自动建页库。")
            return "\n".join(lines)
        # 服务存活
        try:
            h = health(wemm_url)
            lines.append(f"  ✔ 看图服务存活：{wemm_url}（{h.get('model', '?')}，"
                         f"设备 {h.get('device', '?')}）")
        except Exception as e:
            lines.append(f"  ✘ 看图服务未运行：{type(e).__name__}"
                         f"（下次 navigate_knowledge 会自动拉起，或手动 python wemm_server.py）")
        # 每库页索引情况
        any_index = False
        for e in load_registry():
            cfg = effective_config(e)
            mp = wemm_meta_path(cfg["name"])
            meta = load_wemm_meta(mp)
            files = {k: v for k, v in meta.items()
                     if k != "_version" and isinstance(v, dict)}
            if not files:
                lines.append(f"  · 库「{cfg['name']}」：尚未建 WEMM 页索引"
                             f"（下次索引自动建；或立即跑 python wemm_indexer.py）")
                continue
            any_index = True
            pages = sum(v.get("pages", 0) for v in files.values()
                        if not (v.get("tbd") or v.get("xfail")))
            failed = [k for k, v in files.items() if (v.get("tbd") or v.get("xfail"))]
            hdr = f"  · 库「{cfg['name']}」：已建页索引 —— {len(files)} 份 PDF、{pages} 页向量"
            if failed:
                hdr += f"，其中 {len(failed)} 份渲染失败待重试"
            lines.append(hdr)
            for k in failed[:10]:
                lines.append(f"      ✗ {k}")
            if len(failed) > 10:
                lines.append(f"      … 其余 {len(failed) - 10} 份省略")
        if not any_index:
            lines.append("  ⚠ 所有库都还没建 WEMM 页索引：需先跑 python wemm_indexer.py "
                         "--library <库名> --backend on 建库，navigate_knowledge 才有东西可导航。")
        return "\n".join(lines)
    except Exception as exc:
        import traceback
        log(f"wemm_status 失败：{exc}\n{traceback.format_exc()}")
        return f"（WEMM 状态查询失败：{exc}）"


if __name__ == "__main__":
    acquire_singleton()
    threading.Thread(target=_gpu_idle_unload_daemon, daemon=True,
                     name="gpu-idle-unload").start()
    server.run("stdio")
