"""
Web console — ops and compliance surface for tack-ai.

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
import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from tack_ai.audit import append, query_records, verify_chain
from tack_ai.core.config import Settings
from tack_ai.core.models import AuditRecord, PolicyDecision
from tack_ai.observability import configure_logging, configure_tracing, get_logger, tasks_total
from tack_ai.policy import _approval_override, reload_policy_version
from tack_ai.web.auth import UserManager, ensure_fga_client, get_fga_client

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
_LOG_JSON = os.environ.get("LOG_JSON", "true").lower() not in ("0", "false", "no")
configure_logging(json=_LOG_JSON, level=_LOG_LEVEL)
log = get_logger("tack_ai.web")

settings = Settings()

TEMPLATES_DIR = Path(__file__).parents[3] / "templates"


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncGenerator[None, None]:
    # Tracing — OTLP exporter wired up before any requests arrive.
    configure_tracing()
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(fastapi_app)
    HTTPXClientInstrumentor().instrument()
    PsycopgInstrumentor().instrument()

    await ensure_fga_client()
    yield

    FastAPIInstrumentor.uninstrument_app(fastapi_app)
    HTTPXClientInstrumentor().uninstrument()


app = FastAPI(title="Tack-AI Console", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static/img", StaticFiles(directory=str(TEMPLATES_DIR / "img")), name="static_img")
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
    return Markup(s)  # nosec B704 — s is json.dumps output with HTML chars escaped above


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

# ── Runtime settings ─────────────────────────────────────────────────────────
# Admins can override routing config at runtime without restarting. Changes
# are persisted to config/runtime_settings.json and survive process restarts.

_RUNTIME_SETTINGS_PATH = Path(__file__).parents[3] / "config" / "runtime_settings.json"


def _load_runtime_settings() -> dict[str, Any]:
    if _RUNTIME_SETTINGS_PATH.exists():
        try:
            return json.loads(_RUNTIME_SETTINGS_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_runtime_settings(data: dict[str, Any]) -> None:
    _RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RUNTIME_SETTINGS_PATH.write_text(json.dumps(data, indent=2))


_runtime: dict[str, Any] = _load_runtime_settings()


def _effective_router_type() -> str:
    return str(_runtime.get("router_type", settings.router_type))


def _effective_budget() -> float:
    return float(_runtime.get("task_budget_usd", settings.task_budget_usd))


def _effective_model_config() -> dict[str, Any]:
    from tack_ai.core.router import load_model_config  # noqa: PLC0415

    base = load_model_config()
    for tier, model in _runtime.get("model_overrides", {}).items():
        if tier in base.get("tiers", {}):
            base["tiers"][tier] = model
    return base


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
                events.append(
                    {
                        "type": "tool_call",
                        "tool_name": getattr(part, "tool_name", "?"),
                        "args": str(getattr(part, "args", ""))[:300],
                    }
                )
            elif ptype == "ToolReturnPart":
                events.append(
                    {
                        "type": "tool_result",
                        "tool_name": getattr(part, "tool_name", "?"),
                        "content": str(getattr(part, "content", ""))[:300],
                    }
                )
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
    state.push(
        {
            "type": "approval_required",
            "approval_id": approval_id,
            "tool_name": tool_name,
            "actor": state.username,
            "args": {k: str(v)[:300] for k, v in args.items()},
        }
    )
    await done.wait()
    info = _pending_approvals.pop(approval_id, {})
    return bool(info.get("approved"))


async def _preflight(state: RunState) -> None:
    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            await c.get("http://localhost:8181/health")
    except Exception:
        state.push(
            {
                "type": "warning",
                "message": (
                    "OPA is not reachable — all tool calls will be denied (fail-closed).\n"
                    "Start it with:  opa run --server --addr :8181 policies/"
                ),
            }
        )

    if not settings.database_url:
        state.push(
            {
                "type": "warning",
                "message": (
                    "DATABASE_URL is not set — document search and conversation memory "
                    "are disabled."
                ),
            }
        )

    if not settings.logfire_token:
        state.push(
            {
                "type": "warning",
                "message": (
                    "LOGFIRE_TOKEN is not set — traces will not be sent to Logfire.\n"
                    "Get a token at https://logfire.pydantic.dev and add it to .env."
                ),
            }
        )


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
    from tack_ai.core.router import LLMRouter, RuleBasedRouter  # noqa: PLC0415

    q = question or state.current_question
    turn_idx = state.start_turn(q)
    current_run_id.set(run_id)
    _approval_override.set(lambda tool, args: _web_request_approval(tool, args, run_id, state))

    # Only run preflight on the first turn
    if turn_idx == 0:
        await _preflight(state)

    try:
        model_config = _effective_model_config()
        state.push({"type": "status", "message": "Routing…"})
        if _effective_router_type() == "llm" and settings.openai_api_key:
            try:
                route = await LLMRouter(
                    model_str=model_config["router"]["decision"],
                    openai_api_key=settings.get_key("openai"),
                ).route(q)
            except Exception as e:
                state.push(
                    {
                        "type": "warning",
                        "message": f"LLM router unavailable ({e.__class__.__name__}), falling back to rule-based.",
                    }
                )
                route = RuleBasedRouter().route(q)
        else:
            route = RuleBasedRouter().route(q)

        state.tier = route.tier.value
        state.push(
            {
                "type": "routing",
                "tier": route.tier.value,
                "effort": route.reasoning_effort.value,
                "reason": route.reason,
            }
        )
        model, model_str = _build_model_with_fallback(route, model_config)
        provider = model_str.split(":")[0]

        await append(
            AuditRecord(
                run_id=run_id,
                actor=state.username,
                event_type="routing",
                routing_tier=route.tier.value,
                routing_reason=route.reason,
                model=model_str,
                provider=provider,
            )
        )

        memory = _get_memory()
        session_id = state.username  # one memory session per user across all their threads
        memory_context = ""
        if memory:
            try:
                memory_context = await memory.get_context(session_id)
            except Exception as e:
                state.push(
                    {
                        "type": "warning",
                        "message": f"Memory read failed ({e.__class__.__name__}) — running without prior context.",
                    }
                )

        full_question = f"{memory_context}\n\nUser: {q}" if memory_context else q

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
            state.push(
                {
                    "type": "error",
                    "message": (
                        f"Tool call limit reached ({_USAGE_LIMITS.tool_calls_limit} calls). "
                        "The model kept calling tools without reaching an answer — "
                        "see the trace above for the loop pattern. "
                        "Check that OPA is running and the tools are returning useful data."
                    ),
                }
            )
            state.status = "failed"
            state.push({"type": "done"})
            return

        assert _agent_run is not None
        run_result = _agent_run.result
        assert run_result is not None
        run_usage = run_result.usage
        cost = _estimate_cost(run_usage, model_str)
        state.cost_usd = cost
        state.total_cost_usd += cost
        out = run_result.output.model_dump()

        await append(
            AuditRecord(
                run_id=run_id,
                actor=state.username,
                event_type="outcome",
                model=model_str,
                provider=provider,
                outcome=out["summary"][:200],
                cost_usd=cost,
            )
        )

        if memory:
            try:
                await memory.add_turn(session_id, "user", q)
                await memory.add_turn(session_id, "assistant", out["summary"])
            except Exception as e:
                state.push(
                    {
                        "type": "warning",
                        "message": f"Memory write failed ({e.__class__.__name__}) — turn not saved.",
                    }
                )

        # Push reasoning trace (tool calls, results, thinking) before the answer
        for trace_event in _extract_trace(run_result.all_messages()):
            state.push(trace_event)

        state.push(
            {
                "type": "answer",
                "summary": out["summary"],
                "sources": out.get("sources", []),
                "confidence": out.get("confidence", 0),
                "cost_usd": cost,
                "input_tokens": run_usage.input_tokens,
                "output_tokens": run_usage.output_tokens,
                "turn_index": turn_idx,
            }
        )
        state.end_turn(out["summary"])
        tasks_total.labels(status="completed").inc()
        # Conversation stays open for follow-up
        state.status = "waiting_follow_up"

    except asyncio.CancelledError:
        state.push({"type": "error", "message": "Task cancelled by user."})
        state.status = "cancelled"
        tasks_total.labels(status="cancelled").inc()
        state.push({"type": "done"})
    except Exception as exc:
        msg = str(exc)
        if "All connection attempts failed" in msg or "Client failed to connect" in msg:
            from tack_ai.core.config import Settings as _S  # noqa: PLC0415

            cfg = _S()
            if cfg.mcp_gateway_url:
                msg = (
                    f"MCP gateway at {cfg.mcp_gateway_url!r} is not reachable.\n"
                    "Start it with:  uv run python -m tack_ai.mcp_gateway\n"
                    "Or unset MCP_GATEWAY_URL in .env to run without MCP tools."
                )
        state.push({"type": "error", "message": msg})
        state.status = "failed"
        tasks_total.labels(status="failed").inc()
        state.push({"type": "done"})


# ── Auth routes ────────────────────────────────────────────────────────────────


@app.get("/login")
async def login_page(request: Request, error: str = "") -> Response:
    from tack_ai.web.oidc import OIDC_ENABLED, OIDC_PROVIDER  # noqa: PLC0415

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "user": None,
            "error": error,
            "oidc_enabled": OIDC_ENABLED,
            "oidc_provider": OIDC_PROVIDER,
        },
    )


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


@app.get("/auth/login")
async def oidc_login(request: Request) -> Response:
    """Redirect to the OIDC provider's authorization endpoint."""
    from tack_ai.web.oidc import OIDC_ENABLED, get_authorization_url  # noqa: PLC0415

    if not OIDC_ENABLED:
        return RedirectResponse("/login?error=OIDC+not+configured", status_code=303)
    state = secrets.token_urlsafe(16)
    # Store state in a short-lived cookie to prevent CSRF
    authorization_url = await get_authorization_url(state)
    redir = RedirectResponse(authorization_url, status_code=302)
    redir.set_cookie("oidc_state", state, httponly=True, samesite="lax", max_age=300)
    return redir


