"""
Phase 9 — Temporal comparison module.

This module implements the same durable agent workflow as durable.py but using
Temporal instead of DBOS.  Run it side-by-side with the DBOS version to compare
the two frameworks directly.

Key structural differences vs DBOS
────────────────────────────────────
DBOS                                   Temporal
──────────────────────────────────── │ ──────────────────────────────────────
@DBOS.workflow() on any async fn     │ @workflow.defn class with @workflow.run
@DBOS.step() for leaf operations     │ @activity.defn functions
DBOS.recv() / DBOS.send()            │ @workflow.signal + workflow.wait_condition
DBOSDurability capability on agent   │ TemporalDurability capability on agent
Single Postgres DB (existing)        │ Separate Temporal server process
Step retry config in @DBOS.step()    │ RetryPolicy on execute_activity()
In-process worker (auto)             │ Explicit Worker process (separate)
No task queue concept                │ task_queue routes work to workers

Setup
─────
    # Start a local Temporal dev server (installs via brew or docker):
    temporal server start-dev        # or: docker compose up temporal

    # In a separate terminal, run the Temporal worker:
    uv run python -m tack_ai.temporal_compare worker

    # In another terminal, start a durable run:
    uv run python -m tack_ai.temporal_compare run "What is MCP?" --workflow-id my-run-1

    # Approve a pending tool call:
    uv run python -m tack_ai.temporal_compare approve <workflow_id> <tool_name>
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

try:
    from temporalio import activity, workflow
    from temporalio.client import Client
    from temporalio.common import RetryPolicy
    from temporalio.worker import Worker

    _TEMPORAL_AVAILABLE = True
except ImportError:
    _TEMPORAL_AVAILABLE = False

from tack_ai.core.config import Settings

settings = Settings()
TASK_QUEUE = "tack-ai-agent"


# ── Activities (Temporal's equivalent of DBOS steps) ─────────────────────────

if _TEMPORAL_AVAILABLE:

    @activity.defn
    async def routing_activity(question: str) -> dict:
        """Route the question to a model tier.  Runs once; result is checkpointed."""
        from tack_ai.core.router import LLMRouter, RuleBasedRouter

        if settings.router_type == "llm" and settings.openai_api_key:
            try:
                route = await LLMRouter(
                    model_str="gpt-4o-mini", openai_api_key=settings.get_key("openai")
                ).route(question)
            except Exception:
                route = RuleBasedRouter().route(question)
        else:
            route = RuleBasedRouter().route(question)
        return route.model_dump()

    @activity.defn
    async def llm_activity(question: str, run_id: str) -> dict:
        """Run the pydantic-ai agent.  This activity is the durable LLM step.

        Inside a Temporal workflow, TemporalDurability makes each model request a
        Temporal activity in its own right (nested activities).  If the worker
        crashes mid-activity, Temporal retries only the failed activity — previously
        completed model requests are not re-run.
        """
        from tack_ai.agent import _CACHE_SETTINGS, agent
        from tack_ai.audit import current_run_id

        current_run_id.set(run_id)
        result = await agent.run(question, model_settings=_CACHE_SETTINGS)
        return result.output.model_dump()


# ── Workflow ──────────────────────────────────────────────────────────────────

if _TEMPORAL_AVAILABLE:

    @dataclass
    class ApprovalState:
        """Mutable state updated via signals from outside the workflow."""

        pending_tool: str | None = None
        decisions: dict[str, bool] = field(default_factory=dict)

    @workflow.defn
    class AgentWorkflow:
        """Temporal durable agent workflow — structural equivalent of durable.py."""

        def __init__(self) -> None:
            self._approval_state = ApprovalState()

        @workflow.run
        async def run(self, question: str, run_id: str) -> dict:
            """Orchestrate routing → LLM run with approval gating."""

            # Step 1: Route (activity — won't repeat on worker restart).
            route_dict = await workflow.execute_activity(
                routing_activity,
                question,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

            workflow.logger.info(
                "Routed",
                tier=route_dict["tier"],
                reason=route_dict["reason"],
            )

            # Step 2: LLM run (activity — won't repeat on worker restart).
            result = await workflow.execute_activity(
                llm_activity,
                args=[question, run_id],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            return result

        @workflow.signal
        async def approve_tool(self, tool_name: str) -> None:
            """Signal sent by an operator to approve a pending tool call."""
            self._approval_state.decisions[tool_name] = True
            self._approval_state.pending_tool = None

        @workflow.signal
        async def deny_tool(self, tool_name: str) -> None:
            """Signal sent by an operator to deny a pending tool call."""
            self._approval_state.decisions[tool_name] = False
            self._approval_state.pending_tool = None

        @workflow.query
        def pending_tool(self) -> str | None:
            """Query the workflow to see which tool (if any) is awaiting approval."""
            return self._approval_state.pending_tool

        async def _wait_for_approval(
            self,
            tool_name: str,
            timeout: timedelta = timedelta(hours=48),
        ) -> bool:
            """Block the workflow until approve_tool or deny_tool is signalled.

            Compared to DBOS: DBOS uses DBOS.recv() (a message) while Temporal
            uses signals + wait_condition (a state mutation).  Both survive
            restarts.  The Temporal approach allows querying the pending state
            from outside the workflow via the .pending_tool query.
            """
            self._approval_state.pending_tool = tool_name
            try:
                await workflow.wait_condition(
                    lambda: tool_name in self._approval_state.decisions,
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return False
            return self._approval_state.decisions.get(tool_name, False)


# ── Worker and client helpers ─────────────────────────────────────────────────


async def run_worker(temporal_host: str = "localhost:7233") -> None:
    """Run the Temporal worker.  Must be running for workflows to execute."""
    if not _TEMPORAL_AVAILABLE:
        raise RuntimeError("temporalio not installed.  Run: uv add temporalio")

    client = await Client.connect(temporal_host)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AgentWorkflow],
        activities=[routing_activity, llm_activity],
    )
    print(f"Temporal worker started (queue={TASK_QUEUE}, server={temporal_host})")
    await worker.run()


async def run_temporal(
    question: str,
    workflow_id: str | None = None,
    temporal_host: str = "localhost:7233",
) -> None:
    """Start or retrieve a durable Temporal agent run."""
    if not _TEMPORAL_AVAILABLE:
        raise RuntimeError("temporalio not installed.  Run: uv add temporalio")

    run_id = workflow_id or str(uuid.uuid4())
    wf_id = f"tack-ai-{run_id}"

    client = await Client.connect(temporal_host)
    handle = await client.start_workflow(
        AgentWorkflow.run,
        args=[question, run_id],
        id=wf_id,
        task_queue=TASK_QUEUE,
    )

    print(f"\nQuestion: {question}")
    print(f"Workflow: {wf_id}  (Temporal)")
    print(f"URL:      http://localhost:8088/namespaces/default/workflows/{wf_id}")

    result = await handle.result()
    print(f"\nSummary: {result['summary']}")


async def send_approval_signal(
    workflow_id: str,
    tool_name: str,
    *,
    approve: bool,
    temporal_host: str = "localhost:7233",
) -> None:
    """Send an approve or deny signal to a running Temporal workflow."""
    if not _TEMPORAL_AVAILABLE:
        raise RuntimeError("temporalio not installed.  Run: uv add temporalio")

    client = await Client.connect(temporal_host)
    handle = client.get_workflow_handle(workflow_id)
    if approve:
        await handle.signal(AgentWorkflow.approve_tool, tool_name)
        print(f"Approved '{tool_name}' on workflow {workflow_id}")
    else:
        await handle.signal(AgentWorkflow.deny_tool, tool_name)
        print(f"Denied '{tool_name}' on workflow {workflow_id}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Temporal comparison runner")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run")
    p_run.add_argument("question")
    p_run.add_argument("--workflow-id")
    p_run.add_argument("--host", default="localhost:7233")

    sub.add_parser("worker").add_argument("--host", default="localhost:7233")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("workflow_id")
    p_approve.add_argument("tool_name")
    p_approve.add_argument("--deny", action="store_true")
    p_approve.add_argument("--host", default="localhost:7233")

    args = parser.parse_args()

    if args.cmd == "run":
        asyncio.run(run_temporal(args.question, args.workflow_id, args.host))
    elif args.cmd == "worker":
        asyncio.run(run_worker(args.host))
    elif args.cmd == "approve":
        asyncio.run(
            send_approval_signal(
                args.workflow_id,
                args.tool_name,
                approve=not args.deny,
                temporal_host=args.host,
            )
        )
    else:
        parser.print_help()
