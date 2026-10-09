from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from tack_ai.core.config import Settings


# ── Agent output types (Pydantic models — used as output_type on sub-agents) ──


class EditPlanStep(BaseModel):
    file_path: str
    instruction: str


class EditPlan(BaseModel):
    steps: list[EditPlanStep]
    reasoning: str = ""


class EditResult(BaseModel):
    file_path: str
    diff: str  # summary of changes made, not necessarily unified-diff format
    review_passed: bool = True


class RunTestResult(BaseModel):
    passed: bool
    output: str
    failed_tests: list[str] = []


class GateDecision(BaseModel):
    action: Literal["fix", "escalate", "pass"]
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = ""


class _GateDecisionJev(BaseModel):
    """Jev-compatible subset — no str fields (Jev cannot generate free text)."""

    action: Literal["fix", "escalate", "pass"]
    confidence: float = Field(default=0.0, ge=0, le=1)


class CodingResult(BaseModel):
    task: str
    iterations: int
    edits: list[EditResult]
    test_result: RunTestResult | None = None
    gate_decision: GateDecision | None = None


# ── Graph internals (dataclasses — mutable state and deps) ────────────────────


@dataclass
class CodingDeps:
    settings: Settings


@dataclass
class CodingState:
    task: str
    run_id: str
    plan: list[EditPlanStep] = field(default_factory=list)
    edits: list[EditResult] = field(default_factory=list)
    test_result: RunTestResult | None = None
    gate_decision: GateDecision | None = None
    iteration: int = 0
    budget_remaining_usd: float = 0.0


class CodingApprovalRequired(Exception):
    """Raised by GateNode when the reviewer escalates to a human."""

    def __init__(self, reason: str, run_id: str, test_result: RunTestResult | None = None) -> None:
        self.reason = reason
        self.run_id = run_id
        self.test_result = test_result
        super().__init__(reason)
