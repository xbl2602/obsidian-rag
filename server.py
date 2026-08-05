"""server.py — MCP server，暴露 search_knowledge / reindex_knowledge 工具给 opencode。
用法：python server.py（stdio 模式，由 opencode 自动拉起）
"""
from mcp.server import MCPServer

from index import VAULT, index_vault, kb_stale, log
from retriever import hybrid_search, reset_bm25_index

server = MCPServer("obsidian-rag", title="Obsidian RAG", version="0.1.0")


def ensure_fresh():
    """指纹检查：Vault 有变化则自动增量重建索引，返回提示文本。

    任何一步失败都降级为"用旧索引检索 + 提示"，绝不让检索整体失败。
    """
    try:
        stale, stats = kb_stale(VAULT)
        if not stale:
            return ""
        index_vault(VAULT, incremental=True)
        reset_bm25_index()
        parts = []
        if stats["changed"]:
            parts.append(f"{stats['changed']} 个文件变更")
        if stats["added"]:
            parts.append(f"新增 {stats['added']} 个文件")
        if stats["removed"]:
            parts.append(f"删除 {stats['removed']} 个文件")
        return f"（检测到{'、'.join(parts)}，已自动更新索引）\n\n"
    except Exception as e:
        log(f"自动同步索引失败（使用旧索引继续）：{e}")
        return f"（检测到 Vault 变化，但自动更新索引失败：{e}；以下为旧索引结果）\n\n"


@server.tool()
def search_knowledge(query: str, top_k: int = 5, folder: str = "", include_body: bool = True) -> str:
    """语义搜索 Obsidian 知识库（混合检索：向量语义 + 关键词）。query 为自然语言问题；folder 可按 Vault 内子目录过滤（如 ROCKETRY 或 AI Knowledge System，边界校验的前缀匹配，须是完整目录名）；返回最相关的笔记段落与来源文件路径，来源行带 [块 k/N] 位置标记。include_body=False 时只返回来源清单（文件名+标题+块位置，无正文），用于两阶段检索：先低成本枚举全量候选，再对命中少数精读。注意：会话首次调用或 Vault 变更后首次调用需加载模型并重建关键词索引，耗时数十秒属正常。"""
    try:
        note = ensure_fresh()
        return note + hybrid_search(query, top_k=top_k, folder=folder, include_body=include_body)
    except Exception as e:
        log(f"search_knowledge 失败：{e}")
        return f"（检索失败：{e}；请稍后重试或检查 Vault/索引状态）"


@server.tool()
def reindex_knowledge() -> str:
    """增量重建索引（扫描 Vault，只对内容变化的文件重新嵌入）。一般无需手动调用：每次搜索前自动检测 Vault 变化并增量同步；仅在自动同步失败提示、或用户明确要求立即刷新时使用。"""
    try:
        index_vault(VAULT, incremental=True)
        reset_bm25_index()
        return "索引已更新。"
    except Exception as e:
        log(f"reindex_knowledge 失败：{e}")
        return f"（重建索引失败：{e}；旧索引保持可用）"


if __name__ == "__main__":
    server.run("stdio")
