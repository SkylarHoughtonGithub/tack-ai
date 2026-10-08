from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskPriority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Task(BaseModel):
    id: str
    description: str
    priority: TaskPriority = TaskPriority.medium
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    run_id: str
    called_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PolicyDecision(str, Enum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class AuditRecord(BaseModel):
    run_id: str
    actor: str
    # "routing" | "tool_call" | "policy_decision" | "approval" | "outcome"
    event_type: str
    # Routing
    routing_tier: str | None = None
    routing_reason: str | None = None
    # Model
    model: str | None = None
    provider: str | None = None
    # Tool
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None  # secrets already redacted
    # Policy
    policy_decision: PolicyDecision | None = None
    policy_version: str | None = None
    policy_rule: str | None = None
    # Approval
    approver: str | None = None
    approved_at: datetime | None = None
    # Outcome
    outcome: str | None = None
    cost_usd: float | None = None
    # Hash chain — both set by audit.append(), not by callers
    prev_hash: str = ""
    hash: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
