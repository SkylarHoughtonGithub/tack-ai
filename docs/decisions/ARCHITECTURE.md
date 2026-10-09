# Architecture

This document describes how tack-ai fits together: its major components, the data
flow through the system for a typical task, and the reasoning behind key structural
decisions. The decision log lives in `docs/decisions/`.

---

## Component map

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser / CLI                                                       │
│  ┌────────────────────┐  ┌──────────────────────────────────────┐   │
│  │  CLI  tack-ai      │  │  Web console  tack-ai-web            │   │
│  │  agent.py          │  │  web/app.py  (FastAPI + SSE)         │   │
│  └─────────┬──────────┘  └────────────────┬─────────────────────┘   │
└────────────┼────────────────────────────────┼───────────────────────┘
             │                                │
             ▼                                ▼
     ┌───────────────┐              ┌──────────────────┐
     │  Router       │              │  RunState        │
     │  core/router  │◄─────────────│  (in-memory,     │
     │  LLM or rule  │              │   per-task)      │
     └───────┬───────┘              └──────────────────┘
             │  Route{tier, effort, path}
             ▼
     ┌───────────────┐
     │  Pydantic AI  │  agent.py
     │  Agent loop   │──────tool call──────►┌─────────────────┐
     │               │◄─────────────────────│  Policy engine  │
     └───────┬───────┘   allow/deny/        │  policy/engine  │
             │           require_approval   │  OPA (primary)  │
             │                             │  Cedar (P11)     │
             │ tool result                 └────────┬────────┘
             │                                      │
             │                              ┌───────▼──────────┐
             │                              │  Approval flow   │
             │                              │  web approval    │
             │                              │  or CLI prompt   │
             │                              └──────────────────┘
             │
             ▼
     ┌───────────────┐           ┌────────────────────┐
     │  Audit trail  │           │  Conversation      │
     │  audit/core   │           │  memory            │
     │  Postgres     │           │  memory/memory     │
     │  hash-chained │           │  Postgres pgvector │
     └───────────────┘           └────────────────────┘
```

---

## Data flow: task end to end

```
1. User submits prompt (web form or CLI)
      │
2. Router classifies the task
      │  Route: tier (simple/general/deep_reasoning)
      │         reasoning_effort (low/medium/high)
      │         execution_path (realtime/batch)
      │
3. Audit record written: "routing" event
      │
4. Memory context fetched from Postgres (if configured)
      │
5. Pydantic AI agent runs with selected model
      │
6. For each tool call the agent proposes:
      a. policy/engine.enforce() → OPA POST /v1/data/tack/policy/decision
      b. decision: allow → execute
                   deny  → return error to model
                   require_approval → pause, push approval_required event via SSE,
                                      wait for admin to click Approve or Deny
      c. Audit record written: "policy_decision" event
      │
7. Agent produces ResearchAnswer{summary, sources, confidence}
      │
8. Audit record written: "outcome" event with cost_usd
      │
9. Memory updated for next session
      │
10. SSE "answer" event pushed to browser
```

---

## Storage

| Store | What lives there |
|-------|-----------------|
| **Postgres** (pgvector) | Audit log, conversation turns, document chunks (embeddings), user accounts |
| **Postgres** (temporal schema) | Temporal workflow history (comparison module) |
| **OPA in-process** | Policy bundle, loaded from `policies/*.rego` at startup |
| **In-memory** | Active run states, pending approvals, session tokens |
| **config/runtime_settings.json** | Admin-overridden routing config (persisted across restarts) |

---

## Module layout

```
src/tack_ai/
├── agent.py                  Main Pydantic AI agent + tool definitions
├── observability.py          structlog setup, Prometheus counters
├── async_demo.py             Demo script
├── durable.py                DBOS durable-execution integration
├── mcp_gateway.py            MCP HTTP SSE gateway
├── mcp_server.py             MCP server stub
├── temporal_compare.py       Temporal workflow comparison module
│
├── core/                     Foundational types — no internal deps
│   ├── config.py             Pydantic Settings (env vars + .env)
│   ├── models.py             Task, ToolCall, AuditRecord, PolicyDecision
│   └── router.py             RouteTier, LLMRouter, RuleBasedRouter
│
├── policy/                   Policy enforcement
│   ├── engine.py             enforce(), OPA + Cedar integration
│   └── langgraph_approval.py LangGraph approval flow (stub; main path uses pydantic_graph)
│
├── audit/                    Append-only audit trail
│   └── core.py               Hash-chained Postgres writes + verification
│
├── web/                      FastAPI web console
│   ├── app.py                Routes, SSE, approval UI, settings, metrics
│   └── auth.py               UserManager (bcrypt), OpenFGA client
│
└── memory/                   RAG + conversation memory
    ├── memory.py             ConversationMemory (rolling summary)
    ├── retrieval.py          pgvector similarity search
    ├── ingestion.py          Chunk → embed → upsert pipeline
    └── sources.py            DocumentSource protocol + adapters
```

Flat shims (`models.py`, `config.py`, `router.py`, `auth.py`, etc.) re-export
from the canonical sub-package locations for backward compatibility.

---

## Security model

**Prompt injection defense:** All tool results (files, web pages, documents) are wrapped
in `[UNTRUSTED CONTENT]` markers in the system prompt and in the tool return value, so
the model is warned never to follow instructions found in tool output.

**Confused-deputy guard:** The OPA policy tracks `prior_tool` per run. If the model
reads a file and then immediately drafts an email, the policy escalates to
`require_approval` to detect potential secret exfiltration.

**Fail-closed:** If OPA is unreachable, `policy_check()` returns `deny`. The agent
cannot execute any tool without a reachable policy server.

**High-risk tools:** `run_code`, `send_email`, `delete_file` are in `_HIGH_RISK_TOOLS`.
The web UI never pre-approves these — each call requires a fresh human decision.

**Session tokens:** 32-byte cryptographically random URL-safe strings; 24-hour TTL;
stored in server memory only (no JWT, no persistent cookies beyond the random token).

---

## Phase map

| Phase | What was built |
|-------|---------------|
| 0–5   | Core agent, OPA policy, hash-chained audit trail, routing, fallback models |
| 6     | Postgres pgvector memory, RAG retrieval, OpenFGA document ACL |
| 7     | MCP gateway integration |
| 8     | (not documented here) |
| 9     | DBOS + Temporal durable execution (comparison module) |
| 10    | FastAPI web console, SSE, approval flow, bcrypt auth |
| 11    | Cedar policy comparison, LangGraph approval flow comparison |
| P1    | Ctrl+C fix, reasoning traces, CI pipeline, multi-turn chat, user management |
| P2    | Module layout, OpenAPI docs, settings UI, observability, OIDC (this branch) |
