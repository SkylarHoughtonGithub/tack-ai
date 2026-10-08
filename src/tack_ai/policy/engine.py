import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

import httpx

from tack_ai.core.config import Settings
from tack_ai.core.models import AuditRecord, PolicyDecision

settings = Settings()

# Set to True inside a DBOS workflow step so approve/deny uses DB polling
# instead of a terminal prompt.
durable_mode: ContextVar[bool] = ContextVar("durable_mode", default=False)

# Web-mode override: when set, request_approval calls this instead of the
# terminal prompt or DB polling.  Set per-task in web.py's background runner.
_approval_override: ContextVar[
    Callable[[str, dict[str, Any]], Awaitable[bool]] | None
] = ContextVar("_approval_override", default=None)

OPA_URL = "http://localhost:8181"
_DECISION_PATH = "/v1/data/tack/policy/decision"
_VERSION_PATH = "/v1/data/tack/policy/policy_version"

_cached_version: str | None = None

# Tracks the most recent tool name per run_id for multi-step (confused-deputy) detection.
_prior_tool: dict[str, str] = {}


async def get_policy_version() -> str:
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{OPA_URL}{_VERSION_PATH}")
            resp.raise_for_status()
            _cached_version = str(resp.json().get("result", "unknown"))
            return _cached_version
    except Exception:
        return "unknown"


async def policy_check(
    tool_name: str,
    args: dict,
    user: str = "user",
    run_id: str | None = None,
) -> PolicyDecision:
    """Evaluate policy for a tool call — routes to Cedar or OPA based on config.

    Cedar handles tool-authorization only; routing/budget calls (__route__)
    always go to OPA regardless of the policy_engine setting.
    """
    prior = _prior_tool.get(run_id) if run_id else None

    if settings.policy_engine == "cedar" and tool_name != "__route__":
        from cedar.cedar_policy import cedar_decide
        decision = cedar_decide(tool_name, args, {"prior_tool": prior or ""})
        if run_id:
            _prior_tool[run_id] = tool_name
        return decision

    payload = {
        "input": {
            "tool_name": tool_name,
            "args": args,
            "user": user,
            "context": {"prior_tool": prior or ""},
        }
    }
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(f"{OPA_URL}{_DECISION_PATH}", json=payload)
            resp.raise_for_status()
            result = PolicyDecision(resp.json().get("result", "deny"))
            # Record this tool as the prior for the next call in the same run.
            if run_id:
                _prior_tool[run_id] = tool_name
            return result
    except httpx.ConnectError:
        print("\n[POLICY] OPA is not reachable — failing closed (deny).")
        print("  Start OPA with: opa run --server --addr :8181 policies/")
        return PolicyDecision.deny
    except Exception as e:
        print(f"\n[POLICY] Unexpected error contacting OPA ({e}) — failing closed.")
        return PolicyDecision.deny


async def request_approval(tool_name: str, args: dict) -> bool:
    """Approve or deny a tool call.

    Priority:
    1. _approval_override (web mode — asyncio.Event-based, no terminal needed)
    2. durable_mode (DBOS — DB-polling, survives restarts)
    3. terminal prompt (CLI default)
    """
    override = _approval_override.get()
    if override is not None:
        return await override(tool_name, args)

    if durable_mode.get():
        # Lazy import to avoid circular dependency at module load time.
        from tack_ai.audit import current_run_id
        from tack_ai.durable import wait_for_db_approval
        run_id = current_run_id.get()
        return await wait_for_db_approval(run_id, tool_name, args)

    print(f"\n{'─'*50}")
    print("  APPROVAL REQUIRED")
    print(f"  Tool: {tool_name}")
    for k, v in args.items():
        print(f"  {k}: {v}")
    print(f"{'─'*50}")
    loop = asyncio.get_event_loop()
    answer = await loop.run_in_executor(None, lambda: input("  Approve? [y/N]: "))
    approved = answer.strip().lower() == "y"
    print(f"  → {'Approved' if approved else 'Denied'}\n")
    return approved


async def enforce(tool_name: str, args: dict, user: str = "user") -> tuple[bool, str]:
    """Run policy check and approval flow. Returns (should_execute, reason)."""
    from tack_ai.audit import append, current_run_id, redact_args
    from tack_ai.observability import policy_decisions_total, tool_calls_total  # noqa: PLC0415

    run_id = current_run_id.get()

    # If a prior approval grant exists in FGA for this run/tool, allow without
    # re-running the approval flow. This enables mid-run revocation: deleting
    # the FGA tuple from the approvals page will force re-approval on the next call.
    if run_id and _approval_override.get() is not None:
        from tack_ai.web.auth import get_fga_client  # noqa: PLC0415
        fga = get_fga_client()
        if fga is not None:
            try:
                if await fga.can_execute(run_id, tool_name):
                    tool_calls_total.labels(tool_name=tool_name, outcome="fga_grant").inc()
                    return True, "approved (FGA grant)"
            except Exception as e:
                print(f"\n[POLICY] FGA check failed ({e}) — falling through to OPA.")

    decision = await policy_check(tool_name, args, user, run_id=run_id)
    policy_decisions_total.labels(decision=decision.value).inc()
    version = await get_policy_version()
    approver: str | None = None
    approved_at: datetime | None = None

    if decision == PolicyDecision.allow:
        ok, reason = True, "allowed"
        tool_calls_total.labels(tool_name=tool_name, outcome="allowed").inc()
    elif decision == PolicyDecision.deny:
        ok, reason = False, f"policy denied '{tool_name}'"
        tool_calls_total.labels(tool_name=tool_name, outcome="denied").inc()
    elif decision == PolicyDecision.require_approval:
        approved = await request_approval(tool_name, args)
        approver = user
        approved_at = datetime.now(timezone.utc)
        if approved:
            ok, reason = True, "approved by user"
            tool_calls_total.labels(tool_name=tool_name, outcome="approved").inc()
        else:
            ok, reason = False, f"user denied '{tool_name}'"
            tool_calls_total.labels(tool_name=tool_name, outcome="user_denied").inc()
    else:
        ok, reason = False, "unknown policy decision"
        tool_calls_total.labels(tool_name=tool_name, outcome="unknown").inc()

    if run_id:
        await append(AuditRecord(
            run_id=run_id,
            actor=user,
            event_type="policy_decision",
            tool_name=tool_name,
            tool_args=redact_args(args),
            policy_decision=decision,
            policy_version=version,
            approver=approver,
            approved_at=approved_at,
            outcome="executed" if ok else "blocked",
        ))

    return ok, reason
