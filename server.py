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
def search_knowledge(query: str, top_k: int = 5, folder: str = "") -> str:
    """语义搜索 Obsidian 知识库（混合检索：向量语义 + 关键词）。query 为自然语言问题；folder 可按 Vault 内子目录过滤（如 ROCKETRY 或 AI Knowledge System）；返回最相关的笔记段落与来源文件路径。"""
    note = ensure_fresh()
    return note + hybrid_search(query, top_k=top_k, folder=folder)


@server.tool()
def reindex_knowledge() -> str:
    """增量重建知识库索引：扫描 Vault，只对内容变化的文件重新嵌入（基于内容 hash 对比）。在写入/修改了笔记后调用此工具保持检索最新。"""
    index_vault(VAULT, incremental=True)
    reset_bm25_index()
    return "索引已更新。"


if __name__ == "__main__":
    server.run("stdio")