@app.get("/auth/callback")
async def oidc_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> Response:
    """Handle the OIDC provider callback and establish a session."""
    from tack_ai.web.oidc import OIDC_ENABLED, resolve_identity  # noqa: PLC0415

    if not OIDC_ENABLED:
        return RedirectResponse("/login?error=OIDC+not+configured", status_code=303)

    if error:
        return RedirectResponse(f"/login?error=OIDC+error:+{error[:80]}", status_code=303)

    stored_state = request.cookies.get("oidc_state", "")
    if not state or state != stored_state:
        return RedirectResponse("/login?error=State+mismatch+—+possible+CSRF", status_code=303)

    if not code:
        return RedirectResponse("/login?error=No+authorization+code", status_code=303)

    try:
        username, role = await resolve_identity(code)
    except Exception as exc:
        log.warning("oidc_callback_failed", error=str(exc))
        return RedirectResponse(
            f"/login?error=OIDC+authentication+failed:+{str(exc)[:80]}",
            status_code=303,
        )

    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(
        username=username,
        role=role,
        expires_at=datetime.now(timezone.utc) + SESSION_TTL,
    )
    log.info("oidc_login", username=username, role=role)
    redir = RedirectResponse("/", status_code=303)
    redir.set_cookie("session", token, httponly=True, samesite="lax")
    redir.delete_cookie("oidc_state")
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

    return templates.TemplateResponse(
        request,
        "index.html",
        {
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
        },
    )


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
    return templates.TemplateResponse(
        request,
        "task.html",
        {
            "user": user,
            "role": role,
            "run_id": run_id,
            "question": state.turns[0]["question"] if state.turns else state.current_question,
            "status": state.status,
            "completed_turns": completed_turns,
            "total_cost_usd": state.total_cost_usd,
        },
    )


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
    items = [{k: v for k, v in a.items() if k != "done"} for a in _pending_approvals.values()]
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "user": user,
            "role": role,
            "approvals": items,
        },
    )


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
    await append(
        AuditRecord(
            run_id=info["run_id"],
            actor=user,
            event_type="approval",
            tool_name=info["tool_name"],
            tool_args=info["args"],
            policy_decision=PolicyDecision.require_approval,
            approver=user,
            approved_at=datetime.now(timezone.utc),
            outcome="approved",
        )
    )
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
    await append(
        AuditRecord(
            run_id=info["run_id"],
            actor=user,
            event_type="approval",
            tool_name=info["tool_name"],
            tool_args=info["args"],
            policy_decision=PolicyDecision.require_approval,
            approver=user,
            approved_at=datetime.now(timezone.utc),
            outcome="denied",
        )
    )
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
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "user": user,
            "role": role,
            "records": records,
            "total": total,
            "chain_ok": ok,
            "chain_msg": msg,
            "filters": {"run_id": run_id, "actor": actor, "tool": tool},
        },
    )


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

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "role": role,
            "sessions": sessions,
        },
    )


