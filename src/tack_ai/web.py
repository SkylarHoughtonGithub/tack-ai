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
import uuid
from datetime import datetime, timezone

from markupsafe import Markup
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from tack_ai.audit import append, query_records, verify_chain
from tack_ai.auth import get_fga_client
from tack_ai.config import Settings
from tack_ai.models import AuditRecord, PolicyDecision
from tack_ai.policy import _approval_override

settings = Settings()

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"

app = FastAPI(title="Tack-AI Console")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.exception_handler(401)
async def _on_401(request: Request, exc: HTTPException) -> Response:
    if "text/event-stream" in request.headers.get("accept", ""):
        return Response("Unauthorized", status_code=401)
    return RedirectResponse("/login", status_code=303)


def _tojson(v: object, indent: int | None = None) -> Markup:
    # Produce JSON then escape HTML-special chars using \uXXXX sequences so
    # the output is safe inside <script> tags.  Return Markup so Jinja2 does
    # not run a second HTML-escaping pass (which would turn " into &quot;).
    s = json.dumps(v, indent=indent)
    s = s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return Markup(s)

templates.env.filters["tojson"] = _tojson

# ── Session store ─────────────────────────────────────────────────────────────

_sessions: dict[str, str] = {}  # token -> username


def _get_user(request: Request) -> str | None:
    token = request.cookies.get("session", "")
    return _sessions.get(token)


async def _auth(request: Request) -> str:
    """Dependency: resolve current user or raise 401 (caught by _on_401)."""
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


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
        self.approved_tools: set[str] = set()
        self.task: asyncio.Task | None = None

    def push(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self.events.append(event)


_runs: dict[str, RunState] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}  # approval_id -> info + asyncio.Event

# Tools that must prompt on every call — never remembered across calls.
# Low-risk tools (web_search, search_documents) earn the per-run grant after first approval.
_HIGH_RISK_TOOLS = {"run_code", "send_email", "delete_file"}


# ── Background runner ─────────────────────────────────────────────────────────

async def _web_request_approval(
    tool_name: str, args: dict[str, Any], run_id: str, state: RunState
) -> bool:
    if tool_name in state.approved_tools:
        return True

    approval_id = f"{run_id[:8]}:{tool_name}:{uuid.uuid4().hex[:6]}"
    done = asyncio.Event()
    _pending_approvals[approval_id] = {
        "approval_id": approval_id,
        "done": done,
        "approved": False,
        "tool_name": tool_name,
        "args": {k: str(v)[:300] for k, v in args.items()},
        "run_id": run_id,
        "actor": state.username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approver": None,
    }
    state.push({
        "type": "approval_required",
        "approval_id": approval_id,
        "tool_name": tool_name,
        "actor": state.username,
        "args": {k: str(v)[:300] for k, v in args.items()},
    })
    await done.wait()
    info = _pending_approvals.pop(approval_id, {})
    return bool(info.get("approved"))


async def _preflight(state: RunState) -> None:
    """Push non-fatal warnings for services that are configured but unreachable."""
    import httpx  # noqa: PLC0415

    # OPA — if unreachable, every tool call will be fail-closed (denied).
    # The run still proceeds but no tool will execute.
    opa_url = "http://localhost:8181/health"
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            await c.get(opa_url)
    except Exception:
        state.push({
            "type": "warning",
            "message": (
                "OPA is not reachable — all tool calls will be denied (fail-closed).\n"
                "Start it with:  opa run --server --addr :8181 policies/"
            ),
        })

    # Postgres/pgvector — if DATABASE_URL is not set, search_documents
    # and conversation memory are silently unavailable.
    if not settings.database_url:
        state.push({
            "type": "warning",
            "message": (
                "DATABASE_URL is not set — document search and conversation memory "
                "are disabled."
            ),
        })

    # Logfire — if LOGFIRE_TOKEN is not set, tracing runs locally only.
    if not settings.logfire_token:
        state.push({
            "type": "warning",
            "message": (
                "LOGFIRE_TOKEN is not set — traces will not be sent to Logfire.\n"
                "Get a token at https://logfire.pydantic.dev and add it to .env."
            ),
        })


