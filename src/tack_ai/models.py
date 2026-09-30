from datetime import datetime
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
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    run_id: str
    called_at: datetime = Field(default_factory=datetime.utcnow)


class PolicyDecision(str, Enum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class AuditRecord(BaseModel):
    run_id: str
    actor: str
    event_type: str
    tool_name: str | None = None
    policy_decision: PolicyDecision | None = None
    outcome: str | None = None
    cost_usd: float | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
