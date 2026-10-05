// Cedar tool-authorization policy for tack-ai (Phase 11)
//
// Scope: tool authorization only. Routing and budget rules are NOT here —
// Cedar is an authorization language and cannot express "pick model X" or
// "estimated_cost_usd > 2.0" budget checks. Those stay in OPA.
//
// Three-way decision encoding:
//   allow            → plain permit, @id starts with "allow_"
//   require_approval → permit, @id starts with "require_approval_"
//   deny             → no matching permit (Cedar's default)
//
// The Python caller (cedar_policy.py) reads the @id of the matching policy
// to distinguish "allow" from "require_approval".

// ── Read-only tools ────────────────────────────────────────────────────────

@id("allow_web_search")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"web_search"
);

@id("allow_read_file")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"read_file"
);

@id("allow_search_documents")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"search_documents"
);

// ── Filesystem MCP read-only tools ────────────────────────────────────────

@id("allow_list_directory")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"list_directory"
);

@id("allow_directory_tree")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"directory_tree"
);

@id("allow_search_files")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"search_files"
);

@id("allow_get_file_info")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"get_file_info"
);

@id("allow_list_allowed_directories")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"list_allowed_directories"
);

@id("allow_read_multiple_files")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"read_multiple_files"
);

// ── File writes ────────────────────────────────────────────────────────────

// Writes inside drafts/ are directly allowed.
@id("allow_write_file_drafts")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"write_file"
) when {
    context.path like "drafts/*"
};

// Writes anywhere else require human approval.
@id("require_approval_write_file")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"write_file"
) when {
    !(context.path like "drafts/*")
};

// ── Filesystem write/move ──────────────────────────────────────────────────

@id("allow_create_directory_drafts")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"create_directory"
) when {
    context.path like "drafts/*"
};

@id("require_approval_create_directory")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"create_directory"
) when {
    !(context.path like "drafts/*")
};

@id("require_approval_move_file")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"move_file"
) when {
    !(context.path like "drafts/*")
};

// ── Code execution ─────────────────────────────────────────────────────────

@id("require_approval_run_code")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"run_code"
);

// ── Email ──────────────────────────────────────────────────────────────────

// Normal draft (no prior read_file) is directly allowed.
@id("allow_draft_email")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"draft_email"
) when {
    context.prior_tool != "read_file"
};

// Confused-deputy guard: draft immediately after read_file requires approval.
@id("require_approval_draft_email_after_read")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"draft_email"
) when {
    context.prior_tool == "read_file"
};

// send_email to the known-safe allowlisted recipient requires approval.
@id("require_approval_send_email_allowlisted")
permit (
    principal,
    action == Action::"call",
    resource == Tool::"send_email"
) when {
    context.to == "skylarhoughton1996@gmail.com"
};

// send_email to any other address: no permit rule → denied by default.

// ── delete_file ────────────────────────────────────────────────────────────
// No permit rule for delete_file. Cedar's deny-by-default means this can
// NEVER be permitted regardless of principal, context, or future policy edits
// that don't explicitly add a permit. The formal property
// "delete_file is never permitted" holds structurally — if no permit rule
// matches, Cedar returns Deny.
