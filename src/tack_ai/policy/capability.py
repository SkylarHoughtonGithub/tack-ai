"""
PolicyEnforcementCapability — PydanticAI Capability that gates every tool call
through the configured policy engine (OPA or Cedar) before execution.

Replaces per-tool enforce() calls. Tool bodies contain only business logic.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.exceptions import ToolFailed

from tack_ai.policy.engine import enforce


class PolicyEnforcementCapability(AbstractCapability):
    """Intercepts tool execution and enforces OPA/Cedar policy before the tool runs."""

    async def wrap_tool_execute(
        self,
        ctx: Any,
        *,
        call: Any,
        tool_def: Any,
        args: Any,
        handler: Any,
    ) -> Any:
        ok, reason = await enforce(call.tool_name, call.args_as_dict())
        if not ok:
            raise ToolFailed(reason)
        return await handler(args)
