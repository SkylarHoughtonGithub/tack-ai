"""
Phase 10 — Web console for the AI agent harness.

Routes
------
GET  /                    Home: submit task + dashboard
GET  /login               Login form
POST /login               Authenticate and set session cookie
GET  /logout              Clear session
POST /tasks               Submit a new task (form)
GET  /tasks/{id}          Task detail page (multi-turn thread)
GET  /tasks/{id}/stream   SSE stream of task events
POST /tasks/{id}/message  Submit a follow-up message to a live thread
POST /tasks/{id}/close    End the conversation
POST /tasks/{id}/cancel   Cancel in-progress turn
GET  /approvals           Pending approvals page
POST /approvals/{id}/approve
POST /approvals/{id}/deny
GET  /audit               Audit browser with chain-verify status
GET  /history             Conversation history
GET  /admin/users         User management (admin only)
POST /admin/users         Create a user (admin only)
POST /admin/users/{u}/delete  Delete a user (admin only)

Start with:
    uv run tack-ai-web
    # or for manual uvicorn:
    uvicorn tack_ai.web:app --reload
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from tack_ai.audit import append, query_records, verify_chain
from tack_ai.auth import UserManager, get_fga_client
from tack_ai.config import Settings
from tack_ai.models import AuditRecord, PolicyDecision
from tack_ai.policy import _approval_override

settings = Settings()

TEMPLATES_DIR = Path(__file__).parents[2] / "templates"

app = FastAPI(title="Tack-AI Console")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SESSION_TTL = timedelta(hours=24)


@app.exception_handler(401)
async def _on_401(request: Request, exc: HTTPException) -> Response:
    if "text/event-stream" in request.headers.get("accept", ""):
        return Response("Unauthorized", status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(403)
async def _on_403(request: Request, exc: HTTPException) -> Response:
    return Response("Forbidden — admin access required", status_code=403)


def _tojson(v: object, indent: int | None = None) -> Markup:
    s = json.dumps(v, indent=indent)
    s = s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return Markup(s)


templates.env.filters["tojson"] = _tojson

# ── Session store ──────────────────────────────────────────────────────────────

@dataclass
class _Session:
    username: str
    role: str
    expires_at: datetime


_sessions: dict[str, _Session] = {}


def _get_session(request: Request) -> _Session | None:
    token = request.cookies.get("session", "")
    sess = _sessions.get(token)
    if sess is None:
        return None
    if sess.expires_at < datetime.now(timezone.utc):
        _sessions.pop(token, None)
        return None
    return sess


def _get_role(request: Request) -> str:
    sess = _get_session(request)
    return sess.role if sess else "viewer"


async def _auth(request: Request) -> str:
    """Dependency: resolve current user or raise 401."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    return sess.username


