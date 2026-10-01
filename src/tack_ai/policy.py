import asyncio
import httpx
from tack_ai.models import PolicyDecision

OPA_URL = "http://localhost:8181"
_POLICY_PATH = "/v1/data/tack/policy/decision"


async def policy_check(tool_name: str, args: dict, user: str = "user") -> PolicyDecision:
    """Ask OPA whether this tool call is allowed, denied, or needs approval."""
    payload = {"input": {"tool_name": tool_name, "args": args, "user": user}}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(f"{OPA_URL}{_POLICY_PATH}", json=payload)
            resp.raise_for_status()
            result = resp.json().get("result", "deny")
            return PolicyDecision(result)
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
    decision = await policy_check(tool_name, args, user)

    if decision == PolicyDecision.allow:
        return True, "allowed"

    if decision == PolicyDecision.deny:
        return False, f"policy denied '{tool_name}'"

    if decision == PolicyDecision.require_approval:
        approved = await request_approval(tool_name, args)
        if approved:
            return True, "approved by user"
        return False, f"user denied '{tool_name}'"

    return False, "unknown policy decision"
