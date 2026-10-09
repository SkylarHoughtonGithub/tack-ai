"""Assembled coding workflow graph and public entry point."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic_graph import GraphBuilder

from tack_ai.graph.nodes import EditNode, GateNode, PlanNode, TestNode
from tack_ai.graph.state import CodingApprovalRequired, CodingDeps, CodingResult, CodingState

if TYPE_CHECKING:
    from tack_ai.core.config import Settings

# Build once at import time. validate_graph_structure=False matches pydantic_ai's own
# internal pattern — BaseNode return-type edges are checked at runtime, not statically.
_builder: GraphBuilder = GraphBuilder(
    state_type=CodingState,
    deps_type=CodingDeps,
    input_type=PlanNode,
    output_type=CodingResult,
)
_builder.add(
    _builder.edge_from(_builder.start_node).to(PlanNode),
    _builder.node(PlanNode),
    _builder.node(EditNode),
    _builder.node(TestNode),
    _builder.node(GateNode),
)
coding_graph = _builder.build(validate_graph_structure=False)


async def run_coding_task(
    task: str,
    *,
    settings: Settings,
    budget_usd: float = 1.0,
    run_id: str | None = None,
) -> CodingResult:
    """Run the coding workflow for *task* and return the final result.

    Raises:
        CodingApprovalRequired: when the gate reviewer escalates, the budget is
            exhausted, or the maximum iteration count is reached.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
    state = CodingState(task=task, run_id=run_id, budget_remaining_usd=budget_usd)
    deps = CodingDeps(settings=settings)
    return await coding_graph.run(state=state, deps=deps, inputs=PlanNode())


__all__ = ["CodingApprovalRequired", "coding_graph", "run_coding_task"]
