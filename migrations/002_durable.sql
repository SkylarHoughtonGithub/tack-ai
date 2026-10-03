-- Phase 9: durable execution tables

-- pending_approvals: tracks tool calls waiting for human approval.
-- Rows persist across process restarts — the tool polls until status changes.
CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id  TEXT        PRIMARY KEY,
    run_id       TEXT        NOT NULL,
    tool_name    TEXT        NOT NULL,
    args         JSONB       NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'denied'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMPTZ,
    resolver     TEXT,

    UNIQUE (run_id, tool_name)
);

-- sent_emails: idempotency log for send_email.
-- Before sending, the tool checks this table by content hash.
-- If a matching row exists, it returns the cached result without re-sending.
CREATE TABLE IF NOT EXISTS sent_emails (
    idempotency_key TEXT        PRIMARY KEY,
    recipient       TEXT        NOT NULL,
    subject         TEXT        NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result          TEXT        NOT NULL
);
