# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A personal AI agent harness built as a structured learning project. The goal is a research/operations assistant that routes tasks across Anthropic and OpenAI models, enforces policy via OPA before every tool call, and records everything in a tamper-evident audit trail. The plan lives at `docs/AI Agent Harness Learning Project Plan.md` (untracked by git).

## Commands

```bash
# Start OPA policy server (required before running the agent)
opa run --server --addr :8181 policies/

# Run the agent (in a separate terminal)
uv run python -m tack_ai.agent
```

All Python commands use `uv run` — no need to activate the venv manually.

## Architecture decisions

**Framework:** Pydantic AI (primary). LangGraph appears in Phase 11 as a standalone approval-flow module for comparison — not a replacement.

**Policy engine:** OPA (primary). The policy layer governs three things: tool authorization, routing decisions (which model/tier), and budget enforcement. Cedar is authorization-only and cannot cover routing/budget rules, so it stays in Phase 11 as a comparison module for the tool-authorization subset only.

**Execution paths:** Every `Route` has a `realtime` or `batch` execution path. Non-interactive tasks route to the Anthropic Batch API (50% cheaper, async settlement). The router decides based on whether a human is waiting on a live response.

**Policy enforcement:** Deterministic and outside the model. The model proposes tool calls; OPA returns `allow`, `deny`, or `require_approval`; a human resolves gray areas. A model can be persuaded — a policy engine cannot.

**Audit trail vs. tracing:** Two separate systems. Traces (OpenTelemetry → Logfire) answer "what happened and why was it slow?" The audit trail answers "who did what, who allowed it, under which policy version?" — append-only, hash-chained, permanent.

## Key types (`src/tack_ai/models.py`)

- `Task` — a unit of work with priority and timestamp
- `ToolCall` — a single tool invocation with its arguments and run ID
- `AuditRecord` — one event in the audit trail; holds actor, tool, policy decision, cost, and hash fields (hash fields added in Phase 4)
- `PolicyDecision` — enum: `allow` / `deny` / `require_approval`

## Phase status

**Phases 0–7 complete.** Next: Phase 8 — security hardening and red-teaming.
