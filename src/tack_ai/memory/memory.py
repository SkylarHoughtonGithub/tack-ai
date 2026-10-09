"""
Conversation memory: persist and retrieve PydanticAI message history across sessions.

Stores serialized ModelMessage lists (tool calls, results, text) in Postgres so
sessions survive process restarts and are passed natively to agent.run().
"""

from __future__ import annotations

import psycopg
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

MAX_MESSAGES = 50  # trim oldest messages when history exceeds this

_messages_ta: TypeAdapter[list[ModelMessage]] = TypeAdapter(list[ModelMessage])


class ConversationMemory:
    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    async def get_messages(self, session_id: str) -> list[ModelMessage]:
        """Load persisted messages for a session, trimmed to MAX_MESSAGES."""
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT messages FROM conversation_messages WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
        if not row:
            return []
        messages = _messages_ta.validate_json(row[0])
        return messages[-MAX_MESSAGES:]

    async def save_messages(self, session_id: str, messages: list[ModelMessage]) -> None:
        """Persist the full message list from a completed run."""
        trimmed = messages[-MAX_MESSAGES:]
        json_str = _messages_ta.dump_json(trimmed).decode()
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_messages (session_id, messages)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (session_id) DO UPDATE
                        SET messages   = EXCLUDED.messages,
                            updated_at = NOW()
                    """,
                    (session_id, json_str),
                )
            await conn.commit()
