# Phase 9 — DBOS vs Temporal: what I observed

## What both frameworks give you

Both solve the same core problem: a long-running process (an LLM agent) must
survive crashes and resume without repeating completed work.  In both cases the
durability guarantee is: *if a step completed and its result was persisted,
that step does not re-run on restart.*

---

## DBOS (via `pydantic_ai.durable_exec.dbos.DBOSDurability`)

**Setup effort**

Add `dbos>=2.10.0` to deps and point it at the Postgres database you already
have.  No new server process.  DBOS creates its own schema tables on first run.
The pydantic-ai integration adds one line:

```python
durability = DBOSDurability()
agent = Agent(..., name="tack-ai-research", capabilities=[durability])
```

Wrap the `agent.run()` call in a `@DBOS.workflow()` and start it with
`SetWorkflowID` + `DBOS.start_workflow()`.  That is the entire setup.

**What becomes durable**

With `DBOSDurability`, every *model request* (the Anthropic API call) is
automatically a DBOS step.  Each tool call that the model triggers runs inside
the workflow (not in its own step), but the *model request that follows the
tool result* is a step.  So the resume granularity is: "from the last
successfully completed model call."

**Approval waiting**

`DBOS.recv(topic, timeout_seconds)` inside a workflow durably blocks until
`DBOS.send(workflow_id, value, topic)` is called from outside.  The wait
survives a crash: on restart DBOS checks whether the message arrived and
either replays the stored result or resumes blocking.

For simpler cases, a Postgres row works equally well (see
`pending_approvals` + `wait_for_db_approval()` in `durable.py`) — this is
what the project uses so approval visibility is trivially queryable.

**Debugging / visibility**

DBOS stores workflow state in Postgres (`dbos_workflow_status`,
`dbos_operation_outputs`).  You inspect it with `psql` or a dashboard if
DBOS Cloud is configured.  Visibility is adequate for development but less
rich than Temporal's UI.

**Verdict**

Use DBOS when you already have Postgres, want minimal ops overhead, and
are building a Python service.  Ideal for this project.

---

## Temporal (via `pydantic_ai.durable_exec.temporal.TemporalDurability`)

**Setup effort**

More moving parts:
1. Run a Temporal server (`docker compose up temporal`).
2. Define a `@workflow.defn` class with a `@workflow.run` method.
3. Define `@activity.defn` functions for each durable unit of work.
4. Run a **separate worker process** (`uv run python -m tack_ai.temporal_compare worker`).
5. Use a Temporal client to start and signal workflows.

The pydantic-ai integration adds:

```python
from pydantic_ai.durable_exec.temporal import TemporalDurability
agent = Agent(..., capabilities=[TemporalDurability()])
```

and the worker must be registered with the agent's activities via
`PydanticAIPlugin`.

**What becomes durable**

Temporal activities are the unit of durability.  With `TemporalDurability`
on the agent, each model request becomes a Temporal activity.  Tool calls that
are themselves activities are also independently durable.  The resume
granularity matches DBOS at the model-request level.

**Approval waiting**

Temporal uses *signals*: an operator calls
`handle.signal(AgentWorkflow.approve_tool, "send_email")`.  Inside the
workflow, `workflow.wait_condition(lambda: ...)` blocks until the signal
updates workflow state.  This is more explicit than DBOS message passing but
also more powerful: you can *query* the workflow's pending state
(`handle.query(AgentWorkflow.pending_tool)`) from a dashboard or API.

**Debugging / visibility**

Temporal's Web UI (`localhost:8088`) shows every workflow, its history,
pending signals, retries, and stack traces.  This is significantly richer than
DBOS.  For production multi-tenant agent systems, Temporal's visibility is a
major advantage.

**Verdict**

Use Temporal when operational complexity is acceptable and you need rich
visibility, strong multi-tenant isolation, or a polyglot environment
(Temporal has SDKs for Go, Java, TypeScript, etc.).

---

## Decision table

| Question                              | Choose    | Why                                           |
|---------------------------------------|-----------|-----------------------------------------------|
| Already have Postgres, minimal ops    | DBOS      | No new server; one DB                         |
| Need a rich workflow UI               | Temporal  | Built-in Web UI, history, retries visible     |
| Long-lived approvals (days)           | Either    | Both survive restarts; Temporal queries nicer |
| Polyglot workers (Go, Java, TS, Python) | Temporal | One server, many worker SDKs                 |
| Simple Python microservice            | DBOS      | Less ceremony                                 |
| Complex orchestration, fan-out        | Temporal  | Richer primitives (child workflows, etc.)     |

---

## Phase 9 implementation checklist

- [x] DBOS workflow wraps routing + model requests (every model call is a step)
- [x] Killing mid-run and restarting with same `--workflow-id` resumes the run
- [x] Approval persists in `pending_approvals` table; `send_approval.py` unblocks it
- [x] `send_email` deduplicated by `sent_emails.idempotency_key`
- [x] Temporal comparison module in `temporal_compare.py`
- [x] Audit trail appends new records on restart; chain stays intact
