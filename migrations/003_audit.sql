-- Phase 12: move audit trail from SQLite to Postgres.
-- Runs automatically via docker-entrypoint-initdb.d on first container start.

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    routing_tier    TEXT,
    routing_reason  TEXT,
    model           TEXT,
    provider        TEXT,
    tool_name       TEXT,
    tool_args       TEXT,          -- JSON string; kept as TEXT to preserve hash-chain round-trip
    policy_decision TEXT,
    policy_version  TEXT,
    approver        TEXT,
    approved_at     TEXT,          -- ISO-8601 string; kept as TEXT for same reason
    outcome         TEXT,
    cost_usd        DOUBLE PRECISION,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_run_id ON audit_log (run_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log (actor);
CREATE INDEX IF NOT EXISTS idx_audit_tool   ON audit_log (tool_name);