# ── Admin: user management ─────────────────────────────────────────────────────


@app.get("/admin/users")
async def admin_users_page(request: Request, user: str = Depends(_require_admin)) -> Response:
    um = _get_user_manager()
    users = await um.list_users()
    db_available = bool(settings.database_url)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "user": user,
            "role": "admin",
            "users": users,
            "db_available": db_available,
            "error": request.query_params.get("error", ""),
        },
    )


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


# ── Admin settings ────────────────────────────────────────────────────────────


@app.get("/admin/settings")
async def admin_settings(request: Request, user: str = Depends(_require_admin)) -> Response:
    from tack_ai.core.router import load_model_config  # noqa: PLC0415
    from tack_ai.policy.engine import get_policy_version  # noqa: PLC0415

    base_config = load_model_config()
    effective = _effective_model_config()
    policy_version = await get_policy_version()
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "user": user,
            "role": "admin",
            "router_type": _effective_router_type(),
            "task_budget_usd": _effective_budget(),
            "tiers": effective.get("tiers", {}),
            "base_tiers": base_config.get("tiers", {}),
            "overrides": _runtime.get("model_overrides", {}),
            "router_decision": effective.get("router", {}).get("decision", ""),
            "policy_engine": settings.policy_engine,
            "opa_url": settings.opa_url,
            "policy_version": policy_version,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/admin/settings")
