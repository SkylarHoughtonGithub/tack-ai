"""
MCP policy gateway.

Sits between agents and MCP servers. Every tool call — regardless of which
agent sent it — passes policy_check() before being forwarded to the upstream
filesystem MCP server.

Architecture:
    Agent → MCPToolset("http://localhost:8082/mcp")
          → Gateway (this process, port 8082)
          → OPA check
          → Upstream filesystem MCP server (npx stdio subprocess)

Run:
    uv run python -m tack_ai.mcp_gateway

Requires Node.js:  npx -y @modelcontextprotocol/server-filesystem
"""

from __future__ import annotations

import contextlib
import inspect
import sys
from pathlib import Path
from typing import Any, Callable

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

from tack_ai.core.config import Settings
from tack_ai.policy import enforce

GATEWAY_PORT = 8082
PROJECT_ROOT = Path(__file__).resolve().parents[2]
settings = Settings()

_client: Client | None = None


def _json_type(schema: dict) -> type:
    return {"string": str, "integer": int, "number": float, "boolean": bool}.get(
        schema.get("type", "string"), str
    )


def _make_proxy(tool_name: str, tool_description: str, input_schema: dict) -> Callable:
    """Build a typed async proxy function for an upstream tool."""
    properties = input_schema.get("properties", {})
    required_fields = set(input_schema.get("required", []))

    async def _proxy(**kwargs: Any) -> str:
        ok, reason = await enforce(tool_name, kwargs)
        if not ok:
            print(f"[gateway] DENIED '{tool_name}': {reason}", file=sys.stderr)
            return f"[gateway] Denied: {reason}"
        if _client is None:
            return "[gateway] Upstream not connected."
        result = await _client.call_tool(tool_name, kwargs)
        parts = [c.text for c in result.content if hasattr(c, "text")]
        return "\n".join(parts) if parts else "(no output)"

    params = []
    annotations: dict[str, Any] = {"return": str}
    for name, schema in properties.items():
        py_type = _json_type(schema)
        annotations[name] = py_type
        default = inspect.Parameter.empty if name in required_fields else None
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=py_type,
                default=default,
            )
        )

    _proxy.__name__ = tool_name
    _proxy.__doc__ = tool_description or f"Proxied: {tool_name}"
    _proxy.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    _proxy.__annotations__ = annotations
    return _proxy


@contextlib.asynccontextmanager
async def _lifespan(server: MCPServer):
    global _client
    fs_root = str(settings.mcp_filesystem_root or PROJECT_ROOT)
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", fs_root],
    )
    try:
        async with Client(params, mode="legacy") as c:
            _client = c
            tools_result = await c.list_tools()
            for tool in tools_result.tools:
                server.add_tool(
                    _make_proxy(
                        tool.name,
                        tool.description or "",
                        tool.input_schema or {},
                    ),
                    name=tool.name,
                    description=tool.description or "",
                )
            print(
                f"[gateway] Upstream ready — {len(tools_result.tools)} tool(s) proxied.",
                file=sys.stderr,
            )
            yield
    except Exception as exc:
        print(f"[gateway] Upstream failed: {exc}", file=sys.stderr)
        yield


gateway = MCPServer("tack-ai-gateway", lifespan=_lifespan)


if __name__ == "__main__":
    try:
        gateway.run(transport="sse", host="0.0.0.0", port=GATEWAY_PORT)
    except KeyboardInterrupt:
        pass
