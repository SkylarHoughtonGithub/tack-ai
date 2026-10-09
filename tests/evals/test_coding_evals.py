"""LLM-as-judge regression evals for the tack-ai coding workflow.

Run:   uv run pytest tests/evals/ -m eval -v
Skip:  included in --ignore=tests/evals or -m 'not eval' (default CI run)

Requires at least one of: ANTHROPIC_API_KEY, OPENAI_API_KEY.
Evals cost money — they are a separate CI job, not part of the unit test suite.
"""
from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import LLMJudge
from pydantic_evals.evaluators.llm_as_a_judge import set_default_judge_model

from tack_ai.graph.state import CodingDeps, EditPlan

pytestmark = pytest.mark.eval

# Eval-specific planner: same output type and system prompt as the production
# planner_agent, but no PolicyEnforcementCapability and no tools. Evals measure
# LLM planning quality, not policy compliance — keeping them infrastructure-free
# makes them fast, deterministic, and runnable without OPA/Cedar.
_eval_planner: Agent[CodingDeps, EditPlan] = Agent(
    None,
    output_type=EditPlan,
    system_prompt=(
        "You are a coding planner. Given a task, decompose it into a list of concrete "
        "file edits. For each file, provide the exact relative file path and a clear, "
        "self-contained instruction for what to change. "
        "Be precise: the edit agent will apply your instructions without additional context."
    ),
)


def _settings():
    from tack_ai.core.config import Settings

    return Settings()


def _require_key() -> None:
    s = _settings()
    if not (s.anthropic_api_key or s.openai_api_key):
        pytest.skip("No LLM API key — set ANTHROPIC_API_KEY or OPENAI_API_KEY to run evals")


def _task_model_str() -> str:
    s = _settings()
    return (
        "anthropic:claude-haiku-4-5-20251001"
        if s.anthropic_api_key
        else "openai:gpt-4o-mini"
    )


def _configure_judge() -> None:
    from tack_ai.core.router import build_model

    s = _settings()
    set_default_judge_model(build_model(_task_model_str(), s))


async def _run_planner(task: str) -> str:
    """Run the eval planner on a task and return the plan as a readable string."""
    from tack_ai.core.router import build_model, make_run_settings

    s = _settings()
    model_str = _task_model_str()
    result = await _eval_planner.run(
        f"Task: {task}",
        model=build_model(model_str, s),
        model_settings=make_run_settings(model_str),
        deps=CodingDeps(settings=s),
    )
    steps = result.output.steps
    if not steps:
        raise ValueError("Planner returned an empty plan")
    return "\n".join(f"- {s.file_path}: {s.instruction}" for s in steps)


_planner_dataset: Dataset[str, str] = Dataset(
    name="planner",
    cases=[
        Case(
            name="add_function",
            inputs=(
                "Add a function `compute_hash(data: bytes) -> str` that returns the SHA-256 "
                "hex digest to src/tack_ai/audit/hash_chain.py"
            ),
            evaluators=[
                LLMJudge(
                    rubric=(
                        "The plan must identify hash_chain.py as the target file and describe "
                        "adding a compute_hash function that accepts bytes and returns a str."
                    ),
                    include_input=True,
                )
            ],
        ),
        Case(
            name="fix_bug",
            inputs=(
                "Fix the KeyError raised in src/tack_ai/core/router.py when build_model "
                "receives a provider name not present in the model registry"
            ),
            evaluators=[
                LLMJudge(
                    rubric=(
                        "The plan must target router.py and describe adding error handling "
                        "or a safe lookup for unknown provider names."
                    ),
                    include_input=True,
                )
            ],
        ),
        Case(
            name="refactor",
            inputs=(
                "Refactor src/tack_ai/audit/__init__.py: extract the `append` function body "
                "into a new module src/tack_ai/audit/writer.py and re-export it from __init__.py"
            ),
            evaluators=[
                LLMJudge(
                    rubric=(
                        "The plan must mention both audit/__init__.py and audit/writer.py and "
                        "describe moving the append function while keeping the public import."
                    ),
                    include_input=True,
                )
            ],
        ),
        Case(
            name="explain_code",
            inputs=(
                "Add a module-level docstring to src/tack_ai/graph/nodes.py explaining the "
                "Plan→Edit→Test→Gate flow and the role of each node"
            ),
            evaluators=[
                LLMJudge(
                    rubric=(
                        "The plan must target nodes.py and describe adding a docstring that "
                        "explains the four-node coding workflow (plan, edit, test, gate)."
                    ),
                    include_input=True,
                )
            ],
        ),
    ],
)


@pytest.mark.asyncio
async def test_planner_quality() -> None:
    """Planner decomposes all four task types into correct file-level edits."""
    _require_key()
    _configure_judge()

    report = await _planner_dataset.evaluate(_run_planner, progress=False)
    report.print()

    if report.failures:
        msgs = "; ".join(f"{f.name}: {f.error_message}" for f in report.failures)
        pytest.fail(f"{len(report.failures)} case(s) errored — {msgs}")

    assert report.cases, "No eval cases ran — check _run_planner for errors"

    # Per-case assertion breakdown — always printed so results are visible
    print("\nPer-case LLMJudge results:")
    per_case: dict[str, float | None] = {}
    for c in report.cases:
        # assertions is dict[str, EvaluationResult[bool]]; True=1.0, False=0.0
        results = [v.value for v in c.assertions.values()] if c.assertions else []
        score: float | None = (sum(results) / len(results)) if results else None
        per_case[c.name] = score
        status = "PASS" if score is not None and score >= 1.0 else "FAIL" if score is not None else "NO_RESULT"
        print(f"  {status:10s} {c.name}  ({f'{score:.0%}' if score is not None else '—'})")

    no_result = [n for n, s in per_case.items() if s is None]
    if no_result:
        pytest.fail(
            f"LLMJudge produced no assertion for: {', '.join(no_result)} — check judge model config"
        )

    failed = [n for n, s in per_case.items() if s is not None and s < 1.0]
    passing = len(per_case) - len(failed)
    assert passing / len(per_case) >= 0.75, (
        f"Planner quality below threshold: {passing}/{len(per_case)} cases passed "
        f"(need ≥75%). Failed: {', '.join(failed)}"
    )
