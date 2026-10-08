"""Audit trail — append-only, hash-chained, stored in Postgres."""
import hashlib
import json
import re
from contextvars import ContextVar

import psycopg
from psycopg.rows import dict_row

from tack_ai.core.models import AuditRecord
from tack_ai.observability import get_logger

_log = get_logger("tack_ai.audit")

current_run_id: ContextVar[str] = ContextVar("current_run_id", default="")

_SECRET_RES = [
    re.compile(r"sk-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_RES:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_args(args: dict) -> dict:
    return {k: redact(str(v)) if isinstance(v, str) else v for k, v in args.items()}


def _canonical(record: AuditRecord) -> str:
    return json.dumps(record.model_dump(mode="json", exclude={"hash"}), sort_keys=True)


def _compute_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row_to_canonical(row: dict) -> str:
    d = {
        "actor":           row["actor"],
        "approved_at":     row["approved_at"],
        "approver":        row["approver"],
        "cost_usd":        row["cost_usd"],
        "event_type":      row["event_type"],
        "model":           row["model"],
        "outcome":         row["outcome"],
        "policy_decision": row["policy_decision"],
        "policy_version":  row["policy_version"],
        "prev_hash":       row["prev_hash"],
        "provider":        row["provider"],
        "routing_reason":  row["routing_reason"],
        "routing_tier":    row["routing_tier"],
        "run_id":          row["run_id"],
        "timestamp":       row["timestamp"],
        "tool_args":       json.loads(row["tool_args"]) if row["tool_args"] else None,
        "tool_name":       row["tool_name"],
    }
    return json.dumps(d, sort_keys=True)


def _db_url() -> str | None:
    from tack_ai.core.config import Settings  # noqa: PLC0415
    return Settings().database_url


async def append(record: AuditRecord) -> AuditRecord:
    """Append a record to the audit trail. Sets prev_hash and hash; returns the completed record."""
    url = _db_url()
    if not url:
        return record
    async with await psycopg.AsyncConnection.connect(url, connect_timeout=5) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
            prev_hash = row["hash"] if row else ""

            record = record.model_copy(update={"prev_hash": prev_hash})
            record = record.model_copy(update={"hash": _compute_hash(_canonical(record))})
            d = record.model_dump(mode="json")

            await cur.execute(
                """
                INSERT INTO audit_log (
                    run_id, actor, event_type, routing_tier, routing_reason,
                    model, provider, tool_name, tool_args, policy_decision,
                    policy_version, approver, approved_at, outcome, cost_usd,
                    prev_hash, hash, timestamp
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    d["run_id"], d["actor"], d["event_type"],
                    d["routing_tier"], d["routing_reason"],
                    d["model"], d["provider"],
                    d["tool_name"],
                    json.dumps(d["tool_args"]) if d["tool_args"] is not None else None,
                    d["policy_decision"], d["policy_version"],
                    d["approver"], d["approved_at"],
                    d["outcome"], d["cost_usd"],
                    d["prev_hash"], d["hash"],
                    d["timestamp"],
                ),
            )
        await conn.commit()

    _log.info(
        "audit_event",
        run_id=record.run_id,
        actor=record.actor,
        event_type=record.event_type,
        tool_name=record.tool_name,
        policy_decision=record.policy_decision,
        outcome=record.outcome,
        model=record.model,
        provider=record.provider,
        routing_tier=record.routing_tier,
        cost_usd=record.cost_usd,
    )
    return record


async def verify_chain() -> tuple[bool, str]:
    """Recompute every hash from individual columns. Reports the first broken link."""
    url = _db_url()
    if not url:
        return True, "No DATABASE_URL — audit trail disabled."
    try:
        async with await psycopg.AsyncConnection.connect(url, connect_timeout=5) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT * FROM audit_log ORDER BY id")
                rows = await cur.fetchall()
    except Exception as e:
        return False, f"Database unreachable: {e}"
    if not rows:
        return True, "Audit log is empty."
    prev = ""
    for row in rows:
        canonical = _row_to_canonical(row)
        expected = _compute_hash(canonical)
        if expected != row["hash"]:
            return False, (
                f"Tamper detected at record {row['id']} (run_id={row['run_id']}): hash mismatch."
            )
        if row["prev_hash"] != prev:
            return False, (
                f"Broken chain link at record {row['id']} (run_id={row['run_id']}): prev_hash mismatch."
            )
        prev = row["hash"]
    n = len(rows)
    return True, f"Chain intact: {n} record{'s' if n != 1 else ''} verified."


async def query_records(
    run_id: str = "",
    actor: str = "",
    tool: str = "",
    limit: int = 200,
) -> tuple[list[dict], int]:
    """Return filtered audit records and total count. Returns empty list if DB is unreachable."""
    url = _db_url()
    if not url:
        return [], 0
    try:
        async with await psycopg.AsyncConnection.connect(url, connect_timeout=5) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                clauses: list[str] = []
                params: list = []
                if run_id:
                    clauses.append("run_id LIKE %s")
                    params.append(f"{run_id}%")
                if actor:
                    clauses.append("actor = %s")
                    params.append(actor)
                if tool:
                    clauses.append("tool_name = %s")
                    params.append(tool)
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

                await cur.execute(
                    f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT %s",
                    params + [limit],
                )
                records = [dict(r) for r in await cur.fetchall()]

                await cur.execute(
                    f"SELECT COUNT(*) AS cnt FROM audit_log {where}", params
                )
                row = await cur.fetchone()
                total = row["cnt"] if row else 0
        return records, total
    except Exception:
        return [], 0


async def replay(run_id: str) -> None:
    """Print the full story of a run from audit records alone."""
    url = _db_url()
    if not url:
        print("No DATABASE_URL — audit trail disabled.")
        return
    async with await psycopg.AsyncConnection.connect(url) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM audit_log WHERE run_id = %s ORDER BY id", (run_id,)
            )
            rows = await cur.fetchall()
    if not rows:
        print(f"No records found for run_id={run_id!r}")
        return
    bar = "═" * 62
    print(f"\n{bar}")
    print(f"  REPLAY  {run_id}")
    print(bar)
    for row in rows:
        print(f"\n[{row['timestamp']}]  {row['event_type'].upper()}")
        if row["routing_tier"]:
            print(f"  Tier     {row['routing_tier']}  —  {row['routing_reason']}")
        if row["model"]:
            print(f"  Model    {row['model']}  ({row['provider']})")
        if row["tool_name"]:
            print(f"  Tool     {row['tool_name']}")
        if row["tool_args"]:
            args = json.loads(row["tool_args"]) if isinstance(row["tool_args"], str) else row["tool_args"]
            for k, v in args.items():
                print(f"    {k}: {v}")
        if row["policy_decision"]:
            ver = f"  v{row['policy_version']}" if row["policy_version"] else ""
            print(f"  Policy   {row['policy_decision']}{ver}")
        if row["approver"]:
            print(f"  Approver {row['approver']}  at {row['approved_at']}")
        if row["outcome"]:
            print(f"  Outcome  {row['outcome']}")
        if row["cost_usd"] is not None:
            print(f"  Cost     ${row['cost_usd']:.5f}")
    print(f"\n{bar}")
    print(f"  {len(rows)} event{'s' if len(rows) != 1 else ''}  ·  actor: {rows[0]['actor']}")
    print(f"{bar}\n")
