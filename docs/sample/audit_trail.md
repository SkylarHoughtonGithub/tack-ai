# Audit Trail

## Purpose

The audit trail answers the question: "Who did what, who allowed it, and under which policy version?" It is a permanent, append-only record of every significant event in the agent's lifecycle — routing decisions, tool calls, policy outcomes, approvals, and cost.

This is distinct from tracing (OpenTelemetry → Logfire), which answers "what happened and why was it slow?" Traces are diagnostic; the audit trail is a compliance and accountability record.

## Hash Chain

Every audit record includes two fields set automatically by `audit.append()`:

- `prev_hash` — the SHA-256 hash of the previous record's canonical JSON
- `hash` — the SHA-256 hash of this record's canonical JSON (including `prev_hash`)

This forms a hash chain: if any historical record is modified, deleted, or reordered, `verify_chain()` will detect it because the hashes no longer match.

```python
ok, msg = audit.verify_chain()
# "Chain intact: 42 records verified." or "Tamper detected at record 17"
```

## Record Fields

Each `AuditRecord` captures:

- `run_id` — groups all events for a single agent run
- `actor` — who initiated the action (user ID)
- `event_type` — `routing`, `tool_call`, `policy_decision`, `approval`, `outcome`
- `tool_name` / `tool_args` — what was called (args are redacted for secrets)
- `policy_decision` — `allow`, `deny`, or `require_approval`
- `policy_version` — the Rego policy version in effect at decision time
- `approver` / `approved_at` — set when a human resolves a `require_approval`
- `cost_usd` — estimated model cost for the run
- `prev_hash` / `hash` — chain integrity fields

## Replay

Any run can be reconstructed from the audit trail alone:

```bash
uv run python -c "from tack_ai.audit import replay; replay('RUN-ID-HERE')"
```

This prints every event in chronological order: the routing decision, each tool call and its policy outcome, any approvals, and the final cost.

## Storage

The audit trail is stored in a SQLite database at `logs/audit.db`. SQLite is append-friendly and needs no server. The file should be treated as immutable — do not edit it directly; use `verify_chain()` to confirm integrity.

## SOC 2 Considerations

For SOC 2 Type II compliance, an audit trail needs:
- Immutability (append-only, hash-chained)
- Actor identification (who made each request)
- Decision traceability (which policy version governed each action)
- Approval evidence (who approved sensitive actions, and when)
- Cost tracking (for budget compliance)

The current implementation satisfies the structural requirements. A production deployment would add a separate tamper-evident log store and automated `verify_chain()` runs on a schedule.
