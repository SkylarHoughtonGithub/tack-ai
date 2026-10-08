"""
Conversation memory: recent turns kept in full, older turns summarized.

This keeps the context window bounded while preserving coherence across
long sessions.  The rolling summary is regenerated whenever a turn is
pushed past the recency window.
"""

from __future__ import annotations

import psycopg
from pydantic_ai import Agent

from tack_ai.core.router import build_model

RECENT_TURNS = 5  # keep this many turns verbatim; summarize the rest


class ConversationMemory:
    def __init__(self, db_url: str, model_str: str, settings: object) -> None:
        self._db_url = db_url
        self._summarizer: Agent[None, str] = Agent(
            build_model(model_str, settings),  # type: ignore[arg-type]
            output_type=str,
            system_prompt=(
                "You are a conversation summarizer. Given a list of turns, "
                "produce a concise paragraph (under 200 words) capturing the key "
                "topics, decisions, and context a reader would need to continue "
                "the conversation. Do not include pleasantries."
            ),
        )

    async def add_turn(self, session_id: str, role: str, content: str) -> None:
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COALESCE(MAX(turn_index), -1) FROM conversation_turns WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                next_index = (row[0] + 1) if row else 0
                await cur.execute(
                    "INSERT INTO conversation_turns (session_id, role, content, turn_index) VALUES (%s, %s, %s, %s)",
                    (session_id, role, content, next_index),
                )
            await conn.commit()

        await self._maybe_summarize(session_id)

    async def get_context(self, session_id: str) -> str:
        """Return a formatted context string: optional summary + recent turns."""
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT summary FROM conversation_summaries WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                summary = row[0] if row else None

                await cur.execute(
                    """
                    SELECT role, content FROM conversation_turns
                    WHERE session_id = %s
                    ORDER BY turn_index DESC LIMIT %s
                    """,
                    (session_id, RECENT_TURNS),
                )
                recent = list(reversed(await cur.fetchall()))

        parts = []
        if summary:
            parts.append(f"[Earlier context]\n{summary}")
        for role, content in recent:
            parts.append(f"{role.capitalize()}: {content}")
        return "\n\n".join(parts)

    async def _maybe_summarize(self, session_id: str) -> None:
        """If there are turns beyond the recency window, roll them into a summary."""
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM conversation_turns WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                total = row[0] if row else 0

                if total <= RECENT_TURNS:
                    return

                await cur.execute(
                    "SELECT up_to_turn FROM conversation_summaries WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                already_summarized_up_to = row[0] if row else -1

                cutoff = total - RECENT_TURNS - 1
                if cutoff <= already_summarized_up_to:
                    return

                await cur.execute(
                    """
                    SELECT role, content, turn_index
                    FROM conversation_turns
                    WHERE session_id = %s AND turn_index <= %s
                    ORDER BY turn_index
                    """,
                    (session_id, cutoff),
                )
                old_turns = await cur.fetchall()

                await cur.execute(
                    "SELECT summary FROM conversation_summaries WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                existing_summary = row[0] if row else None

        if not old_turns:
            return

        turns_text = "\n".join(f"{r}: {c}" for r, c, _ in old_turns)
        prompt = (
            f"{'Previous summary:\n' + existing_summary + chr(10) + chr(10) if existing_summary else ''}"
            f"New turns to incorporate:\n{turns_text}"
        )
        result = await self._summarizer.run(prompt)
        new_summary = result.output

        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_summaries (session_id, summary, up_to_turn)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE
                        SET summary    = EXCLUDED.summary,
                            up_to_turn = EXCLUDED.up_to_turn,
                            updated_at = NOW()
                    """,
                    (session_id, new_summary, cutoff),
                )
            await conn.commit()
