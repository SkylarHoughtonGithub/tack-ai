package tack.policy

import rego.v1

# Fail-closed: anything without an explicit allow/require_approval rule is denied.
default decision := "deny"

# ── Read-only tools ──────────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name in {"web_search", "read_file"}
}

# ── File writes ──────────────────────────────────────────────────────────────

# Writes inside drafts/ are allowed without approval.
decision := "allow" if {
    input.tool_name == "write_file"
    startswith(input.args.path, "drafts/")
}

# Writes anywhere else require human approval.
decision := "require_approval" if {
    input.tool_name == "write_file"
    not startswith(input.args.path, "drafts/")
}

# ── Code execution ───────────────────────────────────────────────────────────

decision := "require_approval" if {
    input.tool_name == "run_code"
}

# ── Email ────────────────────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name == "draft_email"
}

decision := "require_approval" if {
    input.tool_name == "send_email"
}

# ── File deletion ────────────────────────────────────────────────────────────
# delete_file has no allow rule — stays at default "deny" regardless of args.

# ── Routing approval ─────────────────────────────────────────────────────────

# Deep-reasoning runs estimated over $2 require approval.
decision := "require_approval" if {
    input.tool_name == "__route__"
    input.args.tier == "deep_reasoning"
    input.args.estimated_cost_usd > 2.0
}

decision := "allow" if {
    input.tool_name == "__route__"
    not _expensive_deep_reasoning
}

_expensive_deep_reasoning if {
    input.args.tier == "deep_reasoning"
    input.args.estimated_cost_usd > 2.0
}
