# ADR-002: Use OPA as the primary policy engine

**Status:** Accepted  
**Date:** 2025-10

---

## Context

The agent must evaluate three categories of policy before any tool call executes:

1. **Tool authorization** — is this tool allowed for this user/context?
2. **Routing decisions** — which model tier/provider should handle this task?
3. **Budget enforcement** — has the per-task cost cap been reached?

Multiple policy engines exist: OPA (Rego), Cedar, inline Python `if` statements.

## Decision

Use **OPA** for all policy categories. **Cedar** appears in Phase 11 as a
comparison module covering tool-authorization only — not routing or budget.

## Rationale

- **Scope coverage:** Cedar is an authorization engine. It handles "can user X
  do action Y on resource Z?" cleanly. It cannot express routing rules
  ("if this task has more than 80 words, use the batch execution path") or
  budget rules ("deny if cumulative_cost > 0.10"). OPA can.
- **Policy-as-code:** Rego policies live in `policies/` and are version-controlled.
  Changing a rule is a git commit and a policy server restart — no code change.
- **Fail-closed by default:** OPA connection failures return `deny`. An agent
  that cannot reach its policy server executes nothing.
- **Separation of concerns:** The model proposes tool calls; OPA decides whether
  they execute. A model can be persuaded to bypass a check; a policy engine cannot.
- **Deterministic:** Policy decisions are reproducible given the same input.
  This makes the audit trail meaningful — the policy version is recorded
  alongside each decision.

## Consequences

- Requires a running OPA server (`opa run --server`). The `task opa` command
  and Docker Compose handle this.
- Rego is its own language; newcomers need to learn it. The policies in
  `policies/tools.rego` are kept short and heavily commented.
- Cedar is present in `cedar/` as a concrete comparison. Operators can swap
  `POLICY_ENGINE=cedar` to use it for authorization checks.
