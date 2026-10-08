# tack-ai

A personal AI agent harness that routes tasks across Anthropic and OpenAI models, enforces policy via OPA before every tool call, and records everything in a tamper-evident audit trail. Built as a structured learning project across 12 phases.

## What it does

- **Routes tasks** to the right model tier (simple / general / deep reasoning) via a configurable LLM or rule-based router
- **Enforces policy** with OPA — every tool call gets an `allow`, `deny`, or `require_approval` decision before execution
- **Audits everything** in a hash-chained, append-only Postgres table with secret redaction
- **Manages memory** through rolling conversation summarization and pgvector-backed semantic search
- **Web console** for task queue, human approval, history, audit export, and user management (local accounts + OIDC/SSO)

## Architecture

```
┌─────────────┐    task     ┌──────────┐   route   ┌─────────────────┐
│  Web / CLI  │────────────▶│  Router  │──────────▶│  Anthropic API  │
└─────────────┘             └──────────┘           │  OpenAI API     │
                                                   └─────────────────┘
        │                        │ tool call
        │                   ┌────▼─────┐
        │                   │   OPA    │  allow / deny / require_approval
        │                   └──────────┘
        │                        │
        │                   ┌────▼──────────┐
        │                   │  Audit Trail  │  hash-chained Postgres
        │                   └───────────────┘
        │
   ┌────▼──────────┐
   │  Memory / RAG │  pgvector + rolling summarization
   └───────────────┘
```

**Key components:**

| Component | Technology | Purpose |
|-----------|------------|---------|
| Agent | Pydantic AI | 8 tools, prompt caching, cost tracking |
| Policy engine | OPA (primary), Cedar (comparison) | Tool authorization, routing, budget |
| Approval flow | LangGraph | Human-in-the-loop for gray-area decisions |
| Access control | OpenFGA | Fine-grained per-resource permissions |
| Memory | Postgres + pgvector | Conversation history + semantic doc search |
| Observability | structlog, Prometheus, OpenTelemetry, Logfire | Traces, metrics, structured logs |
| Durable execution | DBOS / Temporal | Exactly-once side effects (optional) |

## Quick start

### Prerequisites

- Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker
- `ANTHROPIC_API_KEY` (required); `OPENAI_API_KEY` (optional)

### Local development

```bash
# 1. Clone and install deps
git clone https://github.com/SkylarHoughtonGithub/tack-ai && cd tack-ai
uv sync --all-groups

# 2. Configure environment
cp .env.example .env
# edit .env — set ANTHROPIC_API_KEY at minimum

# 3. Start infrastructure (Postgres, OPA, OpenFGA)
docker compose up postgres opa openfga -d

# 4. Start the web console
uv run tack-ai-web
# → http://localhost:8000  (default: admin / changeme)

# 5. Or run the agent CLI
uv run python -m tack_ai.agent
```

### Docker (full stack)

```bash
cp .env.example .env  # set API keys
docker compose up
# → http://localhost:8000
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic model access |
| `OPENAI_API_KEY` | No | OpenAI model access |
| `DATABASE_URL` | No | Postgres connection string; enables user DB and memory |
| `OPENFGA_URL` | No | OpenFGA server URL for fine-grained access control |
| `WEB_USERNAME` / `WEB_PASSWORD` | No | Override default `admin` / `changeme` credentials |
| `OIDC_PROVIDER` | No | `google`, `github`, or `oidc` for SSO login |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | If OIDC | OAuth application credentials |
| `OIDC_ADMIN_EMAILS` | No | Comma-separated emails granted admin role via OIDC |
| `LOGFIRE_TOKEN` | No | Pydantic Logfire token for AI observability |
| `LOG_JSON` | No | `true` for JSON logs (default), `false` for human-readable |

## Commands

```bash
uv run tack-ai-web              # web console (preferred over uvicorn --reload)
uv run python -m tack_ai.agent  # CLI agent

uv run pytest tests/ -v --ignore=tests/e2e  # unit tests
uv run ruff check src/          # lint
uv run mypy src/tack_ai/        # type-check
uv run bandit -r src/tack_ai/ -ll  # security scan

# Task runner shortcuts (requires https://taskfile.dev)
task dev    # starts all infrastructure + web console
task web    # web console only
task opa    # OPA policy server only
task test   # run test suite
task lint   # ruff + mypy + bandit
```

## Agent tools

The agent has 8 tools, each gated by OPA policy before execution:

| Tool | Risk | Approval required |
|------|------|-------------------|
| `web_search` | Low | No |
| `read_file` | Low | No |
| `search_documents` | Low | No |
| `draft_email` | Medium | No |
| `write_file` | Medium | Yes (configurable) |
| `run_code` | High | Yes |
| `send_email` | High | Yes |
| `delete_file` | High | Yes |

See [`docs/AGENTS.md`](docs/AGENTS.md) for the full policy reference.

## Policies

OPA policies live in `policies/`. They govern three things:

1. **Tool authorization** — which tools a role may call
2. **Routing** — which model tier a task is assigned to
3. **Budget enforcement** — per-session and daily cost caps

The policy server must be running before the agent starts. With Docker Compose it starts automatically; locally: `opa run --server --addr :8181 policies/`.