async def _require_admin(request: Request) -> str:
    """Dependency: require admin role or raise 403."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if sess.role != "admin":
        raise HTTPException(status_code=403)
    return sess.username


# ── User manager ──────────────────────────────────────────────────────────────

_user_manager: UserManager | None = None


def _get_user_manager() -> UserManager:
    global _user_manager
    if _user_manager is None:
        _user_manager = UserManager(
            db_url=settings.database_url,
            admin_username=settings.web_username,
            admin_password=settings.web_password,
        )
    return _user_manager


# ── Run state ─────────────────────────────────────────────────────────────────

class RunState:
    def __init__(self, run_id: str, question: str, username: str) -> None:
        self.run_id = run_id
        self.username = username
        self.status = "running"  # running | waiting_follow_up | closed | failed | cancelled
        self.events: list[dict[str, Any]] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.total_cost_usd: float = 0.0
        self.tier: str | None = None
        self.approved_tools: set[str] = set()
        self.task: asyncio.Task | None = None
        # Multi-turn thread
        self.turns: list[dict[str, Any]] = []  # [{question, answer, start_idx}]
        self.current_question: str = question
        # Legacy compat for dashboard display
        self.question: str = question
        self.cost_usd: float | None = None  # cost of last turn

    def push(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self.events.append(event)

    def start_turn(self, question: str) -> int:
        turn_idx = len(self.turns)
        self.turns.append({"question": question, "answer": None, "start_idx": len(self.events)})
        self.current_question = question
        self.push({"type": "turn_start", "turn_index": turn_idx, "question": question})
        return turn_idx

    def end_turn(self, answer: str | None = None) -> None:
        if self.turns:
            self.turns[-1]["answer"] = answer
        self.push({"type": "turn_end"})


_runs: dict[str, RunState] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}

_HIGH_RISK_TOOLS = {"run_code", "send_email", "delete_file"}


# ── Reasoning trace ───────────────────────────────────────────────────────────

def _extract_trace(messages: list[Any]) -> list[dict[str, Any]]:
    """
    Walk a message list and convert tool calls, results, and reasoning text
    into push-able event dicts. Accepts the list directly so it works for
    both successful runs and partial traces captured on failure.
    """
    events: list[dict[str, Any]] = []
    for msg in messages:
        for part in msg.parts:
            ptype = part.__class__.__name__
            if ptype == "ToolCallPart":
                events.append({
                    "type": "tool_call",
                    "tool_name": getattr(part, "tool_name", "?"),
                    "args": str(getattr(part, "args", ""))[:300],
                })
            elif ptype == "ToolReturnPart":
                events.append({
                    "type": "tool_result",
                    "tool_name": getattr(part, "tool_name", "?"),
                    "content": str(getattr(part, "content", ""))[:300],
                })
            elif ptype == "ThinkingPart":
                thinking = str(getattr(part, "thinking", "")).strip()
                if thinking:
                    events.append({"type": "reasoning", "content": thinking[:2000]})
            elif ptype == "TextPart":
                text = str(getattr(part, "content", "")).strip()
                if text:
                    events.append({"type": "reasoning", "content": text[:2000]})
    return events


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
    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            await c.get("http://localhost:8181/health")
    except Exception:
        state.push({
            "type": "warning",
            "message": (
                "OPA is not reachable — all tool calls will be denied (fail-closed).\n"
                "Start it with:  opa run --server --addr :8181 policies/"
            ),
        })

    if not settings.database_url:
        state.push({
            "type": "warning",
            "message": (
                "DATABASE_URL is not set — document search and conversation memory "
                "are disabled."
            ),
        })

    if not settings.logfire_token:
        state.push({
            "type": "warning",
            "message": (
                "LOGFIRE_TOKEN is not set — traces will not be sent to Logfire.\n"
                "Get a token at https://logfire.pydantic.dev and add it to .env."
            ),
        })


async def _run_agent_bg(run_id: str, state: RunState, question: str | None = None) -> None:
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

    q = question or state.current_question
    turn_idx = state.start_turn(q)
    current_run_id.set(run_id)
    _approval_override.set(
        lambda tool, args: _web_request_approval(tool, args, run_id, state)
    )

    # Only run preflight on the first turn
    if turn_idx == 0:
        await _preflight(state)

    try:
        model_config = load_model_config()
        state.push({"type": "status", "message": "Routing…"})
        if settings.router_type == "llm" and settings.openai_api_key:
            try:
                route = await LLMRouter(
                    model_str=model_config["router"]["decision"],
                    openai_api_key=settings.get_key("openai"),
                ).route(q)
            except Exception as e:
                state.push({
                    "type": "warning",
                    "message": f"LLM router unavailable ({e.__class__.__name__}), falling back to rule-based.",
                })
                route = RuleBasedRouter().route(q)
        else:
            route = RuleBasedRouter().route(q)

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

        memory = _get_memory()
        session_id = state.username  # one memory session per user across all their threads
        memory_context = ""
        if memory:
            try:
                memory_context = await memory.get_context(session_id)
            except Exception as e:
                state.push({
                    "type": "warning",
                    "message": f"Memory read failed ({e.__class__.__name__}) — running without prior context.",
                })

        full_question = (
            f"{memory_context}\n\nUser: {q}" if memory_context else q
        )

        state.push({"type": "status", "message": f"Running on {model_str}…"})

        from pydantic_ai.exceptions import UsageLimitExceeded  # noqa: PLC0415

        _agent_run = None
        try:
            async with agent.iter(
                full_question,
                model=model,
                model_settings=_CACHE_SETTINGS if provider == "anthropic" else None,
                usage_limits=_USAGE_LIMITS,
            ) as _agent_run:
                async for _ in _agent_run:
                    pass  # drive the agent; tool calls execute as side effects
        except UsageLimitExceeded:
            # Emit whatever trace we captured before hitting the ceiling
            partial_msgs = _agent_run.all_messages() if _agent_run else []
            for ev in _extract_trace(partial_msgs):
                state.push(ev)
            state.push({
                "type": "error",
                "message": (
                    f"Tool call limit reached ({_USAGE_LIMITS.tool_calls_limit} calls). "
                    "The model kept calling tools without reaching an answer — "
                    "see the trace above for the loop pattern. "
                    "Check that OPA is running and the tools are returning useful data."
                ),
            })
            state.status = "failed"
            state.push({"type": "done"})
            return

        run_result = _agent_run.result
        run_usage = run_result.usage
        cost = _estimate_cost(run_usage, model_str)
        state.cost_usd = cost
        state.total_cost_usd += cost
        out = run_result.output.model_dump()

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
                await memory.add_turn(session_id, "user", q)
                await memory.add_turn(session_id, "assistant", out["summary"])
            except Exception as e:
                state.push({
                    "type": "warning",
                    "message": f"Memory write failed ({e.__class__.__name__}) — turn not saved.",
                })

        # Push reasoning trace (tool calls, results, thinking) before the answer
        for trace_event in _extract_trace(run_result.all_messages()):
            state.push(trace_event)

        state.push({
            "type": "answer",
            "summary": out["summary"],
            "sources": out.get("sources", []),
            "confidence": out.get("confidence", 0),
            "cost_usd": cost,
            "input_tokens": run_usage.input_tokens,
            "output_tokens": run_usage.output_tokens,
            "turn_index": turn_idx,
        })
        state.end_turn(out["summary"])
        # Conversation stays open for follow-up
        state.status = "waiting_follow_up"

    except asyncio.CancelledError:
        state.push({"type": "error", "message": "Task cancelled by user."})
        state.status = "cancelled"
        state.push({"type": "done"})
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
        state.push({"type": "done"})


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request, error: str = "") -> Response:
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": error})


@app.post("/login")
async def login_post(
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    um = _get_user_manager()
    role = await um.authenticate(username, password)
    if role is None:
        return RedirectResponse("/login?error=Invalid+credentials", status_code=303)
    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(
        username=username,
        role=role,
        expires_at=datetime.now(timezone.utc) + SESSION_TTL,
    )
    redir = RedirectResponse("/", status_code=303)
    redir.set_cookie("session", token, httponly=True, samesite="lax")
    return redir


@app.get("/logout")
async def logout(request: Request) -> Response:
    token = request.cookies.get("session", "")
    _sessions.pop(token, None)
    redir = RedirectResponse("/login", status_code=303)
    redir.delete_cookie("session")
    return redir


# ── Task routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request, user: str = Depends(_auth)) -> Response:
    role = _get_role(request)
    recent = list(reversed(list(_runs.values())))[:20]
    cost_today = sum(r.total_cost_usd for r in _runs.values())
    tier_counts: dict[str, int] = {}
    for r in _runs.values():
        if r.tier:
            tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1

    return templates.TemplateResponse(request, "index.html", {
        "user": user,
        "role": role,
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
                "cost_usd": r.total_cost_usd or None,
            }
            for r in recent
        ],
    })


@app.post("/tasks")
async def submit_task(
    request: Request,
    question: str = Form(...),
    user: str = Depends(_require_admin),
) -> Response:
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
    role = _get_role(request)
    completed_turns = [t for t in state.turns if t.get("answer") is not None]
    return templates.TemplateResponse(request, "task.html", {
        "user": user,
        "role": role,
        "run_id": run_id,
        "question": state.turns[0]["question"] if state.turns else state.current_question,
        "status": state.status,
        "completed_turns": completed_turns,
        "total_cost_usd": state.total_cost_usd,
    })


@app.get("/tasks/{run_id}/stream")
async def task_stream(
    run_id: str,
    request: Request,
    last_id: int = 0,
    user: str = Depends(_auth),
) -> Response:
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
            elif state.status in ("closed", "failed", "cancelled"):
                # Drain remaining events then close
                while idx < len(state.events):
                    yield f"id: {idx}\ndata: {json.dumps(state.events[idx])}\n\n"
                    idx += 1
                break
            else:
                # running or waiting_follow_up — keep stream open
                await asyncio.sleep(0.25)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/tasks/{run_id}/message")
async def submit_follow_up(
    run_id: str,
    request: Request,
    question: str = Form(...),
    user: str = Depends(_require_admin),
) -> Response:
    state = _runs.get(run_id)
    if not state or state.username != user:
        raise HTTPException(status_code=404)
    if state.status != "waiting_follow_up":
        return RedirectResponse(f"/tasks/{run_id}", status_code=303)
    q = question.strip()
    if not q:
        return RedirectResponse(f"/tasks/{run_id}", status_code=303)
    state.status = "running"
    state.task = asyncio.create_task(_run_agent_bg(run_id, state, q))
    return RedirectResponse(f"/tasks/{run_id}", status_code=303)


@app.post("/tasks/{run_id}/close")
async def close_conversation(
    run_id: str,
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
    state = _runs.get(run_id)
    if state:
        if state.task and not state.task.done():
            state.task.cancel()
        state.status = "closed"
        state.push({"type": "done"})
    return RedirectResponse(f"/tasks/{run_id}", status_code=303)


@app.post("/tasks/{run_id}/cancel")
async def cancel_task(
    run_id: str,
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
    state = _runs.get(run_id)
    if state:
        if state.task and not state.task.done():
            state.status = "cancelled"
            state.task.cancel()
        elif state.status == "waiting_follow_up":
            state.status = "closed"
            state.push({"type": "done"})
    return RedirectResponse(f"/tasks/{run_id}", status_code=303)


# ── Approval routes ────────────────────────────────────────────────────────────

@app.get("/approvals")
async def approvals_page(request: Request, user: str = Depends(_auth)) -> Response:
    role = _get_role(request)
    items = [
        {k: v for k, v in a.items() if k != "done"}
        for a in _pending_approvals.values()
    ]
    return templates.TemplateResponse(request, "approvals.html", {
        "user": user,
        "role": role,
        "approvals": items,
    })


@app.post("/approvals/{approval_id}/approve")
async def approve_action(
    approval_id: str,
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
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
async def deny_action(
    approval_id: str,
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
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
            pass
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


# ── Audit route ────────────────────────────────────────────────────────────────

@app.get("/audit")
async def audit_page(
    request: Request,
    run_id: str = "",
    actor: str = "",
    tool: str = "",
    user: str = Depends(_auth),
) -> Response:
    role = _get_role(request)
    ok, msg = await verify_chain()
    records, total = await query_records(run_id=run_id, actor=actor, tool=tool)
    return templates.TemplateResponse(request, "audit.html", {
        "user": user,
        "role": role,
        "records": records,
        "total": total,
        "chain_ok": ok,
        "chain_msg": msg,
        "filters": {"run_id": run_id, "actor": actor, "tool": tool},
    })


# ── History route ──────────────────────────────────────────────────────────────

@app.get("/history")
async def history_page(request: Request, user: str = Depends(_auth)) -> Response:
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    role = _get_role(request)
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
                    await cur.execute(
                        "SELECT session_id, summary, up_to_turn, updated_at "
                        "FROM conversation_summaries"
                    )
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
        "role": role,
        "sessions": sessions,
    })


# ── Admin: user management ─────────────────────────────────────────────────────

@app.get("/admin/users")
async def admin_users_page(request: Request, user: str = Depends(_require_admin)) -> Response:
    um = _get_user_manager()
    users = await um.list_users()
    db_available = bool(settings.database_url)
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user,
        "role": "admin",
        "users": users,
        "db_available": db_available,
        "error": request.query_params.get("error", ""),
    })


@app.post("/admin/users")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
    user: str = Depends(_require_admin),
) -> Response:
    um = _get_user_manager()
    try:
        await um.create_user(username.strip(), password, role)
    except Exception as e:
        return RedirectResponse(
            f"/admin/users?error={str(e)[:100]}",
            status_code=303,
        )
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_username}/delete")
async def admin_delete_user(
    target_username: str,
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
    um = _get_user_manager()
    try:
        await um.delete_user(target_username)
    except Exception as e:
        return RedirectResponse(
            f"/admin/users?error={str(e)[:100]}",
            status_code=303,
        )
    return RedirectResponse("/admin/users", status_code=303)


# ── Entry point (P1: Ctrl+C fix) ──────────────────────────────────────────────

def main() -> None:
    """
    Run the web server directly. Avoids the uv-run → uvicorn-reloader subprocess
    chain that swallows SIGINT when using `uv run uvicorn ... --reload`.
    """
    import uvicorn  # noqa: PLC0415
    uvicorn.run("tack_ai.web:app", host="0.0.0.0", port=8000, reload=True)
