"""Sub-agents for the pydantic_graph coding workflow.

Each agent has its own system prompt, output_type, tool subset, and
PolicyEnforcementCapability so every tool call is OPA-gated independently.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from pydantic_ai import Agent, RunContext

from tack_ai.graph.state import CodingDeps, EditPlan, EditResult, GateDecision, RunTestResult
from tack_ai.policy import PolicyEnforcementCapability

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ── Shared tool implementations ───────────────────────────────────────────────
# Registered on multiple agents via agent.tool(func). Each agent gets its own
# policy gate even though the underlying function is shared.


async def read_file(ctx: RunContext[CodingDeps], filename: str) -> str:
    """Read a file from the project folder."""
    target = (_PROJECT_ROOT / filename).resolve()
    if not str(target).startswith(str(_PROJECT_ROOT)):
        return "Error: access outside project folder is not allowed."
    if not target.exists():
        return f"Error: file '{filename}' not found."
    if ctx.deps.settings.database_url:
        try:
            from tack_ai.memory.code_index import get_file_chunks  # noqa: PLC0415

            chunks = await get_file_chunks(str(target), ctx.deps.settings.database_url)
            if chunks:
                return "\n---\n".join(chunks)
        except Exception:
            pass
    return target.read_text()


async def search_code_chunks(ctx: RunContext[CodingDeps], query: str) -> str:
    """Search the embedded code index for chunks relevant to a query."""
    s = ctx.deps.settings
    if not s.database_url or not s.openai_api_key:
        return "Code search unavailable — no database configured."
    from tack_ai.memory.code_index import search_code  # noqa: PLC0415

    chunks = await search_code(query, s.database_url, s.get_key("openai"))
    if not chunks:
        return "No relevant code found."
    return "\n\n---\n\n".join(
        f"[{c['file_path']} chunk={c['chunk_index']} score={c['score']}]\n{c['content']}"
        for c in chunks
    )


async def write_file(ctx: RunContext[CodingDeps], path: str, content: str) -> str:
    """Write content to a file in the project folder."""
    target = (_PROJECT_ROOT / path).resolve()
    if not str(target).startswith(str(_PROJECT_ROOT)):
        return "Error: access outside project folder is not allowed."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    s = ctx.deps.settings
    if s.database_url and s.openai_api_key:
        from tack_ai.memory.code_index import embed_file  # noqa: PLC0415

        asyncio.create_task(
            embed_file(str(target), content, s.database_url, s.get_key("openai"))
        )
    return f"Written {len(content)} bytes to {path}."


async def run_tests(ctx: RunContext[CodingDeps]) -> str:
    """Run the project test suite (pytest) and return the output."""
    proc = subprocess.run(
        ["uv", "run", "pytest", "tests/", "-v", "--ignore=tests/e2e", "--tb=short", "-q"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_PROJECT_ROOT),
    )
    output = (proc.stdout + proc.stderr).strip()
    return output[:8000] if output else "(no output)"


# ── PlannerAgent — decomposes task into per-file edit steps ───────────────────

planner_agent: Agent[CodingDeps, EditPlan] = Agent(
    None,
    output_type=EditPlan,
    system_prompt=(
        "You are a coding planner. Given a task, decompose it into a list of concrete "
        "file edits. For each file, provide the exact relative file path and a clear, "
        "self-contained instruction for what to change. Use search_code_chunks to locate "
        "relevant existing code before planning. Use read_file to inspect specific files. "
        "Be precise: the edit agent will apply your instructions without additional context."
    ),
    capabilities=[PolicyEnforcementCapability()],
)
planner_agent.tool(read_file)
planner_agent.tool(search_code_chunks)


# ── EditAgent — applies a single file edit given a plan step ──────────────────

edit_agent: Agent[CodingDeps, EditResult] = Agent(
    None,
    output_type=EditResult,
    system_prompt=(
        "You are a coding agent. You receive a file path and an instruction. "
        "Read the current file content, apply the requested change, and write the "
        "updated content back with write_file. Return a concise diff field describing "
        "what you changed (not a unified diff — a plain English summary is fine). "
        "Set review_passed=True unless you could not complete the edit for a clear reason."
    ),
    capabilities=[PolicyEnforcementCapability()],
)
edit_agent.tool(read_file)
edit_agent.tool(write_file)
edit_agent.tool(search_code_chunks)


# ── ReviewAgent — gates the loop with a typed fix/escalate/pass decision ──────

review_agent: Agent[CodingDeps, GateDecision] = Agent(
    None,
    output_type=GateDecision,
    system_prompt=(
        "You are a code reviewer. You receive a summary of a coding task, the edits made, "
        "and the test results. Decide: 'pass' if tests pass and the work looks complete; "
        "'fix' if tests failed or the edits need improvement (the agent will retry); "
        "'escalate' if the situation requires a human (e.g., breaking changes, unclear task, "
        "repeated failures). Provide a confidence score (0–1) and a concise reason. "
        "Use read_file to inspect edited files if needed."
    ),
    capabilities=[PolicyEnforcementCapability()],
)
review_agent.tool(read_file)


# ── TestAgent — runs the test suite and produces a structured RunTestResult ──────

test_agent: Agent[CodingDeps, RunTestResult] = Agent(
    None,
    output_type=RunTestResult,
    system_prompt=(
        "You are a test runner. Call run_tests to execute the project test suite, "
        "then analyse the output. Set passed=True only if all tests pass. "
        "List failing test names in failed_tests. Include the full output in the output field."
    ),
    capabilities=[PolicyEnforcementCapability()],
)
test_agent.tool(run_tests)