async def _run_agent_bg(run_id: str, state: RunState) -> None:
    from tack_ai.agent import (  # noqa: PLC0415
        _CACHE_SETTINGS,
        _USAGE_LIMITS,
        _build_model_with_fallback,
        _estimate_cost,
        _get_memory,
        agent,
    )
    from tack_ai.audit import current_run_id  # noqa: PLC0415
    from tack_ai.router import LLMRouter, RuleBasedRouter, load_model_config  # noqa: PLC0415

    current_run_id.set(run_id)
    _approval_override.set(
        lambda tool, args: _web_request_approval(tool, args, run_id, state)
    )

    try:
        await _preflight(state)
        model_config = load_model_config()
        state.push({"type": "status", "message": "Routing…"})
        if settings.router_type == "llm" and settings.openai_api_key:
            try:
                route = await LLMRouter(model_str=model_config["router"]["decision"], openai_api_key=settings.get_key("openai")).route(state.question)
            except Exception as e:
                state.push({"type": "warning", "message": f"LLM router unavailable ({e.__class__.__name__}), falling back to rule-based."})
                route = RuleBasedRouter().route(state.question)
        else:
            route = RuleBasedRouter().route(state.question)
        state.tier = route.tier.value
        state.push({
            "type": "routing",
            "tier": route.tier.value,
            "effort": route.reasoning_effort.value,
            "reason": route.reason,
        })
        model, model_str = _build_model_with_fallback(route, model_config)
        provider = model_str.split(":")[0]

        await append(AuditRecord(
            run_id=run_id,
            actor=state.username,
            event_type="routing",
            routing_tier=route.tier.value,
            routing_reason=route.reason,
            model=model_str,
            provider=provider,
        ))

        # Conversation memory — session is per-user so context persists across tasks.
        memory = _get_memory()
        session_id = state.username
        memory_context = ""
        if memory:
            try:
                memory_context = await memory.get_context(session_id)
            except Exception as e:
                state.push({"type": "warning", "message": f"Memory read failed ({e.__class__.__name__}) — running without prior context."})

        full_question = (
            f"{memory_context}\n\nUser: {state.question}" if memory_context else state.question
        )

        state.push({"type": "status", "message": f"Running on {model_str}…"})

        result = await agent.run(
            full_question,
            model=model,
            model_settings=_CACHE_SETTINGS if provider == "anthropic" else None,
            usage_limits=_USAGE_LIMITS,
        )

        cost = _estimate_cost(result.usage, model_str)
        state.cost_usd = cost
        out = result.output.model_dump()

        await append(AuditRecord(
            run_id=run_id,
            actor=state.username,
            event_type="outcome",
            model=model_str,
            provider=provider,
            outcome=out["summary"][:200],
            cost_usd=cost,
        ))

        if memory:
            try:
                await memory.add_turn(session_id, "user", state.question)
                await memory.add_turn(session_id, "assistant", out["summary"])
            except Exception as e:
                state.push({"type": "warning", "message": f"Memory write failed ({e.__class__.__name__}) — turn not saved."})

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

    except asyncio.CancelledError:
        state.push({"type": "error", "message": "Task cancelled by user."})
        state.status = "cancelled"
    except Exception as exc:
        msg = str(exc)
        if "All connection attempts failed" in msg or "Client failed to connect" in msg:
            from tack_ai.config import Settings as _S  # noqa: PLC0415
            cfg = _S()
            if cfg.mcp_gateway_url:
                msg = (
                    f"MCP gateway at {cfg.mcp_gateway_url!r} is not reachable.\n"
                    "Start it with:  uv run python -m tack_ai.mcp_gateway\n"
                    "Or unset MCP_GATEWAY_URL in .env to run without MCP tools."
                )
        state.push({"type": "error", "message": msg})
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
async def index(request: Request, user: str = Depends(_auth)) -> Response:
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
async def submit_task(request: Request, question: str = Form(...), user: str = Depends(_auth)) -> Response:
    if not question.strip():
        return RedirectResponse("/", status_code=303)

    run_id = str(uuid.uuid4())
    state = RunState(run_id=run_id, question=question.strip(), username=user)
    _runs[run_id] = state
    state.task = asyncio.create_task(_run_agent_bg(run_id, state))
    return RedirectResponse(f"/tasks/{run_id}", status_code=303)


