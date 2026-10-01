import hashlib
import json
import re
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path

from tack_ai.models import AuditRecord

DB_PATH = Path(__file__).resolve().parents[2] / "logs" / "audit.db"

# Set this before each agent run so policy.py can tag records without arg threading.
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
    """Stable JSON of all AuditRecord fields except `hash` itself."""
    return json.dumps(record.model_dump(mode="json", exclude={"hash"}), sort_keys=True)


def _compute_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode()).hexdigest()


# Reconstruct the same canonical dict from a raw DB row dict so verify_chain
# re-hashes each record from its stored column values — not from a cached blob.
# Every key must match what model_dump(mode="json") produces when writing.
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


_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT NOT NULL,
        actor           TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        routing_tier    TEXT,
        routing_reason  TEXT,
        model           TEXT,
        provider        TEXT,
        tool_name       TEXT,
        tool_args       TEXT,
        policy_decision TEXT,
        policy_version  TEXT,
        approver        TEXT,
        approved_at     TEXT,
        outcome         TEXT,
        cost_usd        REAL,
        prev_hash       TEXT NOT NULL,
        hash            TEXT NOT NULL,
        timestamp       TEXT NOT NULL
    )
"""


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE)
    conn.commit()


def _last_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else ""


def append(record: AuditRecord, *, db_path: Path = DB_PATH) -> AuditRecord:
    """Append a record to the audit trail. Sets prev_hash and hash; returns the completed record."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        _init_db(conn)
        record = record.model_copy(update={"prev_hash": _last_hash(conn)})
        record = record.model_copy(update={"hash": _compute_hash(_canonical(record))})
        d = record.model_dump(mode="json")
        conn.execute(
            """
            INSERT INTO audit_log (
                run_id, actor, event_type, routing_tier, routing_reason,
                model, provider, tool_name, tool_args, policy_decision,
                policy_version, approver, approved_at, outcome, cost_usd,
                prev_hash, hash, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        conn.commit()
    return record


def verify_chain(*, db_path: Path = DB_PATH) -> tuple[bool, str]:
    """Recompute every hash from individual columns. Reports the first broken link."""
    if not db_path.exists():
        return True, "No audit log found — nothing to verify."
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    if not rows:
        return True, "Audit log is empty."
    prev = ""
    for row in rows:
        row = dict(row)
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


def replay(run_id: str, *, db_path: Path = DB_PATH) -> None:
    """Print the full story of a run from audit records alone."""
    if not db_path.exists():
        print(f"No audit log at {db_path}")
        return
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
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
