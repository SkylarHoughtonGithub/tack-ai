import asyncio
from datetime import datetime, timezone

import httpx

from tack_ai.models import AuditRecord, PolicyDecision

OPA_URL = "http://localhost:8181"
_DECISION_PATH = "/v1/data/tack/policy/decision"
_VERSION_PATH = "/v1/data/tack/policy/policy_version"

_cached_version: str | None = None


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


async def policy_check(tool_name: str, args: dict, user: str = "user") -> PolicyDecision:
    """Ask OPA whether this tool call is allowed, denied, or needs approval."""
    payload = {"input": {"tool_name": tool_name, "args": args, "user": user}}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(f"{OPA_URL}{_DECISION_PATH}", json=payload)
            resp.raise_for_status()
            return PolicyDecision(resp.json().get("result", "deny"))
    except httpx.ConnectError:
        print("\n[POLICY] OPA is not reachable — failing closed (deny).")
        print("  Start OPA with: opa run --server --addr :8181 policies/")
        return PolicyDecision.deny
    except Exception as e:
        print(f"\n[POLICY] Unexpected error contacting OPA ({e}) — failing closed.")
        return PolicyDecision.deny


async def request_approval(tool_name: str, args: dict) -> bool:
    """Prompt the user to approve or deny a tool call from the terminal."""
    print(f"\n{'─'*50}")
    print(f"  APPROVAL REQUIRED")
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

    decision = await policy_check(tool_name, args, user)
    version = await get_policy_version()
    approver: str | None = None
    approved_at: datetime | None = None

    if decision == PolicyDecision.allow:
        ok, reason = True, "allowed"
    elif decision == PolicyDecision.deny:
        ok, reason = False, f"policy denied '{tool_name}'"
    elif decision == PolicyDecision.require_approval:
        approved = await request_approval(tool_name, args)
        if approved:
            approver = user
            approved_at = datetime.now(timezone.utc)
            ok, reason = True, "approved by user"
        else:
            ok, reason = False, f"user denied '{tool_name}'"
    else:
        ok, reason = False, "unknown policy decision"

    run_id = current_run_id.get()
    if run_id:
        append(AuditRecord(
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
