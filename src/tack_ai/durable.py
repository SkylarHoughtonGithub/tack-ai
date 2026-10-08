"""
Phase 9 — Durable execution via Pydantic AI's DBOS integration.

Concepts demonstrated
─────────────────────
workflow  Every model request inside a @DBOS.workflow becomes a persisted DBOS
          step.  Killing the process and restarting with the same workflow_id
          resumes from the last completed model request — earlier model calls
          are NOT repeated.

approval  A pending_approvals Postgres row stands in for the old terminal
          prompt.  The tool polls that row every 5 s; the row survives
          restarts so a retried tool call finds the same record.

idempotent  send_email hashes (to, subject, body) into a key stored in
            sent_emails.  A retry returns the cached result without re-sending.

Usage
─────
    uv run python scripts/durable_run.py "What is MCP?" --workflow-id my-run-1
    uv run python scripts/send_approval.py <approval_id> --approve

Prerequisites
─────────────
    docker compose up -d postgres
    opa run --server --addr :8181 policies/
    # .env must have DATABASE_URL and ANTHROPIC_API_KEY
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
import warnings
from typing import Any

from tack_ai.core.config import Settings

settings = Settings()

# ── DBOS availability ──────────────────────────────────────────────────────────
try:
    from dbos import DBOS, DBOSConfig, SetWorkflowID

    _DBOS_AVAILABLE = True
except ImportError:
    _DBOS_AVAILABLE = False

# ── Lazy-init state ────────────────────────────────────────────────────────────
_dbos_instance: Any = None
_durable_agent: Any = None
_agent_workflow_fn: Any = None


def _init_dbos() -> None:
    """Connect DBOS to Postgres and build the durable agent wrapper.

    Called on the first durable run so that importing this module never fails
    even when dbos is not installed or DATABASE_URL is not set.
    """
    global _dbos_instance, _durable_agent, _agent_workflow_fn

    if _dbos_instance is not None:
        return

    if not _DBOS_AVAILABLE:
        raise RuntimeError("dbos package not installed.  Run: uv add dbos 'pydantic-ai[dbos]'")
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for durable mode.")

    # Import here so that importing durable.py at the top level never triggers
    # agent.py's module-level provider checks.
    from tack_ai.agent import _CACHE_SETTINGS
    from tack_ai.agent import agent as _base_agent  # noqa: PLC0415

    # DBOS uses its own system tables in the same Postgres instance.
    config = DBOSConfig(
        name="tack-ai",
        system_database_url=settings.database_url,
    )
    _dbos_instance = DBOS(config=config)
    DBOS.launch()

    # DBOSAgent (deprecated wrapper) is the simplest path for Phase 9 — it
    # re-wraps every @tool_plain from the base agent so all tools keep working.
    # Future migration: replace with Agent(..., capabilities=[DBOSDurability()]).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from pydantic_ai.durable_exec.dbos import DBOSAgent  # noqa: PLC0415

        _durable_agent = DBOSAgent(wrapped=_base_agent, name="tack-ai-research")

    @DBOS.workflow()
    async def _agent_wf(question: str, run_id: str) -> dict:
        """Outer DBOS workflow.  Each model request becomes a durable step.

        DBOSAgent wraps every model request as a DBOS step, so:
        - If killed BETWEEN two model requests, only incomplete ones re-run.
        - Earlier completed model calls are replayed from DBOS state (not from
          the Anthropic API), so they are free and instant on recovery.
        """
        from tack_ai.audit import current_run_id  # noqa: PLC0415
        from tack_ai.policy import durable_mode  # noqa: PLC0415

        current_run_id.set(run_id)
        durable_mode.set(True)
        try:
            result = await _durable_agent.run(
                question,
                model_settings=_CACHE_SETTINGS,
            )
        finally:
            durable_mode.set(False)

        return result.output.model_dump()

    _agent_workflow_fn = _agent_wf


# ── DB-backed approval (called by policy.py in durable mode) ──────────────────


def _approval_id(run_id: str, tool_name: str) -> str:
    return hashlib.sha256(f"{run_id}:{tool_name}".encode()).hexdigest()[:16]


async def wait_for_db_approval(
    run_id: str,
    tool_name: str,
    args: dict,
    timeout_seconds: int = 172_800,  # 48 h
) -> bool:
    """Create (or find) a pending_approvals row and poll until resolved.

    The row is keyed by sha256(run_id:tool_name)[:16].  A retried tool call
    on restart finds the same record and resumes waiting — or returns
    immediately if it was already resolved.
    """
    import psycopg  # noqa: PLC0415

    approval_id = _approval_id(run_id, tool_name)
    assert settings.database_url, "DATABASE_URL required for durable approval"

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        await conn.execute(
            """
            INSERT INTO pending_approvals (approval_id, run_id, tool_name, args)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (approval_id) DO NOTHING
            """,
            (approval_id, run_id, tool_name, json.dumps(args)),
        )
        await conn.commit()

    print(f"\n{'─' * 54}")
    print("  DURABLE APPROVAL REQUIRED")
    print(f"  Tool:        {tool_name}")
    print(f"  Approval ID: {approval_id}")
    print(f"  Run ID:      {run_id}")
    print(f"  Approve:  uv run python scripts/send_approval.py {approval_id} --approve")
    print(f"  Deny:     uv run python scripts/send_approval.py {approval_id} --deny")
    print(f"{'─' * 54}")

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM pending_approvals WHERE approval_id = %s",
                    (approval_id,),
                )
                row = await cur.fetchone()

        if row and row[0] != "pending":
            approved = row[0] == "approved"
            print(f"  → {'Approved' if approved else 'Denied'} (id={approval_id})\n")
            return approved

        await asyncio.sleep(5)

    # Timeout: mark as denied.
    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        await conn.execute(
            """
            UPDATE pending_approvals
            SET status = 'denied', resolved_at = NOW(), resolver = 'timeout'
            WHERE approval_id = %s
            """,
            (approval_id,),
        )
        await conn.commit()
    print(f"  → Approval timed out after {timeout_seconds // 3600} h (id={approval_id})\n")
    return False


# ── Idempotent email (called by agent.py's send_email tool) ───────────────────


def _email_key(to: str, subject: str, body: str) -> str:
    return hashlib.sha256(f"{to}\0{subject}\0{body}".encode()).hexdigest()[:16]


async def check_or_record_email(to: str, subject: str, body: str) -> str:
    """Return a cached result if this exact email was already sent; else send.

    Idempotency key = sha256(to|subject|body)[:16].  A step retry after a crash
    returns the stored result instead of re-sending.
    """
    import psycopg  # noqa: PLC0415

    key = _email_key(to, subject, body)
    assert settings.database_url, "DATABASE_URL required for idempotent email"

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT result FROM sent_emails WHERE idempotency_key = %s",
                (key,),
            )
            row = await cur.fetchone()

        if row:
            return f"[dedup] {row[0]}"

        result = f"[stub] Email sent to {to}."
        await conn.execute(
            """
            INSERT INTO sent_emails (idempotency_key, recipient, subject, result)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (key, to, subject, result),
        )
        await conn.commit()

    return result


