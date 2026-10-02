package tack.policy

import rego.v1

policy_version := "1.0.0"

# Fail-closed: anything without an explicit allow/require_approval rule is denied.
default decision := "deny"

# ── Read-only tools ──────────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name in {"web_search", "read_file", "search_documents"}
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

# Allowlist of known-safe recipients. send_email to any other address is denied.
email_allowlist := {"skylarhoughton1996@gmail.com"}

decision := "allow" if {
    input.tool_name == "draft_email"
    not _prior_read_file
}

# Stretch: confused-deputy guard — drafting email immediately after reading a file
# requires approval (model could be reading secrets then exfiltrating them).
decision := "require_approval" if {
    input.tool_name == "draft_email"
    _prior_read_file
}

_prior_read_file if {
    input.context.prior_tool == "read_file"
}

# send_email to a known-safe recipient still requires human approval.
decision := "require_approval" if {
    input.tool_name == "send_email"
    input.args.to in email_allowlist
}
# send_email to any other recipient: default "deny" blocks it.

# ── Filesystem MCP tools (proxied through gateway) ───────────────────────────

# Read-only filesystem operations: always allow
decision := "allow" if {
    input.tool_name in {
        "list_directory", "directory_tree", "search_files",
        "get_file_info", "list_allowed_directories", "read_multiple_files",
    }
}

# Filesystem writes: reuse same path rules as write_file
decision := "allow" if {
    input.tool_name in {"create_directory"}
    startswith(input.args.path, "drafts/")
}

decision := "require_approval" if {
    input.tool_name in {"move_file", "create_directory"}
    not startswith(input.args.path, "drafts/")
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
