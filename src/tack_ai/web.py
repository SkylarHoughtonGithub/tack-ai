"""
Phase 10 — Web console for the AI agent harness.

Routes
------
GET  /              Home: submit task + dashboard
GET  /login         Login form
POST /login         Authenticate and set session cookie
GET  /logout        Clear session
POST /tasks         Submit a new task (form)
GET  /tasks/{id}    Task detail page
GET  /tasks/{id}/stream  SSE stream of task events
GET  /approvals     Pending approvals page
POST /approvals/{id}/approve
POST /approvals/{id}/deny
GET  /audit         Audit browser with chain-verify status

Start with:
    uv run uvicorn tack_ai.web:app --reload
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from tack_ai.audit import DB_PATH, append, verify_chain
from tack_ai.config import Settings
from tack_ai.models import AuditRecord, PolicyDecision
from tack_ai.policy import _approval_override

settings = Settings()

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"

app = FastAPI(title="Tack-AI Console")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tojson"] = lambda v, indent=None: json.dumps(v, indent=indent)

# ── Session store ─────────────────────────────────────────────────────────────

_sessions: dict[str, str] = {}  # token -> username


def _get_user(request: Request) -> str | None:
    token = request.cookies.get("session", "")
    return _sessions.get(token)


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# ── Run state ─────────────────────────────────────────────────────────────────

class RunState:
    def __init__(self, run_id: str, question: str, username: str) -> None:
        self.run_id = run_id
        self.question = question
        self.username = username
        self.status = "running"
        self.events: list[dict[str, Any]] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.cost_usd: float | None = None
        self.tier: str | None = None

    def push(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self.events.append(event)


_runs: dict[str, RunState] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}  # approval_id -> info + asyncio.Event


# ── Background runner ─────────────────────────────────────────────────────────

async def _web_request_approval(
    tool_name: str, args: dict[str, Any], run_id: str, state: RunState
) -> bool:
    approval_id = f"{run_id[:8]}:{tool_name}"
    done = asyncio.Event()
    _pending_approvals[approval_id] = {
        "approval_id": approval_id,
        "done": done,
        "approved": False,
        "tool_name": tool_name,
        "args": {k: str(v)[:300] for k, v in args.items()},
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approver": None,
    }
    state.push({
        "type": "approval_required",
        "approval_id": approval_id,
        "tool_name": tool_name,
        "args": {k: str(v)[:300] for k, v in args.items()},
    })
    await done.wait()
    info = _pending_approvals.pop(approval_id, {})
    return bool(info.get("approved"))


async def _run_agent_bg(run_id: str, state: RunState) -> None:
    from tack_ai.agent import (  # noqa: PLC0415
        _CACHE_SETTINGS,
        _build_model_with_fallback,
        _estimate_cost,
        agent,
    )
    from tack_ai.audit import current_run_id  # noqa: PLC0415
    from tack_ai.router import RuleBasedRouter, load_model_config  # noqa: PLC0415

    current_run_id.set(run_id)
    _approval_override.set(
        lambda tool, args: _web_request_approval(tool, args, run_id, state)
    )

    try:
        state.push({"type": "status", "message": "Routing…"})
        route = RuleBasedRouter().route(state.question)
        state.tier = route.tier.value
        state.push({
            "type": "routing",
            "tier": route.tier.value,
            "effort": route.reasoning_effort.value,
            "reason": route.reason,
        })

        model_config = load_model_config()
        model, model_str = _build_model_with_fallback(route, model_config)
        provider = model_str.split(":")[0]

        append(AuditRecord(
            run_id=run_id,
            actor=state.username,
            event_type="routing",
            routing_tier=route.tier.value,
            routing_reason=route.reason,
            model=model_str,
            provider=provider,
        ))

        state.push({"type": "status", "message": f"Running on {model_str}…"})

        result = await agent.run(
            state.question,
            model=model,
            model_settings=_CACHE_SETTINGS if provider == "anthropic" else None,
        )

        cost = _estimate_cost(result.usage, model_str)
        state.cost_usd = cost
        out = result.output.model_dump()

        append(AuditRecord(
            run_id=run_id,
            actor=state.username,
            event_type="outcome",
            model=model_str,
            provider=provider,
            outcome=out["summary"][:200],
            cost_usd=cost,
        ))

        state.push({
            "type": "answer",
            "summary": out["summary"],
            "sources": out.get("sources", []),
            "confidence": out.get("confidence", 0),
            "cost_usd": cost,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        })
        state.status = "done"

    except Exception as exc:
        state.push({"type": "error", "message": str(exc)})
        state.status = "failed"
    finally:
        state.push({"type": "done"})


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request, error: str = "") -> Response:
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": error})


@app.post("/login")
async def login_post(
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    if username == settings.web_username and password == settings.web_password:
        token = secrets.token_urlsafe(32)
        _sessions[token] = username
        redir = RedirectResponse("/", status_code=303)
        redir.set_cookie("session", token, httponly=True, samesite="lax")
        return redir
    return RedirectResponse("/login?error=Invalid+credentials", status_code=303)


@app.get("/logout")
async def logout(request: Request) -> Response:
    token = request.cookies.get("session", "")
    _sessions.pop(token, None)
    redir = RedirectResponse("/login", status_code=303)
    redir.delete_cookie("session")
    return redir


# ── Task routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()

    recent = list(reversed(list(_runs.values())))[:20]
    cost_today = sum(r.cost_usd or 0.0 for r in _runs.values())
    tier_counts: dict[str, int] = {}
    for r in _runs.values():
        if r.tier:
            tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1

    return templates.TemplateResponse(request, "index.html", {
        "user": user,
        "stats": {
            "cost_today": cost_today,
            "runs_today": len(_runs),
            "pending": len(_pending_approvals),
            "tier_counts": tier_counts,
        },
        "recent_runs": [
            {
                "run_id": r.run_id,
                "question": r.question[:80],
                "tier": r.tier,
                "status": r.status,
                "cost_usd": r.cost_usd,
            }
            for r in recent
        ],
    })


@app.post("/tasks")
async def submit_task(request: Request, question: str = Form(...)) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()
    if not question.strip():
        return RedirectResponse("/", status_code=303)

    run_id = str(uuid.uuid4())
    state = RunState(run_id=run_id, question=question.strip(), username=user)
    _runs[run_id] = state
    asyncio.create_task(_run_agent_bg(run_id, state))
    return RedirectResponse(f"/tasks/{run_id}", status_code=303)


@app.get("/tasks/{run_id}")
async def task_detail(run_id: str, request: Request) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()
    state = _runs.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Task not found")
    return templates.TemplateResponse(request, "task.html", {
        "user": user,
        "run_id": run_id,
        "question": state.question,
        "status": state.status,
    })


@app.get("/tasks/{run_id}/stream")
async def task_stream(run_id: str, request: Request, last_id: int = 0) -> Response:
    user = _get_user(request)
    if not user:
        return Response("Unauthorized", status_code=401)
    state = _runs.get(run_id)
    if not state:
        return Response("Not found", status_code=404)

    async def generate():
        idx = last_id
        while True:
            if idx < len(state.events):
                event = state.events[idx]
                yield f"id: {idx}\ndata: {json.dumps(event)}\n\n"
                idx += 1
            elif state.status != "running":
                # Drain any final events before closing
                while idx < len(state.events):
                    yield f"id: {idx}\ndata: {json.dumps(state.events[idx])}\n\n"
                    idx += 1
                break
            else:
                await asyncio.sleep(0.25)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Approval routes ───────────────────────────────────────────────────────────

@app.get("/approvals")
async def approvals_page(request: Request) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()
    items = [
        {k: v for k, v in a.items() if k != "done"}
        for a in _pending_approvals.values()
    ]
    return templates.TemplateResponse(request, "approvals.html", {
        "user": user,
        "approvals": items,
    })


@app.post("/approvals/{approval_id}/approve")
async def approve_action(approval_id: str, request: Request) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()
    info = _pending_approvals.get(approval_id)
    if not info:
        return RedirectResponse("/approvals", status_code=303)

    info["approved"] = True
    info["approver"] = user
    append(AuditRecord(
        run_id=info["run_id"],
        actor=user,
        event_type="approval",
        tool_name=info["tool_name"],
        tool_args=info["args"],
        policy_decision=PolicyDecision.require_approval,
        approver=user,
        approved_at=datetime.now(timezone.utc),
        outcome="approved",
    ))
    info["done"].set()
    return RedirectResponse("/approvals", status_code=303)


@app.post("/approvals/{approval_id}/deny")
async def deny_action(approval_id: str, request: Request) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()
    info = _pending_approvals.get(approval_id)
    if not info:
        return RedirectResponse("/approvals", status_code=303)

    info["approved"] = False
    info["approver"] = user
    append(AuditRecord(
        run_id=info["run_id"],
        actor=user,
        event_type="approval",
        tool_name=info["tool_name"],
        tool_args=info["args"],
        policy_decision=PolicyDecision.require_approval,
        approver=user,
        approved_at=datetime.now(timezone.utc),
        outcome="denied",
    ))
    info["done"].set()
    return RedirectResponse("/approvals", status_code=303)


# ── Audit route ───────────────────────────────────────────────────────────────

@app.get("/audit")
async def audit_page(
    request: Request,
    run_id: str = "",
    actor: str = "",
    tool: str = "",
) -> Response:
    user = _get_user(request)
    if not user:
        return _redirect_login()

    ok, msg = verify_chain()
    records: list[dict[str, Any]] = []
    total = 0

    if DB_PATH.exists():
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            clauses: list[str] = []
            params: list[str] = []
            if run_id:
                clauses.append("run_id LIKE ?")
                params.append(f"{run_id}%")
            if actor:
                clauses.append("actor = ?")
                params.append(actor)
            if tool:
                clauses.append("tool_name = ?")
                params.append(tool)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT 200",
                params,
            ).fetchall()
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM audit_log {where}", params
            ).fetchone()
            total = total_row[0] if total_row else 0
            records = [dict(r) for r in rows]

    return templates.TemplateResponse(request, "audit.html", {
        "user": user,
        "records": records,
        "total": total,
        "chain_ok": ok,
        "chain_msg": msg,
        "filters": {"run_id": run_id, "actor": actor, "tool": tool},
    })
