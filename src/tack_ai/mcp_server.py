"""
MCP server exposing tack-ai tools to external MCP clients (Claude Desktop, other agents).
Each tool is protected by the same OPA policy as the native agent.

Run (stdio, for Claude Desktop):
    uv run python -m tack_ai.mcp_server

Run (SSE, for programmatic clients):
    uv run python -m tack_ai.mcp_server --sse
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

from tack_ai.core.config import Settings
from tack_ai.policy import enforce

settings = Settings()
mcp = MCPServer(
    "tack-ai-tools",
    description="Policy-governed document search and email drafting.",
)


@mcp.tool()
async def search_documents(query: str, user: str = "user") -> str:
    """Search the indexed document store and return the most relevant passages."""
    ok, reason = await enforce("search_documents", {"query": query[:80]}, user)
    if not ok:
        return f"Error: {reason}."
    if not settings.database_url or not settings.openai_api_key:
        return "Document search not configured (DATABASE_URL or OPENAI_API_KEY missing)."
    if not settings.openfga_store_id or not settings.openfga_model_id:
        return "Document search not configured (OPENFGA_STORE_ID or OPENFGA_MODEL_ID missing)."

    from tack_ai.memory.retrieval import format_for_prompt
    from tack_ai.memory.retrieval import search_documents as _search

    results = await _search(
        query=query,
        user=user,
        db_url=settings.database_url,
        openai_api_key=settings.get_key("openai"),
    )
    return format_for_prompt(results)


@mcp.tool()
async def draft_email(to: str, subject: str, body: str) -> str:
    """Draft an email without sending it."""
    ok, reason = await enforce("draft_email", {"to": to, "subject": subject})
    if not ok:
        return f"Error: {reason}."
    return f"[draft] To: {to}\nSubject: {subject}\n\n{body}"


def main() -> None:
    if "--sse" in sys.argv:
        mcp.run(transport="sse", host="0.0.0.0", port=8083)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
