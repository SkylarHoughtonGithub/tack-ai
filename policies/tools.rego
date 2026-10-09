package tack.policy

import rego.v1

policy_version := "1.0.0"

# Fail-closed: anything without an explicit allow/require_approval rule is denied.
default decision := "deny"
default rule := "default_deny"

# ── Read-only tools ──────────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name in {"web_search", "read_file", "search_documents", "search_code_chunks"}
}
rule := "read_only" if {
    input.tool_name in {"web_search", "read_file", "search_documents", "search_code_chunks"}
}

# ── Test runner ───────────────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name == "run_tests"
}
rule := "run_tests_allow" if {
    input.tool_name == "run_tests"
}

# ── File writes ──────────────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name == "write_file"
    startswith(input.args.path, "drafts/")
}
rule := "write_drafts_allow" if {
    input.tool_name == "write_file"
    startswith(input.args.path, "drafts/")
}

decision := "require_approval" if {
    input.tool_name == "write_file"
    not startswith(input.args.path, "drafts/")
}
rule := "write_approval" if {
    input.tool_name == "write_file"
    not startswith(input.args.path, "drafts/")
}

# ── Code execution ───────────────────────────────────────────────────────────

decision := "require_approval" if {
    input.tool_name == "run_code"
}
rule := "code_execution_approval" if {
    input.tool_name == "run_code"
}

# ── Email ────────────────────────────────────────────────────────────────────

email_allowlist := {"skylarhoughton1996@gmail.com"}

decision := "allow" if {
    input.tool_name == "draft_email"
    not _prior_read_file
}
rule := "draft_email_allow" if {
    input.tool_name == "draft_email"
    not _prior_read_file
}

# Confused-deputy guard — drafting email immediately after reading a file requires approval.
decision := "require_approval" if {
    input.tool_name == "draft_email"
    _prior_read_file
}
rule := "draft_email_confused_deputy" if {
    input.tool_name == "draft_email"
    _prior_read_file
}

_prior_read_file if {
    input.context.prior_tool == "read_file"
}

decision := "require_approval" if {
    input.tool_name == "send_email"
    input.args.to in email_allowlist
}
rule := "send_email_allowlist_approval" if {
    input.tool_name == "send_email"
    input.args.to in email_allowlist
}

# ── Filesystem MCP tools ──────────────────────────────────────────────────────

decision := "allow" if {
    input.tool_name in {
        "list_directory", "directory_tree", "search_files",
        "get_file_info", "list_allowed_directories", "read_multiple_files",
    }
}
rule := "mcp_fs_read_only" if {
    input.tool_name in {
        "list_directory", "directory_tree", "search_files",
        "get_file_info", "list_allowed_directories", "read_multiple_files",
    }
}

decision := "allow" if {
    input.tool_name in {"create_directory"}
    startswith(input.args.path, "drafts/")
}
rule := "mcp_fs_write_drafts_allow" if {
    input.tool_name in {"create_directory"}
    startswith(input.args.path, "drafts/")
}

decision := "require_approval" if {
    input.tool_name in {"move_file", "create_directory"}
    not startswith(input.args.path, "drafts/")
}
rule := "mcp_fs_write_approval" if {
    input.tool_name in {"move_file", "create_directory"}
    not startswith(input.args.path, "drafts/")
}

# ── File deletion — no allow rule; stays at default "deny" ───────────────────

# ── Routing approval ──────────────────────────────────────────────────────────

decision := "require_approval" if {
    input.tool_name == "__route__"
    input.args.tier == "deep_reasoning"
    input.args.estimated_cost_usd > 2.0
}
rule := "route_deep_reasoning_expensive" if {
    input.tool_name == "__route__"
    input.args.tier == "deep_reasoning"
    input.args.estimated_cost_usd > 2.0
}

decision := "allow" if {
    input.tool_name == "__route__"
    not _expensive_deep_reasoning
}
rule := "route_allow" if {
    input.tool_name == "__route__"
    not _expensive_deep_reasoning
}

_expensive_deep_reasoning if {
    input.args.tier == "deep_reasoning"
    input.args.estimated_cost_usd > 2.0
}
