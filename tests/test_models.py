"""Unit tests for core data models."""

from datetime import datetime, timezone

import pytest

from tack_ai.core.models import AuditRecord, PolicyDecision, Task, TaskPriority, ToolCall


def test_task_defaults():
    t = Task(id="t1", description="do something")
    assert t.priority == TaskPriority.medium
    assert isinstance(t.created_at, datetime)


def test_tool_call_fields():
    tc = ToolCall(tool_name="web_search", arguments={"query": "foo"}, run_id="r1")
    assert tc.tool_name == "web_search"
    assert tc.run_id == "r1"


def test_policy_decision_values():
    assert PolicyDecision.allow == "allow"
    assert PolicyDecision.deny == "deny"
    assert PolicyDecision.require_approval == "require_approval"


def test_audit_record_hash_defaults():
    rec = AuditRecord(run_id="r1", actor="user", event_type="routing")
    assert rec.prev_hash == ""
    assert rec.hash == ""
    assert rec.timestamp is not None


def test_audit_record_optional_fields():
    rec = AuditRecord(
        run_id="r1",
        actor="alice",
        event_type="approval",
        tool_name="run_code",
        tool_args={"language": "python"},
        policy_decision=PolicyDecision.require_approval,
        approver="bob",
        approved_at=datetime.now(timezone.utc),
        outcome="approved",
    )
    assert rec.approver == "bob"
    assert rec.outcome == "approved"
    assert rec.policy_decision == PolicyDecision.require_approval
