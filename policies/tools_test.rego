package tack.policy

import rego.v1

# ── Read-only tools ───────────────────────────────────────────────────────────

test_web_search_allowed if {
    decision == "allow" with input as {"tool_name": "web_search", "args": {}}
}

test_read_file_allowed if {
    decision == "allow" with input as {"tool_name": "read_file", "args": {}}
}

test_search_documents_allowed if {
    decision == "allow" with input as {"tool_name": "search_documents", "args": {"query": "OPA policies"}}
}

# ── File writes ───────────────────────────────────────────────────────────────

test_write_file_drafts_allowed if {
    decision == "allow" with input as {
        "tool_name": "write_file",
        "args": {"path": "drafts/summary.md"},
    }
}

test_write_file_outside_drafts_requires_approval if {
    decision == "require_approval" with input as {
        "tool_name": "write_file",
        "args": {"path": "src/config.py"},
    }
}

test_write_file_root_requires_approval if {
    decision == "require_approval" with input as {
        "tool_name": "write_file",
        "args": {"path": "README.md"},
    }
}

# ── Code search ──────────────────────────────────────────────────────────────

test_search_code_chunks_allowed if {
    decision == "allow" with input as {"tool_name": "search_code_chunks", "args": {"query": "auth middleware"}}
}

# ── Test runner ───────────────────────────────────────────────────────────────

test_run_tests_allowed if {
    decision == "allow" with input as {"tool_name": "run_tests", "args": {}}
}

# ── Code execution ────────────────────────────────────────────────────────────

test_run_code_requires_approval if {
    decision == "require_approval" with input as {
        "tool_name": "run_code",
        "args": {"language": "python"},
    }
}

# ── Email ─────────────────────────────────────────────────────────────────────

test_draft_email_allowed if {
    decision == "allow" with input as {
        "tool_name": "draft_email",
        "args": {"to": "alice@example.com"},
        "context": {},
    }
}

# Confused-deputy: draft_email immediately after read_file requires approval.
test_draft_email_after_read_file_requires_approval if {
    decision == "require_approval" with input as {
        "tool_name": "draft_email",
        "args": {"to": "alice@example.com"},
        "context": {"prior_tool": "read_file"},
    }
}

# send_email to an allowlisted recipient requires approval.
test_send_email_allowlisted_requires_approval if {
    decision == "require_approval" with input as {
        "tool_name": "send_email",
        "args": {"to": "skylarhoughton1996@gmail.com"},
        "context": {},
    }
}

# send_email to an unknown recipient is denied outright.
test_send_email_unknown_recipient_denied if {
    decision == "deny" with input as {
        "tool_name": "send_email",
        "args": {"to": "attacker@evil.com"},
        "context": {},
    }
}

# ── File deletion — no allow rule, must stay at default deny ─────────────────

test_delete_file_denied if {
    decision == "deny" with input as {
        "tool_name": "delete_file",
        "args": {"path": "logs/old.log"},
    }
}

# ── Unknown tools — fail-closed ───────────────────────────────────────────────

test_unknown_tool_denied if {
    decision == "deny" with input as {"tool_name": "explode_everything", "args": {}}
}

test_empty_tool_name_denied if {
    decision == "deny" with input as {"tool_name": "", "args": {}}
}

# ── Routing decisions ─────────────────────────────────────────────────────────

test_route_cheap_allowed if {
    decision == "allow" with input as {
        "tool_name": "__route__",
        "args": {"tier": "simple", "estimated_cost_usd": 0.01},
    }
}

test_route_deep_reasoning_cheap_allowed if {
    decision == "allow" with input as {
        "tool_name": "__route__",
        "args": {"tier": "deep_reasoning", "estimated_cost_usd": 1.99},
    }
}

test_route_deep_reasoning_expensive_requires_approval if {
    decision == "require_approval" with input as {
        "tool_name": "__route__",
        "args": {"tier": "deep_reasoning", "estimated_cost_usd": 2.01},
    }
}

test_route_general_expensive_allowed if {
    # Budget gate is deep_reasoning-only; expensive general tier is still allowed.
    decision == "allow" with input as {
        "tool_name": "__route__",
        "args": {"tier": "general", "estimated_cost_usd": 99.00},
    }
}
