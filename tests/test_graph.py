"""Unit tests for the pydantic_graph coding workflow.

All tests use mocked agents — no API keys or network required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tack_ai.graph.state import (
    CodingApprovalRequired,
    CodingDeps,
    CodingResult,
    CodingState,
    EditPlan,
    EditPlanStep,
    EditResult,
    GateDecision,
    RunTestResult,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    s = MagicMock()
    s.typesafe_api_key = None
    s.database_url = None
    s.openai_api_key = None
    return s


@pytest.fixture
def deps(settings):
    return CodingDeps(settings=settings)


@pytest.fixture
def state():
    return CodingState(task="Add a docstring to foo()", run_id="test-run", budget_remaining_usd=1.0)


def _mock_ctx(state, deps):
    ctx = MagicMock()
    ctx.state = state
    ctx.deps = deps
    return ctx


def _agent_result(output):
    """Return an AsyncMock that resolves to a fake AgentRunResult with the given output."""
    result = MagicMock()
    result.output = output
    result.usage = MagicMock(input_tokens=10, output_tokens=5)
    mock = AsyncMock(return_value=result)
    return mock


# ── State model tests ─────────────────────────────────────────────────────────


def test_gate_decision_defaults():
    d = GateDecision(action="pass")
    assert d.confidence == 0.0
    assert d.reason == ""


def test_coding_state_defaults():
    s = CodingState(task="t", run_id="r")
    assert s.iteration == 0
    assert s.plan == []
    assert s.edits == []
    assert s.budget_remaining_usd == 0.0


def test_coding_result_fields():
    r = CodingResult(task="t", iterations=2, edits=[], gate_decision=GateDecision(action="pass"))
    assert r.iterations == 2
    assert r.test_result is None


def test_approval_required_exception():
    exc = CodingApprovalRequired("needs human", "run-1")
    assert exc.reason == "needs human"
    assert exc.run_id == "run-1"
    assert str(exc) == "needs human"


# ── PlanNode ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_node_populates_state(state, deps):
    from tack_ai.graph.nodes import PlanNode

    plan = EditPlan(steps=[EditPlanStep(file_path="foo.py", instruction="Add docstring")])

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"simple": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.planner_agent.run", _agent_result(plan)),
    ):
        ctx = _mock_ctx(state, deps)
        node = PlanNode()
        next_node = await node.run(ctx)

    assert len(state.plan) == 1
    assert state.plan[0].file_path == "foo.py"
    assert type(next_node).__name__ == "EditNode"


# ── EditNode ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_node_runs_all_steps_in_parallel(state, deps):
    from tack_ai.graph.nodes import EditNode

    state.plan = [
        EditPlanStep(file_path="a.py", instruction="add docstring"),
        EditPlanStep(file_path="b.py", instruction="fix typo"),
    ]

    edit_result = EditResult(file_path="a.py", diff="added docstring")

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"general": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.edit_agent.run", _agent_result(edit_result)),
    ):
        ctx = _mock_ctx(state, deps)
        next_node = await EditNode().run(ctx)

    assert len(state.edits) == 2
    assert type(next_node).__name__ == "TestNode"


# ── TestNode ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_node_populates_result(state, deps):
    from tack_ai.graph.nodes import TestNode

    state.edits = [EditResult(file_path="foo.py", diff="added docstring")]
    test_result = RunTestResult(passed=True, output="5 passed")

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"general": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.test_agent.run", _agent_result(test_result)),
    ):
        ctx = _mock_ctx(state, deps)
        next_node = await TestNode().run(ctx)

    assert state.test_result is not None
    assert state.test_result.passed is True
    assert type(next_node).__name__ == "GateNode"


# ── GateNode ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_node_pass_returns_end(state, deps):
    from pydantic_graph import End

    from tack_ai.graph.nodes import GateNode

    state.edits = [EditResult(file_path="foo.py", diff="done")]
    state.test_result = RunTestResult(passed=True, output="5 passed")
    decision = GateDecision(action="pass", confidence=0.95, reason="all good")

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"simple": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.review_agent.run", _agent_result(decision)),
    ):
        ctx = _mock_ctx(state, deps)
        result = await GateNode().run(ctx)

    assert isinstance(result, End)
    assert result.data.gate_decision.action == "pass"
    assert result.data.iterations == 1


@pytest.mark.asyncio
async def test_gate_node_fix_returns_plan_node(state, deps):
    from tack_ai.graph.nodes import GateNode

    state.edits = [EditResult(file_path="foo.py", diff="partial")]
    state.test_result = RunTestResult(passed=False, output="1 failed", failed_tests=["test_foo"])
    decision = GateDecision(action="fix", confidence=0.8, reason="tests still failing")

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"simple": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.review_agent.run", _agent_result(decision)),
    ):
        ctx = _mock_ctx(state, deps)
        result = await GateNode().run(ctx)

    assert type(result).__name__ == "PlanNode"
    assert state.iteration == 1
    assert state.edits == []
    assert state.test_result is None


@pytest.mark.asyncio
async def test_gate_node_escalate_raises(state, deps):
    from tack_ai.graph.nodes import GateNode

    state.edits = [EditResult(file_path="foo.py", diff="unclear")]
    state.test_result = RunTestResult(passed=False, output="error")
    decision = GateDecision(action="escalate", confidence=0.5, reason="ambiguous requirements")

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"simple": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.review_agent.run", _agent_result(decision)),
    ):
        ctx = _mock_ctx(state, deps)
        with pytest.raises(CodingApprovalRequired) as exc_info:
            await GateNode().run(ctx)

    assert exc_info.value.run_id == "test-run"


@pytest.mark.asyncio
async def test_gate_node_max_iterations_raises(state, deps):
    from tack_ai.graph.nodes import MAX_ITERATIONS, GateNode

    state.iteration = MAX_ITERATIONS - 1
    state.edits = [EditResult(file_path="foo.py", diff="still broken")]
    state.test_result = RunTestResult(passed=False, output="2 failed")
    decision = GateDecision(action="fix", confidence=0.6, reason="needs another try")

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"simple": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", return_value=MagicMock()),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.review_agent.run", _agent_result(decision)),
    ):
        ctx = _mock_ctx(state, deps)
        with pytest.raises(CodingApprovalRequired):
            await GateNode().run(ctx)


@pytest.mark.asyncio
async def test_gate_node_uses_jev_when_key_set(state, deps):
    """GateNode selects typesafe:jev-latest when TYPESAFE_API_KEY is present."""
    from tack_ai.graph.nodes import GateNode

    deps.settings.typesafe_api_key = "sk-fake-typesafe-key"
    state.edits = [EditResult(file_path="foo.py", diff="done")]
    state.test_result = RunTestResult(passed=True, output="all pass")
    decision = GateDecision(action="pass")

    captured_model_str: list[str] = []

    def capturing_build_model(model_str, settings):
        captured_model_str.append(model_str)
        return MagicMock()

    with (
        patch("tack_ai.graph.nodes.load_model_config", return_value={"tiers": {"simple": "anthropic:claude-haiku-4-5-20251001"}}),
        patch("tack_ai.graph.nodes.build_model", side_effect=capturing_build_model),
        patch("tack_ai.graph.nodes.FallbackModel", side_effect=lambda *models: models[0]),
        patch("tack_ai.graph.nodes.make_run_settings", return_value=None),
        patch("tack_ai.graph.nodes.append", new_callable=AsyncMock),
        patch("tack_ai.graph.agents.review_agent.run", _agent_result(decision)),
    ):
        ctx = _mock_ctx(state, deps)
        await GateNode().run(ctx)

    assert captured_model_str[0] == "typesafe:jev-latest"
