"""
Start or resume a durable DBOS agent run.

Usage
─────
    # Fresh run:
    uv run python scripts/durable_run.py "What is the Model Context Protocol?"

    # Resume a crashed or paused run:
    uv run python scripts/durable_run.py "What is MCP?" --workflow-id abc123

    # Show this help:
    uv run python scripts/durable_run.py --help

Prerequisites
─────────────
1.  Docker / Postgres running:  docker compose up -d postgres
2.  OPA running:                opa run --server --addr :8181 policies/
3.  Environment variables set:  DATABASE_URL, ANTHROPIC_API_KEY
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tack_ai.durable import run_durable


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the tack-ai agent as a DBOS durable workflow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("question", help="Question or task to run.")
    parser.add_argument(
        "--workflow-id",
        default=None,
        help="Workflow ID to start or resume.  Omit to generate a new one.",
    )
    args = parser.parse_args()
    asyncio.run(run_durable(args.question, args.workflow_id))


if __name__ == "__main__":
    main()
