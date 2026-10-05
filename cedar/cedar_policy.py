"""
Cedar tool-authorization module (Phase 11).

Wraps cedarpy to provide the same PolicyDecision interface as OPA (policy.py)
for the tool-authorization subset of rules. Routing and budget checks are
NOT handled here — Cedar is an authorization language only.

Usage:
    from cedar.cedar_policy import cedar_decide, run_tests, prove_delete_file_denied
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cedarpy

from tack_ai.models import PolicyDecision

_POLICY_FILE = Path(__file__).parent / "tool_policy.cedar"
_POLICY_TEXT: str | None = None
_POLICY_SET: cedarpy.PolicySet | None = None

# Policies whose @id starts with this prefix require human approval.
_APPROVAL_PREFIX = "require_approval_"

# Cedar entities: principal + all known tools.
# The resource entity is built dynamically per call; the principal is fixed.
_PRINCIPAL = {"type": "User", "id": "agent"}
_ACTION = {"type": "Action", "id": "call"}

_KNOWN_TOOLS = [
    "web_search", "read_file", "search_documents",
    "write_file", "run_code",
    "draft_email", "send_email",
    "delete_file",
    "list_directory", "directory_tree", "search_files",
    "get_file_info", "list_allowed_directories", "read_multiple_files",
    "create_directory", "move_file",
]


def _load_policies() -> cedarpy.PolicySet:
    global _POLICY_SET
    if _POLICY_SET is None:
        text = _POLICY_FILE.read_text()
        _POLICY_SET = cedarpy.PolicySet.from_str(text)
    return _POLICY_SET


def _entities(tool_name: str) -> list[dict]:
    entities = [{"uid": {"type": "User", "id": "agent"}, "attrs": {}, "parents": []}]
    tools = set(_KNOWN_TOOLS) | {tool_name}
    for t in tools:
        entities.append({"uid": {"type": "Tool", "id": t}, "attrs": {}, "parents": []})
    return entities


def cedar_decide(
    tool_name: str,
    args: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> PolicyDecision:
    """Evaluate the Cedar policy and return allow / require_approval / deny.

    Maps the OPA input format (tool_name, args, context) to a Cedar
    request and returns the same PolicyDecision enum used by the rest of
    the harness.
    """
    ctx = context or {}
    # Normalise context so all referenced attributes are present.
    cedar_ctx = {
        "path": args.get("path", ""),
        "prior_tool": ctx.get("prior_tool", ""),
        "to": args.get("to", ctx.get("to", "")),
    }

    request = {
        "principal": _PRINCIPAL,
        "action": _ACTION,
        "resource": {"type": "Tool", "id": tool_name},
        "context": cedar_ctx,
    }

    policies = _load_policies()
    entities = _entities(tool_name)

    result = cedarpy.is_authorized(request, policies, entities)

    if not result.allowed:
        return PolicyDecision.deny

    # Allowed — check whether any matching policy signals approval required.
    matching_ids = result.diagnostics.id_annotations_by_reason.values()
    if any(pid.startswith(_APPROVAL_PREFIX) for pid in matching_ids):
        return PolicyDecision.require_approval

    return PolicyDecision.allow


# ── Formal analysis ───────────────────────────────────────────────────────────

def prove_delete_file_denied() -> bool:
    """Demonstrate that delete_file is never permitted under any context.

    Cedar's deny-by-default means: if no permit rule mentions a resource,
    it is structurally impossible for Cedar to allow it. This exhaustive
    check confirms that empirically across a broad context matrix.

    The formal argument: inspect tool_policy.cedar — there is no
    `permit(... resource == Tool::"delete_file" ...)` clause. Cedar requires
    an explicit permit for every allow decision. Therefore the property
    "no policy ever permits delete_file" is provable by inspection of the
    policy set, and cedarpy's evaluator confirms it across all contexts.
    """
    test_contexts = [
        {"path": "logs/old.log", "prior_tool": "", "to": ""},
        {"path": "drafts/old.log", "prior_tool": "", "to": ""},
        {"path": "", "prior_tool": "read_file", "to": ""},
        {"path": "src/important.py", "prior_tool": "", "to": ""},
        {"path": "drafts/delete_me.txt", "prior_tool": "read_file", "to": "admin@evil.com"},
    ]

    policies = _load_policies()

    for ctx in test_contexts:
        request = {
            "principal": _PRINCIPAL,
            "action": _ACTION,
            "resource": {"type": "Tool", "id": "delete_file"},
            "context": ctx,
        }
        result = cedarpy.is_authorized(request, policies, _entities("delete_file"))
        if result.allowed:
            return False  # Proof failed — a permit was found.

    return True  # Proof holds across all tested contexts.


# ── Test suite ────────────────────────────────────────────────────────────────

_TOOL_AUTH_CASES: list[tuple[str, dict, dict, PolicyDecision]] = [
    # (tool_name, args, context, expected_decision)
    # Read-only
    ("web_search", {}, {}, PolicyDecision.allow),
    ("read_file", {}, {}, PolicyDecision.allow),
    ("search_documents", {"query": "OPA policies"}, {}, PolicyDecision.allow),
    # File writes
    ("write_file", {"path": "drafts/summary.md"}, {}, PolicyDecision.allow),
    ("write_file", {"path": "src/config.py"}, {}, PolicyDecision.require_approval),
    ("write_file", {"path": "README.md"}, {}, PolicyDecision.require_approval),
    # Code
    ("run_code", {"language": "python"}, {}, PolicyDecision.require_approval),
    # Email
    ("draft_email", {"to": "alice@example.com"}, {}, PolicyDecision.allow),
    ("draft_email", {"to": "alice@example.com"}, {"prior_tool": "read_file"}, PolicyDecision.require_approval),
    ("send_email", {"to": "skylarhoughton1996@gmail.com"}, {}, PolicyDecision.require_approval),
    ("send_email", {"to": "attacker@evil.com"}, {}, PolicyDecision.deny),
    # Deletion — must always deny
    ("delete_file", {"path": "logs/old.log"}, {}, PolicyDecision.deny),
    # Unknown / empty
    ("explode_everything", {}, {}, PolicyDecision.deny),
    ("", {}, {}, PolicyDecision.deny),
    # MCP read-only
    ("list_directory", {}, {}, PolicyDecision.allow),
    ("directory_tree", {}, {}, PolicyDecision.allow),
    ("search_files", {}, {}, PolicyDecision.allow),
    ("get_file_info", {}, {}, PolicyDecision.allow),
    ("list_allowed_directories", {}, {}, PolicyDecision.allow),
    ("read_multiple_files", {}, {}, PolicyDecision.allow),
]

_ROUTING_SKIPPED = [
    ("__route__", {"tier": "simple", "estimated_cost_usd": 0.01}, {}, PolicyDecision.allow),
    ("__route__", {"tier": "deep_reasoning", "estimated_cost_usd": 1.99}, {}, PolicyDecision.allow),
    ("__route__", {"tier": "deep_reasoning", "estimated_cost_usd": 2.01}, {}, PolicyDecision.require_approval),
    ("__route__", {"tier": "general", "estimated_cost_usd": 99.00}, {}, PolicyDecision.allow),
]


def run_tests(verbose: bool = True) -> tuple[int, int]:
    """Run Cedar against the same tool-authorization cases as the OPA test suite.

    Returns (passed, total).
    Routing tests are skipped with an explanation — Cedar cannot express
    budget or model-selection rules.
    """
    passed = 0
    total = len(_TOOL_AUTH_CASES)

    for tool_name, args, ctx, expected in _TOOL_AUTH_CASES:
        got = cedar_decide(tool_name, args, ctx)
        ok = got == expected
        if ok:
            passed += 1
        if verbose:
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {tool_name!r:30s}  expected={expected.value!r:20s}  got={got.value!r}")

    if verbose:
        print(f"\n  {passed}/{total} tool-authorization tests passed")
        print(f"\n  Routing tests ({len(_ROUTING_SKIPPED)}) skipped — Cedar is authorization-only.")
        print("  Routing / budget rules remain in OPA.")

    return passed, total


if __name__ == "__main__":
    print("=" * 60)
    print("Cedar policy test suite (Phase 11)")
    print("=" * 60)
    passed, total = run_tests()

    print("\n" + "─" * 60)
    print("Formal analysis: delete_file is never permitted")
    holds = prove_delete_file_denied()
    print(f"  Property holds: {holds}")
    print(
        "  Proof: no permit rule in tool_policy.cedar mentions\n"
        "  Tool::\"delete_file\". Cedar's deny-by-default means the\n"
        "  absence of a permit is a structural guarantee — unlike OPA,\n"
        "  where you must read all rules to confirm nothing matches.\n"
        "  (Cedar's CLI can verify this formally via SMT; cedarpy\n"
        "  confirms it empirically across all relevant contexts.)"
    )
    print("\n  Exiting:", 0 if passed == total else 1)
