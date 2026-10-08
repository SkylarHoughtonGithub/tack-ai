# Agent Tools

This document is the canonical reference for every tool the tack-ai agent can call, the
policy tier that governs it, and what approval is required.

The policy engine (OPA by default; Cedar in Phase 11 comparison mode) evaluates each tool
call before it executes. The decision is one of `allow`, `deny`, or `require_approval`.
`require_approval` pauses the agent and waits for a human to click Approve or Deny in the
web console (or type `y`/`n` in the CLI).

---

## Tool inventory

### `web_search`

| Field | Value |
|-------|-------|
| **Policy decision** | `allow` |
| **Approval required** | No |
| **High-risk** | No |
| **Available when** | `BRAVE_API_KEY` is set |

Search the web for current information on a topic via the Brave Search API.

**Args:**
- `query` (str) — the search query

**Notes:** The backend issues a real HTTP request to Brave Search. When the key is not set,
this tool is not registered at all — the agent will answer from its training knowledge.

**Policy path:** `tools.rego` → `input.tool_name in {"web_search", ...}`

---

### `read_file`

| Field | Value |
|-------|-------|
| **Policy decision** | `allow` |
| **Approval required** | No |
| **High-risk** | No |
| **Available when** | Always |

Read a file within the project folder. Path traversal outside the project root is blocked
at the Python layer regardless of the policy decision.

**Args:**
- `filename` (str) — path relative to the project root

**Notes:** File content is wrapped in `[UNTRUSTED CONTENT]` markers so the model treats
it as potentially adversarial input (prompt-injection defense).

**Policy path:** `tools.rego` → `input.tool_name in {"web_search", "read_file", ...}`

---

### `write_file`

| Field | Value |
|-------|-------|
| **Policy decision** | `allow` for `drafts/`, `require_approval` elsewhere |
| **Approval required** | Only for paths outside `drafts/` |
| **High-risk** | No |
| **Available when** | Always |

Write content to a file. Paths inside `drafts/` are allowed without approval; any other
path requires a human to approve the write.

**Args:**
- `path` (str) — destination path relative to project root
- `content` (str) — file content

**Policy path:** `tools.rego` → `write_file` rules

---

### `run_code`

| Field | Value |
|-------|-------|
| **Policy decision** | `require_approval` |
| **Approval required** | Yes — every invocation |
| **High-risk** | Yes |
| **Available when** | Docker is running on the host |

Execute code in a Docker sandbox with no network access, read-only filesystem, 128 MB
memory, 0.5 CPU, and a 15-second timeout. Supports Python and JavaScript.

**Args:**
- `code` (str) — source code to execute
- `language` (str, default `"python"`) — `"python"` or `"javascript"`

**Notes:** Because `run_code` is in `_HIGH_RISK_TOOLS`, prior approval for this tool in
one turn does not carry over to the next — each call requires a fresh approval.

**Policy path:** `tools.rego` → `input.tool_name == "run_code"`

---

### `draft_email`

| Field | Value |
|-------|-------|
| **Policy decision** | `allow` (normally); `require_approval` after `read_file` |
| **Approval required** | Only when preceded by `read_file` in the same run |
| **High-risk** | No |
| **Available when** | Always |

Compose an email without sending it. Returns the draft as a formatted string.

**Args:**
- `to` (str) — recipient address
- `subject` (str) — subject line
- `body` (str) — email body

**Notes:** The confused-deputy guard in OPA detects when the prior tool call was
`read_file` (indicating the model may have read a secret and is now drafting an email
with it) and escalates to `require_approval`.

**Policy path:** `tools.rego` → `draft_email` + `_prior_read_file` rules

---

### `send_email`

| Field | Value |
|-------|-------|
| **Policy decision** | `require_approval` for allowlisted addresses; `deny` for all others |
| **Approval required** | Yes — always |
| **High-risk** | Yes |
| **Available when** | Always (stubbed; integrate an SMTP provider for real sends) |

Send an email. The OPA policy maintains an `email_allowlist`; addresses outside the list
are denied without ever reaching the approval flow. Allowlisted addresses still require
explicit human approval.

**Args:**
- `to` (str) — recipient address
- `subject` (str) — subject line
- `body` (str) — email body

**Notes:** When `DATABASE_URL` is set, the durable module records `(to, subject, body)`
hashes to prevent duplicate sends on retry.

**Policy path:** `tools.rego` → `send_email` + `email_allowlist` rules

---

### `delete_file`

| Field | Value |
|-------|-------|
| **Policy decision** | `deny` (always) |
| **Approval required** | N/A — denied before approval is reached |
| **High-risk** | Yes |
| **Available when** | Always (registered so the model can attempt and receive a clear error) |

Delete a file. This operation is **never permitted** — the OPA policy has no allow rule
for `delete_file` so the default `deny` always applies. The tool is registered so the
agent receives a clean error message rather than a tool-not-found failure.

**Args:**
- `path` (str) — file path

**Policy path:** `tools.rego` → default `deny` (no explicit rule)

---

### `search_documents`

| Field | Value |
|-------|-------|
| **Policy decision** | `allow` |
| **Approval required** | No |
| **High-risk** | No |
| **Available when** | `DATABASE_URL` and `OPENAI_API_KEY` are set |

Semantic search over the indexed document store. Returns the most relevant passages for
the query using pgvector cosine similarity on OpenAI embeddings.

**Args:**
- `query` (str) — the search query
- `user` (str, default `"user"`) — used for OpenFGA access-control checks on documents

**Notes:** Results are wrapped in `[UNTRUSTED CONTENT]` markers. An audit record is
written for every retrieval so the trail shows which documents were returned to the model.

**Policy path:** `tools.rego` → `input.tool_name in {"web_search", "read_file", "search_documents"}`

---

## Policy engine

The policy layer is deterministic and runs outside the model:

```
model proposes tool call
        │
        ▼
  OPA (or Cedar)  ──── deny ────► blocked (audit record written)
        │
      allow
        │
        ▼
  execute tool
        │
   require_approval
        │
        ▼
  human approves / denies  ──► audit record written
        │
      approved
        │
        ▼
   execute tool
```

Cedar is used only for tool-authorization comparisons (Phase 11). Routing and budget
decisions always go through OPA.

## Adding a new tool

1. Implement the tool function decorated with `@agent.tool_plain` in `agent.py`.
2. Add a policy rule in `policies/tools.rego` (default is `deny`).
3. Add a row to this document.
4. If the tool is high-risk (irreversible side effects), add it to `_HIGH_RISK_TOOLS`
   in `web.py` so the web UI re-prompts for every invocation.