@app.get("/tasks/{run_id}")
async def task_detail(run_id: str, request: Request, user: str = Depends(_auth)) -> Response:
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
async def task_stream(run_id: str, request: Request, last_id: int = 0, user: str = Depends(_auth)) -> Response:
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


@app.post("/tasks/{run_id}/cancel")
async def cancel_task(run_id: str, request: Request, user: str = Depends(_auth)) -> Response:
    state = _runs.get(run_id)
    if state and state.task and not state.task.done():
        state.status = "cancelled"  # set before redirect so button disappears immediately
        state.task.cancel()
    return RedirectResponse(f"/tasks/{run_id}", status_code=303)


# ── Approval routes ───────────────────────────────────────────────────────────

@app.get("/approvals")
async def approvals_page(request: Request, user: str = Depends(_auth)) -> Response:
    items = [
        {k: v for k, v in a.items() if k != "done"}
        for a in _pending_approvals.values()
    ]
    return templates.TemplateResponse(request, "approvals.html", {
        "user": user,
        "approvals": items,
    })


@app.post("/approvals/{approval_id}/approve")
async def approve_action(approval_id: str, request: Request, user: str = Depends(_auth)) -> Response:
    info = _pending_approvals.get(approval_id)
    if not info:
        return RedirectResponse("/approvals", status_code=303)

    info["approved"] = True
    info["approver"] = user
    run_state = _runs.get(info["run_id"])
    if run_state and info["tool_name"] not in _HIGH_RISK_TOOLS:
        run_state.approved_tools.add(info["tool_name"])
    fga = get_fga_client()
    if fga is not None and info["tool_name"] not in _HIGH_RISK_TOOLS:
        await fga.grant_tool(info["run_id"], info["tool_name"])
    await append(AuditRecord(
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
async def deny_action(approval_id: str, request: Request, user: str = Depends(_auth)) -> Response:
    info = _pending_approvals.get(approval_id)
    if not info:
        return RedirectResponse("/approvals", status_code=303)

    info["approved"] = False
    info["approver"] = user
    fga = get_fga_client()
    if fga is not None:
        try:
            await fga.revoke_tool(info["run_id"], info["tool_name"])
        except Exception:
            pass  # no grant existed — nothing to revoke
    await append(AuditRecord(
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
    user: str = Depends(_auth),
) -> Response:
    ok, msg = await verify_chain()
    records, total = await query_records(run_id=run_id, actor=actor, tool=tool)
    return templates.TemplateResponse(request, "audit.html", {
        "user": user,
        "records": records,
        "total": total,
        "chain_ok": ok,
        "chain_msg": msg,
        "filters": {"run_id": run_id, "actor": actor, "tool": tool},
    })


@app.get("/history")
async def history_page(request: Request, user: str = Depends(_auth)) -> Response:
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    sessions: dict[str, Any] = {}
    if settings.database_url:
        try:
            async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT session_id, role, content, turn_index, created_at "
                        "FROM conversation_turns ORDER BY session_id, turn_index"
                    )
                    turns = await cur.fetchall()
                    await cur.execute("SELECT session_id, summary, up_to_turn, updated_at FROM conversation_summaries")
                    summaries = {r["session_id"]: dict(r) for r in await cur.fetchall()}
            for t in turns:
                sid = t["session_id"]
                if sid not in sessions:
                    sessions[sid] = {"turns": [], "summary": summaries.get(sid)}
                sessions[sid]["turns"].append(dict(t))
        except Exception:
            pass

    return templates.TemplateResponse(request, "history.html", {
        "user": user,
        "sessions": sessions,
    })