async def admin_settings_save(
    request: Request,
    router_type: str = Form(...),
    task_budget_usd: float = Form(...),
    simple_model: str = Form(...),
    general_model: str = Form(...),
    deep_reasoning_model: str = Form(...),
    user: str = Depends(_require_admin),
) -> Response:
    if router_type not in ("llm", "rule_based"):
        return RedirectResponse("/admin/settings?error=Invalid+router_type", status_code=303)
    if task_budget_usd <= 0:
        return RedirectResponse("/admin/settings?error=Budget+must+be+positive", status_code=303)

    from tack_ai.core.router import load_model_config  # noqa: PLC0415

    base = load_model_config()

    overrides: dict[str, str] = {}
    for tier, new_model in [
        ("simple", simple_model.strip()),
        ("general", general_model.strip()),
        ("deep_reasoning", deep_reasoning_model.strip()),
    ]:
        if new_model and new_model != base.get("tiers", {}).get(tier, ""):
            overrides[tier] = new_model

    _runtime["router_type"] = router_type
    _runtime["task_budget_usd"] = task_budget_usd
    _runtime["model_overrides"] = overrides
    _save_runtime_settings(_runtime)
    return RedirectResponse("/admin/settings?success=1", status_code=303)


@app.post("/admin/settings/reset")
async def admin_settings_reset(
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
    _runtime.clear()
    if _RUNTIME_SETTINGS_PATH.exists():
        _RUNTIME_SETTINGS_PATH.unlink()
    return RedirectResponse("/admin/settings?success=1", status_code=303)


@app.post("/admin/policy/reload")
async def admin_policy_reload(
    request: Request,
    user: str = Depends(_require_admin),
) -> Response:
    """Invalidate the cached policy version so the next request re-fetches from OPA."""
    reload_policy_version()
    return RedirectResponse("/admin/settings?success=1", status_code=303)


# ── Metrics & observability ───────────────────────────────────────────────────


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(user: str = Depends(_auth)) -> Response:
    """Prometheus text-format metrics. Auth-gated (any authenticated user)."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: PLC0415

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ── Audit export ──────────────────────────────────────────────────────────────


@app.get("/audit/export")
async def audit_export(
    request: Request,
    fmt: str = "json",
    run_id: str = "",
    actor: str = "",
    tool: str = "",
    limit: int = 1000,
    user: str = Depends(_auth),
) -> Response:
    """Download audit records as JSON or CSV."""
    from tack_ai.audit import query_records  # noqa: PLC0415

    records, _total = await query_records(run_id=run_id, actor=actor, tool=tool, limit=limit)

    if fmt == "csv":
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415

        buf = io.StringIO()
        if records:
            writer = csv.DictWriter(buf, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit.csv"},
        )

    return Response(
        content=json.dumps(records, default=str, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=audit.json"},
    )


# ── API docs (auth-gated) ─────────────────────────────────────────────────────

_BRAND = "#4DCF1E"
_BRAND_HOVER = "#60E82A"

_SWAGGER_DARK_CSS = f"""
body {{ margin: 0; background: #0d0d0d; }}
.swagger-ui .topbar {{ display: none; }}
.swagger-ui {{ color: #e5e7eb; background: #0d0d0d; font-family: monospace; }}
.swagger-ui .wrapper {{ background: #0d0d0d; }}
.swagger-ui .info .title {{ color: {_BRAND}; font-family: monospace; }}
.swagger-ui .info p, .swagger-ui .info li, .swagger-ui .info a {{ color: #9ca3af; }}
.swagger-ui .scheme-container {{ background: #111; box-shadow: none; border-bottom: 1px solid #222; }}
.swagger-ui .opblock {{ background: #111; border: 1px solid #222; box-shadow: none; }}
.swagger-ui .opblock .opblock-summary {{ border-bottom: 1px solid #222; }}
.swagger-ui .opblock.opblock-get {{ border-color: rgba(77,207,30,.25); background: rgba(77,207,30,.04); }}
.swagger-ui .opblock.opblock-get .opblock-summary-method {{ background: {_BRAND}; color: #000; }}
.swagger-ui .opblock.opblock-post {{ border-color: rgba(59,130,246,.25); background: rgba(59,130,246,.04); }}
.swagger-ui .opblock.opblock-post .opblock-summary-method {{ background: #3b82f6; }}
.swagger-ui .opblock.opblock-delete {{ border-color: rgba(239,68,68,.25); background: rgba(239,68,68,.04); }}
.swagger-ui .opblock.opblock-delete .opblock-summary-method {{ background: #ef4444; }}
.swagger-ui .opblock.opblock-put {{ border-color: rgba(245,158,11,.25); background: rgba(245,158,11,.04); }}
.swagger-ui .opblock.opblock-put .opblock-summary-method {{ background: #f59e0b; }}
.swagger-ui .opblock.opblock-patch {{ border-color: rgba(99,102,241,.25); background: rgba(99,102,241,.04); }}
.swagger-ui .opblock.opblock-patch .opblock-summary-method {{ background: #6366f1; }}
.swagger-ui .opblock-summary-description, .swagger-ui .opblock-summary-path,
.swagger-ui .opblock-summary-path__deprecated {{ color: #9ca3af; }}
.swagger-ui .opblock-description-wrapper p, .swagger-ui .opblock-external-docs-wrapper p {{ color: #9ca3af; }}
.swagger-ui table thead tr td, .swagger-ui table thead tr th {{ border-bottom: 1px solid #333; color: #9ca3af; }}
.swagger-ui .parameter__name {{ color: #e5e7eb; }}
.swagger-ui .parameter__type {{ color: {_BRAND}; }}
.swagger-ui .parameter__in {{ color: #6b7280; font-style: italic; }}
.swagger-ui .parameter__deprecated {{ color: #6b7280; }}
.swagger-ui input[type=text], .swagger-ui input[type=password], .swagger-ui input[type=search],
.swagger-ui input[type=email], .swagger-ui textarea, .swagger-ui select {{
  background: #1a1a1a !important; border: 1px solid #333 !important; color: #e5e7eb !important;
}}
.swagger-ui .btn {{ background: #1a1a1a; color: #e5e7eb; border: 1px solid #333; }}
.swagger-ui .btn:hover {{ background: #222; border-color: #444; }}
.swagger-ui .btn.execute {{ background: {_BRAND}; color: #000; border-color: {_BRAND}; font-weight: 700; }}
.swagger-ui .btn.execute:hover {{ background: {_BRAND_HOVER}; border-color: {_BRAND_HOVER}; }}
.swagger-ui .btn.authorize {{ background: transparent; color: {_BRAND}; border-color: {_BRAND}; }}
.swagger-ui .btn.authorize svg {{ fill: {_BRAND}; }}
.swagger-ui .btn.cancel {{ background: transparent; color: #ef4444; border-color: #ef4444; }}
.swagger-ui .auth-container {{ background: #111; border-color: #222; }}
.swagger-ui .dialog-ux .modal-ux {{ background: #111; border: 1px solid #222; }}
.swagger-ui .dialog-ux .modal-ux-header {{ border-bottom: 1px solid #222; }}
.swagger-ui .dialog-ux .modal-ux-header h3 {{ color: {_BRAND}; }}
.swagger-ui section.models {{ border: 1px solid #222; background: #0d0d0d; }}
.swagger-ui section.models h4 {{ color: #9ca3af; border-bottom: 1px solid #222; background: #0d0d0d; }}
.swagger-ui section.models h4 span {{ color: #9ca3af; }}
.swagger-ui section.models h4 svg {{ fill: #6b7280; }}
.swagger-ui section.models .model-container {{ background: #161616; border-top: 1px solid #222; margin: 0; padding: 8px 16px; }}
.swagger-ui section.models .model-container:hover {{ background: #1a1a1a; }}
.swagger-ui .model-box {{ background: transparent !important; box-shadow: none !important; }}
.swagger-ui .model-title {{ color: #c9d1d9; font-weight: 500; font-size: 13px; }}
.swagger-ui .model-title__text {{ color: #c9d1d9; }}
.swagger-ui .model {{ color: #8b949e; }}
.swagger-ui .model span {{ color: #8b949e; }}
.swagger-ui .model .property {{ color: #8b949e; }}
.swagger-ui .model .property.primitive {{ color: #8b949e; }}
.swagger-ui span[class*="model-title"] {{ background: transparent !important; }}
.swagger-ui .model span.model {{ background: transparent; }}
.swagger-ui .prop-type {{ color: #8b949e; }}
.swagger-ui .prop-format {{ color: #6b7280; }}
/* "object" / type badge text — neutralise the default blue */
.swagger-ui .model > span, .swagger-ui .model > .prop-type,
.swagger-ui span.model-title + span {{ color: #8b949e !important; }}
/* "Expand all" button */
.swagger-ui .model-box .model-box--body {{ color: #6b7280; border-color: #333; background: transparent; }}
.swagger-ui button.model-box--body:hover {{ color: #9ca3af; border-color: #4b5563; }}
.swagger-ui .highlight-code, .swagger-ui .microlight {{ background: #0d0d0d !important; }}
.swagger-ui .opblock-body pre.microlight {{ background: #0d0d0d !important; color: #e5e7eb; }}
/* Parameters / Responses section headers */
.swagger-ui .opblock .opblock-section-header {{
  background: #1a1a1a !important; box-shadow: none !important;
  border-top: 1px solid #333;
}}
.swagger-ui .opblock .opblock-section-header h4 {{ color: #9ca3af !important; font-weight: 600; }}
.swagger-ui .opblock .opblock-section-header label {{ color: #9ca3af !important; }}
.swagger-ui .opblock .opblock-section-header .btn {{ background: transparent !important; color: #6b7280 !important; border-color: #444 !important; }}
/* opblock body + param rows */
.swagger-ui .opblock-body {{ background: #0d0d0d; }}
.swagger-ui .opblock-body tr, .swagger-ui .opblock-body td {{ background: transparent !important; }}
.swagger-ui .parameters-container, .swagger-ui .parameters {{ background: transparent; }}
.swagger-ui tr.parameters {{ background: transparent; }}
.swagger-ui .no-margin p {{ color: #9ca3af; }}
.swagger-ui .parameters-col_description p {{ color: #9ca3af; }}
/* Response area */
.swagger-ui .response-col_status {{ color: #e5e7eb; }}
.swagger-ui .response-col_description {{ color: #9ca3af; }}
.swagger-ui .responses-inner {{ background: #0d0d0d; border: 1px solid #222; }}
.swagger-ui .response {{ background: #111; border-color: #222; }}
.swagger-ui .response-content-type {{ background: transparent; color: #9ca3af; }}
/* Nested model / inner object — remove stray light backgrounds */
.swagger-ui .inner-object {{ background: transparent !important; }}
.swagger-ui .model-box-control {{ background: transparent !important; }}
.swagger-ui span.model span {{ color: #8b949e !important; background: transparent !important; }}
.swagger-ui .model .inner-object .model-title {{ color: #8b949e !important; background: transparent !important; }}
.swagger-ui .model table tr td {{ background: transparent !important; color: #8b949e; }}
.swagger-ui .model table {{ background: transparent; }}
/* "array<object>", compound type text */
.swagger-ui .model > .prop-type, .swagger-ui .model span[class*="prop-type"] {{ color: #8b949e !important; }}
.swagger-ui .tab li {{ color: #9ca3af; }}
.swagger-ui .tab li.tabitem.active {{ color: {_BRAND}; }}
.swagger-ui .servers > label {{ color: #9ca3af; }}
.swagger-ui .servers > label select {{ color: #e5e7eb; background: #1a1a1a; border: 1px solid #333; }}
.swagger-ui svg.arrow {{ fill: #9ca3af; }}
.swagger-ui .url {{ color: {_BRAND}; }}
"""


@app.get("/docs", include_in_schema=False)
async def swagger_ui(user: str = Depends(_auth)) -> Response:
    from fastapi.responses import HTMLResponse  # noqa: PLC0415

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>Tack-AI API Docs</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script>
    // Apply theme class before any CSS loads to prevent flash
    var _theme = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', _theme);
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <!-- Dark overrides — disabled at parse time, enabled by JS when theme=dark -->
  <style id="dark-style" disabled>{_SWAGGER_DARK_CSS}</style>
  <style>
    html[data-theme="light"] body {{ background: #fff; }}
    #docs-nav {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 20px; font-family: monospace; font-size: 13px;
      border-bottom: 1px solid var(--nav-border, #1e1e1e);
      background: var(--nav-bg, #000); color: var(--nav-tx, #9ca3af);
    }}
    html[data-theme="light"] #docs-nav {{
      --nav-bg: #fff; --nav-border: #e2e8f0; --nav-tx: #475569;
    }}
    #docs-nav a {{ color: inherit; text-decoration: none; }}
    #docs-nav a:hover, #docs-toggle:hover {{ color: var(--nav-brand, #4DCF1E); }}
    html[data-theme="light"] #docs-nav a:hover,
    html[data-theme="light"] #docs-toggle:hover {{ --nav-brand: #2da312; color: #2da312; }}
    #docs-toggle {{ background: none; border: none; cursor: pointer; font-size: 14px;
                    color: inherit; font-family: monospace; padding: 0 4px; }}
    #docs-brand {{ color: var(--nav-brand, #4DCF1E); font-weight: bold; }}
    html[data-theme="light"] #docs-brand {{ color: #2da312; }}
  </style>
</head>
<body>
  <div id="docs-nav">
    <div style="display:flex;align-items:center;gap:20px">
      <a href="/">← back</a>
      <span id="docs-brand">tack-ai</span>
      <span>API Docs</span>
    </div>
    <button id="docs-toggle" onclick="toggleDocsTheme()" title="Toggle light / dark"></button>
  </div>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    var isDark = (localStorage.getItem('theme') || 'dark') === 'dark';

    function setDocsTheme(dark) {{
      isDark = dark;
      document.getElementById('dark-style').disabled = !dark;
      document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
      document.getElementById('docs-toggle').textContent = dark ? '☀' : '☾';
      if (dark) darkify();
    }}

    function toggleDocsTheme() {{
      var next = !isDark;
      localStorage.setItem('theme', next ? 'dark' : 'light');
      // Simplest reliable approach: reload so Swagger re-renders clean
      location.reload();
    }}

    // MutationObserver to re-apply dark patches after Swagger's JS renders
    function darkify(root) {{
      if (!isDark) return;
      root = root || document;
      root.querySelectorAll('.opblock-section-header').forEach(function(el) {{
        el.style.background = '#1a1a1a';
        el.style.boxShadow  = 'none';
        el.style.borderTop  = '1px solid #333';
      }});
      root.querySelectorAll('.opblock-section-header h4, .opblock-section-header label').forEach(function(el) {{
        el.style.color = '#9ca3af';
      }});
      root.querySelectorAll('.model-box, .inner-object').forEach(function(el) {{
        el.style.background = 'transparent';
        el.style.boxShadow  = 'none';
      }});
      root.querySelectorAll('.model span, .prop-type, .inner-object .model-title').forEach(function(el) {{
        var c = window.getComputedStyle(el).color;
        if (c === 'rgb(59, 65, 81)' || el.classList.contains('prop-type')) {{
          el.style.color = '#8b949e';
        }}
      }});
      root.querySelectorAll('tr.parameters td, .parameters-container').forEach(function(el) {{
        el.style.background = 'transparent';
      }});
    }}

    var _observer = new MutationObserver(function(mutations) {{
      if (!isDark) return;
      mutations.forEach(function(m) {{
        m.addedNodes.forEach(function(n) {{ if (n.nodeType === 1) darkify(n); }});
      }});
      darkify();
    }});

    window.onload = function() {{
      setDocsTheme(isDark);
      SwaggerUIBundle({{
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "BaseLayout",
        deepLinking: true,
        onComplete: function() {{
          darkify();
          _observer.observe(document.getElementById('swagger-ui'), {{
            childList: true, subtree: true
          }});
        }},
      }});
    }};
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/redoc", include_in_schema=False)
async def redoc_ui(user: str = Depends(_auth)) -> Response:
    from fastapi.responses import HTMLResponse  # noqa: PLC0415

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>Tack-AI API Docs</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>body {{ margin: 0; background: #0d0d0d; }}</style>
</head>
<body>
  <div id="redoc-container"></div>
  <script src="https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js"></script>
  <script>
    Redoc.init("/openapi.json", {{
      theme: {{
        colors: {{ primary: {{ main: "{_BRAND}" }} }},
        typography: {{ fontFamily: "monospace", fontSize: "13px" }},
        sidebar: {{ backgroundColor: "#111", textColor: "#9ca3af" }},
        rightPanel: {{ backgroundColor: "#0d0d0d", textColor: "#9ca3af" }},
        logo: {{ gutter: "12px" }},
      }},
      hideDownloadButton: false,
      nativeScrollbars: false,
    }}, document.getElementById("redoc-container"));
  </script>
</body>
</html>"""
    return HTMLResponse(html)


# ── Entry point (P1: Ctrl+C fix) ──────────────────────────────────────────────


def main() -> None:
    """
    Run the web server directly. Avoids the uv-run → uvicorn-reloader subprocess
    chain that swallows SIGINT when using `uv run uvicorn ... --reload`.
    """
    import uvicorn  # noqa: PLC0415

    uvicorn.run("tack_ai.web:app", host="0.0.0.0", port=8000, reload=True)  # nosec B104
