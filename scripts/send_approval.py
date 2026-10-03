"""
Approve or deny a pending tool call from a durable run.

Usage
─────
    uv run python scripts/send_approval.py <approval_id> --approve
    uv run python scripts/send_approval.py <approval_id> --deny

The approval_id is printed to the terminal when a durable run hits a tool that
requires approval.  It is also visible in the pending_approvals Postgres table:

    SELECT approval_id, run_id, tool_name, status FROM pending_approvals;
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


async def resolve(approval_id: str, *, approve: bool) -> None:
    import psycopg
    from tack_ai.config import Settings

    settings = Settings()
    if not settings.database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    status = "approved" if approve else "denied"
    resolver = "operator"

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        await conn.execute(
            """
            UPDATE pending_approvals
            SET status = %s, resolved_at = NOW(), resolver = %s
            WHERE approval_id = %s
            """,
            (status, resolver, approval_id),
        )
        await conn.commit()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, tool_name, status FROM pending_approvals WHERE approval_id = %s",
                (approval_id,),
            )
            row = await cur.fetchone()

    if row:
        print(f"{'Approved' if approve else 'Denied'}  tool={row[1]}  run={row[0]}  (id={approval_id})")
    else:
        print(f"WARNING: approval_id {approval_id!r} not found — nothing changed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Approve or deny a pending durable-run tool call.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("approval_id", help="Approval ID printed by the durable run.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true", help="Approve the tool call.")
    group.add_argument("--deny", action="store_true", help="Deny the tool call.")
    args = parser.parse_args()
    asyncio.run(resolve(args.approval_id, approve=args.approve))


if __name__ == "__main__":
    main()
