# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A policy-governed coding agent with a tamper-evident audit trail. Anthropic and OpenAI are first-class equal providers. Every tool call is OPA/Cedar-authorized, every routing decision is auditable, and cost is tracked per run. Designed for compliance-sensitive environments. Roadmap lives at `docs/roadmap.md`.

## Commands

```bash
# Start OPA policy server (required before running the agent)
opa run --server --addr :8181 policies/

# Run the agent CLI (in a separate terminal)
uv run python -m tack_ai.agent

# Start the web console
uv run tack-ai-web          # preferred — proper Ctrl+C handling
# then open http://localhost:8000  (default credentials: admin / changeme)
# set WEB_USERNAME and WEB_PASSWORD in .env to change them

# Development / CI
uv sync --all-groups         # install runtime + dev deps
uv run pytest tests/ -v --ignore=tests/e2e   # unit tests (no external services needed)
uv run ruff check src/       # lint
uv run mypy src/tack_ai/     # type-check
uv run bandit -r src/tack_ai/ -ll  # security scan
```

All Python commands use `uv run` — no need to activate the venv manually.

**Note on Ctrl+C:** Use `uv run tack-ai-web` instead of `uv run uvicorn ... --reload`.
The `uv run tack-ai-web` entry point calls uvicorn programmatically, which delivers
SIGINT correctly. The `--reload` flag with `uv run uvicorn` wraps uvicorn in an extra
subprocess layer that can swallow Ctrl+C.

## Architecture decisions

**Framework:** Pydantic AI throughout. `pydantic_graph` for the coding workflow (Plan → Edit → Test → Evaluate). `langgraph_approval.py` is a retained stub — not used in the main path.

**Policy engine:** OPA (primary). The policy layer governs three things: tool authorization, routing decisions (which model/tier), and budget enforcement. Cedar covers the tool-authorization subset — it is authorization-only and cannot express routing or budget rules.

**Execution paths:** Every `Route` has a `realtime` or `batch` execution path. Non-interactive tasks route to the Anthropic Batch API (50% cheaper, async settlement). The router decides based on whether a human is waiting on a live response.

**Policy enforcement:** Deterministic and outside the model. The model proposes tool calls; OPA returns `allow`, `deny`, or `require_approval`; a human resolves gray areas. A model can be persuaded — a policy engine cannot.

**Audit trail vs. tracing:** Two separate systems. Traces (OpenTelemetry → Logfire) answer "what happened and why was it slow?" The audit trail answers "who did what, who allowed it, under which policy version?" — append-only, hash-chained, permanent.

## Key types (`src/tack_ai/models.py`)

- `Task` — a unit of work with priority and timestamp
- `ToolCall` — a single tool invocation with its arguments and run ID
- `AuditRecord` — one event in the audit trail; holds actor, tool, policy decision, cost, and hash fields
- `PolicyDecision` — enum: `allow` / `deny` / `require_approval`

## What is built

- Module layout: sub-packages `core/`, `policy/`, `audit/`, `web/`, `memory/`; flat shims for backward compat
- Taskfile.yml: `task dev`, `task opa`, `task web`, `task test`, `task lint`
- OpenAPI docs: auth-gated `/docs` and `/redoc`
- Routing config UI: `/admin/settings` — runtime overrides for router type, budget, model tiers
- Observability: structlog JSON logging, Prometheus `/metrics`, audit CSV/JSON export at `/audit/export`
- Docker Compose: `app` + `opa` services, multi-stage Dockerfile, `.dockerignore`
- CD pipeline: `.github/workflows/cd.yml` — builds and pushes Docker image to GHCR on main
- Architecture docs: `docs/decisions/ARCHITECTURE.md` component map + data flow; ADRs in `docs/decisions/`
- Agent tool inventory: `docs/AGENTS.md` — all tools, policies, approval requirements
- OIDC / SSO: `web/oidc.py` — Google, GitHub, or generic OIDC via Authlib; role mapping from claims
