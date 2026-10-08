# ADR-004: Append-only, hash-chained audit trail

**Status:** Accepted  
**Date:** 2025-08

---

## Context

The audit trail must answer "who did what, who allowed it, and under which policy
version?" in a way that cannot be silently altered after the fact. A simple
append-only log in a table is easy to modify with `UPDATE` or `DELETE`.

## Decision

Each `AuditRecord` stores a `hash` field (SHA-256 of its own canonical JSON) and
a `prev_hash` field (the hash of the preceding record). This forms a chain:
tampering with any record invalidates all subsequent records.

`verify_chain()` in `audit/core.py` recomputes the entire chain and detects any
gap or mismatch.

## Rationale

- **Tamper evidence:** Even a DBA with direct Postgres access cannot silently
  alter a policy decision record without breaking the chain. The break is
  detectable by anyone with read access.
- **Auditability requirement:** The project spec requires records of "who did
  what, who allowed it, under which policy version." A mutable log does not
  satisfy this — "under which policy version" must be unforgeable.
- **No external dependency:** The chain is self-verifying. No external
  ledger, blockchain, or trusted timestamping service is needed.
- **Low cost:** One SHA-256 hash per record. At 10 records/minute (heavy
  use), this is negligible.

## Consequences

- Records must be inserted in order; concurrent inserts require a transaction
  or sequence lock to maintain a valid chain. The current implementation inserts
  synchronously within each request, so this is not yet an issue.
- The chain cannot be "reset" to fix a corrupted record without invalidating
  everything after it. The right response to corruption is investigation, not
  repair.
- CSV/JSON export (added in P2) exports the raw records including hash fields,
  so consumers can re-verify independently.
