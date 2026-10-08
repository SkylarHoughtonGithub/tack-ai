"""
LangGraph approval flow stub.

Implements the approval gate as an explicit LangGraph graph with a
MemorySaver checkpointer and interrupt()-based human-in-the-loop step.
Retained for reference; the main approval path uses pydantic_graph.

Usage:
    uv run python -m tack_ai.langgraph_approval
"""

import asyncio
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from tack_ai.policy import policy_check


class ApprovalState(TypedDict):
    tool_name: str
    args: dict[str, Any]
    policy_decision: str | None
    approved: bool | None
    result: str | None


# ── Nodes ─────────────────────────────────────────────────────────────────────


async def _check_policy(state: ApprovalState) -> dict:
    decision = await policy_check(state["tool_name"], state["args"])
    return {"policy_decision": decision.value}


def _route_after_policy(state: ApprovalState) -> str:
    d = state.get("policy_decision")
    if d == "allow":
        return "execute_tool"
    if d == "require_approval":
        return "request_approval"
    return END


def _request_approval(state: ApprovalState) -> dict:
    """Interrupt graph and surface the pending approval to the caller.

    The caller resumes by invoking the graph with:
        Command(resume={"approved": True})   # or False
    """
    human_input = interrupt(
        {
            "type": "approval_required",
            "tool_name": state["tool_name"],
            "args": state["args"],
        }
    )
    approved = (
        human_input.get("approved", False) if isinstance(human_input, dict) else bool(human_input)
    )
    return {"approved": approved}


def _route_after_approval(state: ApprovalState) -> str:
    return "execute_tool" if state.get("approved") else END


def _execute_tool(state: ApprovalState) -> dict:
    # Stub: in production replace with real tool dispatch.
    return {"result": f"executed {state['tool_name']} with {state['args']}"}


# ── Graph ─────────────────────────────────────────────────────────────────────


def build_graph() -> StateGraph:
    g = StateGraph(ApprovalState)

    g.add_node("check_policy", _check_policy)
    g.add_node("request_approval", _request_approval)
    g.add_node("execute_tool", _execute_tool)

    g.set_entry_point("check_policy")

    g.add_conditional_edges(
        "check_policy",
        _route_after_policy,
        {"execute_tool": "execute_tool", "request_approval": "request_approval", END: END},
    )
    g.add_conditional_edges(
        "request_approval",
        _route_after_approval,
        {"execute_tool": "execute_tool", END: END},
    )
    g.add_edge("execute_tool", END)

    return g


def compiled_graph(checkpointer=None):
    g = build_graph()
    return g.compile(checkpointer=checkpointer)


# ── Demo ──────────────────────────────────────────────────────────────────────


async def demo() -> None:
    """Run the same approval scenario through both implementations and compare."""
    checkpointer = MemorySaver()
    graph = compiled_graph(checkpointer=checkpointer)

    # Scenario: write_file outside drafts/ — requires approval in OPA.
    tool_name = "write_file"
    args = {"path": "src/secret.py", "content": "# generated"}
    config = {"configurable": {"thread_id": "demo-1"}}

    print("=" * 60)
    print("LangGraph approval flow (stub)")
    print("=" * 60)
    print(f"\nTool:    {tool_name}")
    print(f"Args:    {args}")

    # First invocation — runs until interrupt() fires inside request_approval.
    # In LangGraph 1.2+, ainvoke() returns (no exception) when interrupted;
    # check pending.next to distinguish interrupt from normal completion.
    await graph.ainvoke(
        {
            "tool_name": tool_name,
            "args": args,
            "policy_decision": None,
            "approved": None,
            "result": None,
        },
        config=config,
    )

    pending = graph.get_state(config)
    if pending.next:
        # Graph is paused at an interrupt — extract payload from task list.
        payload: dict = {}
        for task in pending.tasks:
            for intr in task.interrupts:
                payload = intr.value if isinstance(intr.value, dict) else {}

        print(f"\n[INTERRUPT] {payload.get('type', 'approval_required')}")
        print(f"  → Graph paused at:            {pending.next}")
        print(f"  → Awaiting approval for tool: {payload.get('tool_name')}")
        print(f"  → Args: {payload.get('args')}")

        user_approved = True
        print(f"\n[HUMAN] Approve? → {'yes' if user_approved else 'no'}")

        final = await graph.ainvoke(
            Command(resume={"approved": user_approved}),
            config=config,
        )
        print(f"\n[RESULT] {final.get('result')}")
    else:
        state = graph.get_state(config).values
        decision = state.get("policy_decision")
        print(f"\n[POLICY] {decision}")
        print(f"[RESULT] {state.get('result') or 'blocked by policy'}")


if __name__ == "__main__":
    asyncio.run(demo())
