"""
Web console — compliance surface for tack-ai.

Routes
------
GET  /                    Dashboard (links to key surfaces)
GET  /chat                PydanticAI native chat UI (agent.to_web(), auth-gated)
GET  /login               Login form
POST /login               Authenticate and set session cookie
GET  /logout              Clear session
GET  /approvals           Pending approvals
POST /approvals/{id}/approve
POST /approvals/{id}/deny
GET  /audit               Audit trail browser with chain-verify status
GET  /admin/users         User management (admin only)
POST /admin/users         Create a user (admin only)
POST /admin/users/{u}/delete  Delete a user (admin only)
GET  /admin/settings      Routing & policy settings
POST /admin/settings      Save settings
POST /admin/settings/reset  Reset to defaults
POST /admin/policy/reload Invalidate cached OPA policy version

Start with:
    uv run tack-ai-web
    # or for manual uvicorn:
    uvicorn tack_ai.web:app --reload
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from tack_ai.audit import append, query_records, verify_chain
from tack_ai.core.config import Settings
from tack_ai.core.models import AuditRecord, PolicyDecision
from tack_ai.observability import configure_logging, configure_tracing, get_logger
from tack_ai.policy import reload_policy_version
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


_db_status_cache: dict[str, Any] = {"ok": True, "checked_at": None}
_DB_CACHE_TTL = 30  # seconds


async def _check_db() -> bool:
    import time  # noqa: PLC0415

    now = time.monotonic()
    if (
        _db_status_cache["checked_at"] is not None
        and now - _db_status_cache["checked_at"] < _DB_CACHE_TTL
    ):
        return _db_status_cache["ok"]
    if not settings.database_url:
        _db_status_cache.update(ok=True, checked_at=now)
        return True
    try:
        import psycopg  # noqa: PLC0415

        async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
            await conn.execute("SELECT 1")
        _db_status_cache.update(ok=True, checked_at=now)
        return True
    except Exception:
        _db_status_cache.update(ok=False, checked_at=now)
        return False


@app.middleware("http")
async def _inject_db_status(request: Request, call_next: Any) -> Response:
    request.state.db_ok = await _check_db()
    return await call_next(request)


SESSION_TTL = timedelta(hours=24)


@app.middleware("http")
async def _chat_auth_middleware(request: Request, call_next: Any) -> Response:
    """Gate the PydanticAI chat UI behind the same session cookie as the rest of the app.

    The chat HTML is served by the catch-all sub-app mount at '/'.  Its JS bundle
    uses absolute paths /api/chat and /api/configure — we block those here so the
    API is inaccessible without a valid session, matching the rest of the console.
    """
    path = request.url.path
    if path in ("/api/chat", "/api/configure") or path.startswith("/chat"):
        token = request.cookies.get("session", "")
        sess = _sessions.get(token)
        if not sess or sess.expires_at < datetime.now(timezone.utc):
            if path in ("/api/chat", "/api/configure"):
                return Response("Unauthorized", status_code=401)
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


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


_pending_approvals: dict[str, dict[str, Any]] = {}

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


# ── Dashboard ──────────────────────────────────────────────────────────────────


@app.get("/")
async def index(request: Request, user: str = Depends(_auth)) -> Response:
    role = _get_role(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "role": role,
            "pending_approvals": len(_pending_approvals),
        },
    )


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
    fga = get_fga_client()
    if fga is not None:
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
    from tack_ai.web.oidc import OIDC_ADMIN_EMAILS as _oidc_admin_emails  # noqa: PLC0415
    from tack_ai.web.oidc import OIDC_CLIENT_ID as _oidc_client_id  # noqa: PLC0415
    from tack_ai.web.oidc import OIDC_DEFAULT_ROLE as _oidc_default_role  # noqa: PLC0415
    from tack_ai.web.oidc import OIDC_DISCOVERY_URL as _oidc_discovery_url  # noqa: PLC0415
    from tack_ai.web.oidc import OIDC_ENABLED as _oidc_enabled  # noqa: PLC0415
    from tack_ai.web.oidc import OIDC_PROVIDER as _oidc_provider  # noqa: PLC0415
    from tack_ai.web.oidc import OIDC_REDIRECT_BASE as _oidc_redirect_base  # noqa: PLC0415

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
            "oidc_enabled": _oidc_enabled,
            "oidc_provider": _oidc_provider,
            "oidc_redirect_base": _oidc_redirect_base,
            "oidc_client_id_set": bool(_oidc_client_id),
            "oidc_discovery_url": _oidc_discovery_url,
            "oidc_admin_emails": sorted(_oidc_admin_emails),
            "oidc_default_role": _oidc_default_role,
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


# ── PydanticAI chat UI ────────────────────────────────────────────────────────
# Mounted at "/" as a catch-all AFTER all FastAPI routes are registered so
# FastAPI routes take precedence (they're checked first in the route list).
# The JS bundle fetched from CDN uses absolute paths /api/chat and /api/configure
# which resolve against the FastAPI root — the middleware above gates those.
# Auth-gated: sessions required before /api/chat, /api/configure, and /chat/*.

try:
    from tack_ai.agent import agent as _tack_agent  # noqa: PLC0415
    from tack_ai.core.router import build_model, load_model_config  # noqa: PLC0415

    _mc = load_model_config()
    # to_web(models=...) calls pydantic_ai.infer_model() on each entry which reads
    # os.environ directly, bypassing our Settings object loaded from .env.
    # Pass already-constructed Model instances via build_model() so API keys come
    # from Settings. Deduplicate by string to avoid showing the same model twice.
    _seen_model_strs: set[str] = set()
    _tier_models_built: dict[str, Any] = {}
    for _tier, _label in [
        ("simple", "Simple"),
        ("general", "General"),
        ("deep_reasoning", "Deep reasoning"),
    ]:
        _model_str = _mc["tiers"][_tier]
        _provider = _model_str.split(":")[0]
        if getattr(settings, f"{_provider}_api_key", None) and _model_str not in _seen_model_strs:
            _tier_models_built[f"{_label}  ({_model_str})"] = build_model(_model_str, settings)
            _seen_model_strs.add(_model_str)
    app.mount("/", _tack_agent.to_web(models=_tier_models_built or None))
except Exception as _e:
    log.warning("Could not mount chat UI — check API key config", error=str(_e))


# ── Entry point (P1: Ctrl+C fix) ──────────────────────────────────────────────


def main() -> None:
    """
    Run the web server directly. Avoids the uv-run → uvicorn-reloader subprocess
    chain that swallows SIGINT when using `uv run uvicorn ... --reload`.
    """
    import uvicorn  # noqa: PLC0415

    uvicorn.run("tack_ai.web:app", host="0.0.0.0", port=8000, reload=True)  # nosec B104
