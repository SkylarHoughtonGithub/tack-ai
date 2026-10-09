#!/usr/bin/env bash
# Smoke test for the pydantic_graph coding workflow.
# Runs a simple task end-to-end: Plan → Edit → Test → Gate.
# Requires OPA running (task opa or docker-compose up opa).
uv run python3 - <<'EOF'
import asyncio
from tack_ai.graph.workflow import run_coding_task
from tack_ai.graph.state import CodingApprovalRequired
from tack_ai.core.config import Settings

async def main():
    try:
        result = await run_coding_task(
            'Create drafts/hello.py with a single hello_world() function that returns the string "hello, world"',
            settings=Settings(),
            budget_usd=0.50,
        )
        print(result)
        if result.test_result:
            print(f"\n--- test output ---\n{result.test_result.output[:2000]}")
    except CodingApprovalRequired as e:
        print(f"Escalated: {e.reason or '(no reason)'}")
        print(f"Run ID: {e.run_id}")
        if e.test_result:
            print(f"\n--- test output ---\n{e.test_result.output[:3000]}")
    except Exception as e:
        print(f"Error ({type(e).__name__}): {e}")

asyncio.run(main())
EOF
