"""pydantic_graph nodes for the coding workflow.

Flow: PlanNode → EditNode → TestNode → GateNode
           ↑__________________________|  (on "fix", up to MAX_ITERATIONS)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import genai_prices
from pydantic_ai.models.fallback import FallbackModel
from pydantic_graph import BaseNode, End, GraphRunContext

from tack_ai.audit import append
from tack_ai.core.models import AuditRecord, PolicyDecision
from tack_ai.core.router import build_model, load_model_config, make_run_settings
from tack_ai.graph.state import (
    CodingApprovalRequired,
    CodingDeps,
    CodingResult,
    CodingState,
    EditPlanStep,
    EditResult,
    GateDecision,
    _GateDecisionJev,
)

if TYPE_CHECKING:
    pass

MAX_ITERATIONS = 3


def _debit_budget(state: CodingState, usage: object, model_str: str) -> None:
    if usage is None:
        return
    model_name = model_str.split(":", 1)[1]
    try:
        calc = genai_prices.calc_price(usage, model_name)
        cost = float(calc.total_price)
    except Exception:
        cost = (
            (getattr(usage, "input_tokens", 0) or 0) * 3.0
            + (getattr(usage, "output_tokens", 0) or 0) * 15.0
        ) / 1_000_000
    state.budget_remaining_usd -= cost


# ── PlanNode ──────────────────────────────────────────────────────────────────


class PlanNode(BaseNode[CodingState, CodingDeps, CodingResult]):
    async def run(self, ctx: GraphRunContext[CodingState, CodingDeps]) -> EditNode:
        from tack_ai.graph.agents import planner_agent  # noqa: PLC0415

        mc = load_model_config()
        model_str = mc["tiers"]["simple"]
        result = await planner_agent.run(
            f"Task: {ctx.state.task}\nIteration: {ctx.state.iteration}",
            model=build_model(model_str, ctx.deps.settings),
            model_settings=make_run_settings(model_str),
            deps=ctx.deps,
            message_history=ctx.state.history,
        )
        ctx.state.plan = result.output.steps
        ctx.state.history.extend(result.new_messages())
        _debit_budget(ctx.state, result.usage, model_str)

        await append(
            AuditRecord(
                run_id=ctx.state.run_id,
                actor="planner_agent",
                event_type="tool_call",
                graph_node="plan",
                tool_name="plan",
                tool_args={"task": ctx.state.task, "iteration": ctx.state.iteration},
                outcome=f"{len(ctx.state.plan)} steps planned",
            )
        )
        return EditNode()


# ── EditNode ──────────────────────────────────────────────────────────────────


class EditNode(BaseNode[CodingState, CodingDeps, CodingResult]):
    async def run(self, ctx: GraphRunContext[CodingState, CodingDeps]) -> TestNode:
        from tack_ai.graph.agents import edit_agent  # noqa: PLC0415

        mc = load_model_config()
        model_str = mc["tiers"]["general"]
        history_snapshot = list(ctx.state.history)

        async def _edit_one(step: EditPlanStep) -> tuple[EditResult, list]:
            r = await edit_agent.run(
                f"File: {step.file_path}\nInstruction: {step.instruction}",
                model=build_model(model_str, ctx.deps.settings),
                model_settings=make_run_settings(model_str),
                deps=ctx.deps,
                message_history=history_snapshot,
            )
            _debit_budget(ctx.state, r.usage, model_str)
            return r.output, r.new_messages()

        results = await asyncio.gather(*[_edit_one(step) for step in ctx.state.plan])
        ctx.state.edits = [r[0] for r in results]
        for _, msgs in results:
            ctx.state.history.extend(msgs)

        await append(
            AuditRecord(
                run_id=ctx.state.run_id,
                actor="edit_agent",
                event_type="tool_call",
                graph_node="edit",
                tool_name="edit",
                tool_args={"files": [s.file_path for s in ctx.state.plan]},
                outcome=f"{len(ctx.state.edits)} file(s) edited",
            )
        )
        return TestNode()


# ── TestNode ──────────────────────────────────────────────────────────────────


class TestNode(BaseNode[CodingState, CodingDeps, CodingResult]):
    async def run(self, ctx: GraphRunContext[CodingState, CodingDeps]) -> GateNode:
        from tack_ai.graph.agents import test_agent  # noqa: PLC0415

        mc = load_model_config()
        model_str = mc["tiers"]["general"]
        edited = [e.file_path for e in ctx.state.edits]
        result = await test_agent.run(
            f"Run tests after editing: {edited}",
            model=build_model(model_str, ctx.deps.settings),
            model_settings=make_run_settings(model_str),
            deps=ctx.deps,
            message_history=ctx.state.history,
        )
        ctx.state.test_result = result.output
        ctx.state.history.extend(result.new_messages())
        _debit_budget(ctx.state, result.usage, model_str)

        await append(
            AuditRecord(
                run_id=ctx.state.run_id,
                actor="test_agent",
                event_type="tool_call",
                graph_node="test",
                tool_name="run_tests",
                tool_args={"files": edited},
                outcome="passed" if result.output.passed else "failed",
            )
        )
        return GateNode()


# ── GateNode ──────────────────────────────────────────────────────────────────


class GateNode(BaseNode[CodingState, CodingDeps, CodingResult]):
    async def run(
        self, ctx: GraphRunContext[CodingState, CodingDeps]
    ) -> PlanNode | End[CodingResult]:
        from tack_ai.graph.agents import review_agent  # noqa: PLC0415

        mc = load_model_config()
        # Jev is purpose-built for structured decisions — use it when the key is set.
        # Fall back to the simple LLM tier so the gate always works without a TypeSafe account.
        model_str = (
            "typesafe:jev-latest" if ctx.deps.settings.typesafe_api_key else mc["tiers"]["simple"]
        )

        tr = ctx.state.test_result
        test_summary = (
            f"Tests: {'PASSED' if tr.passed else 'FAILED'}\n"
            f"Failed tests: {', '.join(tr.failed_tests) or 'none'}\n"
            f"Output:\n{tr.output[:1000]}"
            if tr
            else "Tests: not run"
        )
        prompt = (
            f"Task: {ctx.state.task}\n"
            f"Iteration: {ctx.state.iteration + 1}/{MAX_ITERATIONS}\n"
            f"Files edited: {[e.file_path for e in ctx.state.edits]}\n"
            f"{test_summary}\n"
            f"Budget remaining: ${ctx.state.budget_remaining_usd:.4f}"
        )

        use_jev = bool(ctx.deps.settings.typesafe_api_key)
        if use_jev:
            # Jev handles the final Choice (fix/escalate/pass); the LLM handles tool calls
            # (read_file args etc.) that Jev cannot fill. FallbackModel routes each step
            # to whichever model can handle it.
            llm_str = mc["tiers"]["simple"]
            model = FallbackModel(
                build_model(model_str, ctx.deps.settings),
                build_model(llm_str, ctx.deps.settings),
            )
        else:
            model = build_model(model_str, ctx.deps.settings)

        try:
            result = await review_agent.run(
                prompt,
                model=model,
                model_settings=make_run_settings(model_str),
                output_type=_GateDecisionJev if use_jev else GateDecision,
                deps=ctx.deps,
            )
            if use_jev:
                raw = cast(_GateDecisionJev, result.output)
                decision: GateDecision = GateDecision(action=raw.action, confidence=raw.confidence)
            else:
                decision = cast(GateDecision, result.output)
        except Exception as exc:
            if use_jev and getattr(exc, "status_code", None) in (401, 403):
                # Bad/missing TypeSafe key — fall back to LLM tier entirely
                llm_str = mc["tiers"]["simple"]
                result = await review_agent.run(
                    prompt,
                    model=build_model(llm_str, ctx.deps.settings),
                    model_settings=make_run_settings(llm_str),
                    deps=ctx.deps,
                )
                decision = cast(GateDecision, result.output)
            else:
                raise
        ctx.state.gate_decision = decision
        ctx.state.history.extend(result.new_messages())
        _debit_budget(ctx.state, result.usage, model_str)

        out_of_budget = ctx.state.budget_remaining_usd <= 0
        at_max_iter = ctx.state.iteration >= MAX_ITERATIONS - 1

        await append(
            AuditRecord(
                run_id=ctx.state.run_id,
                actor="review_agent",
                event_type="tool_call",
                graph_node="gate",
                tool_name="gate_decision",
                tool_args={"iteration": ctx.state.iteration},
                outcome=f"{decision.action} (confidence={decision.confidence:.2f})",
                decision_confidence=decision.confidence,
                policy_decision=(
                    PolicyDecision.require_approval if decision.action == "escalate" else None
                ),
            )
        )

        if decision.action == "pass":
            return End(
                CodingResult(
                    task=ctx.state.task,
                    iterations=ctx.state.iteration + 1,
                    edits=ctx.state.edits,
                    test_result=ctx.state.test_result,
                    gate_decision=decision,
                )
            )

        if decision.action == "escalate" or out_of_budget or at_max_iter:
            reason = (
                "Budget exhausted"
                if out_of_budget
                else f"Max iterations ({MAX_ITERATIONS}) reached"
                if at_max_iter
                else decision.reason
            )
            raise CodingApprovalRequired(reason, ctx.state.run_id, ctx.state.test_result)

        # "fix" — reset edits and retry
        ctx.state.iteration += 1
        ctx.state.edits = []
        ctx.state.test_result = None
        return PlanNode()