# ── Public entry point ─────────────────────────────────────────────────────────


async def run_durable(question: str, workflow_id: str | None = None) -> None:
    """Start or resume a durable agent run.

    If workflow_id matches a previous run, DBOS returns the existing workflow
    handle and resumes from the last completed model-request step.
    """
    _init_dbos()

    from tack_ai.audit import append, current_run_id, verify_chain  # noqa: PLC0415
    from tack_ai.core.models import AuditRecord  # noqa: PLC0415
    from tack_ai.core.router import RuleBasedRouter  # noqa: PLC0415

    run_id = workflow_id or str(uuid.uuid4())
    wf_id = f"tack-ai-{run_id}"

    current_run_id.set(run_id)

    route = RuleBasedRouter().route(question)

    print(f"\nQuestion: {question}")
    print(f"Workflow: {wf_id}  (restart with --workflow-id {run_id})")
    print(f"Route:    {route.tier.value} | {route.reason}")
    print("Durable:  yes (DBOS → Postgres)")

    await append(
        AuditRecord(
            run_id=run_id,
            actor="user",
            event_type="routing",
            routing_tier=route.tier.value,
            routing_reason=route.reason,
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
        )
    )

    # SetWorkflowID scopes the next start_workflow_async call.
    # workflow_id_reuse_policy='return-existing' means: if this workflow ID
    # already exists (crashed run), return its handle and resume it.
    with SetWorkflowID(wf_id, workflow_id_reuse_policy="return-existing"):
        handle = await DBOS.start_workflow_async(_agent_workflow_fn, question, run_id)

    result_dict: dict = await handle.get_result()

    print("\n=== Answer ===")
    print(f"Summary:    {result_dict['summary']}")
    print(f"Sources:    {result_dict.get('sources', [])}")
    print(f"Confidence: {result_dict.get('confidence', 0):.2f}")

    await append(
        AuditRecord(
            run_id=run_id,
            actor="user",
            event_type="outcome",
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            outcome=result_dict["summary"][:200],
        )
    )

    ok, chain_msg = await verify_chain()
    print("\n=== Audit ===")
    print(f"Chain: {'✓' if ok else '✗'}  {chain_msg}")
    print(f"Replay: uv run python -c \"from tack_ai.audit import replay; replay('{run_id}')\"")

    DBOS.destroy()
