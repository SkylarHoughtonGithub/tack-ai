"""
Retrieval: embed query → pgvector similarity search → return top results.
"""

from __future__ import annotations

import psycopg
from openai import AsyncOpenAI
from pgvector.psycopg import register_vector_async

from tack_ai.ingestion import EMBED_MODEL

TOP_K = 8
RETURN_K = 5


async def search_documents(
    query: str,
    user: str,
    db_url: str,
    openai_api_key: str,
    *,
    source_id: str | None = None,
) -> list[dict]:
    """
    Embed the query, find the nearest chunks, filter by FGA permissions, and
    return up to RETURN_K results.

    Returns a list of dicts with keys: source_id, origin, chunk_index, content, score.
    """
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    resp = await openai_client.embeddings.create(model=EMBED_MODEL, input=[query])
    query_vec = resp.data[0].embedding

    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await register_vector_async(conn)
        async with conn.cursor() as cur:
            if source_id:
                await cur.execute(
                    """
                    SELECT source_id, origin, chunk_index, content,
                           1 - (embedding <=> %s::vector) AS score
                    FROM document_chunks
                    WHERE source_id = %s AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_vec, source_id, query_vec, TOP_K),
                )
            else:
                await cur.execute(
                    """
                    SELECT source_id, origin, chunk_index, content,
                           1 - (embedding <=> %s::vector) AS score
                    FROM document_chunks
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_vec, query_vec, TOP_K),
                )
            rows = await cur.fetchall()

    return [
        {
            "source_id": sid,
            "origin": origin,
            "chunk_index": chunk_idx,
            "content": content,
            "score": round(float(score), 4),
        }
        for sid, origin, chunk_idx, content, score in rows
    ][:RETURN_K]


def format_for_prompt(results: list[dict]) -> str:
    """Format retrieval results into a compact string for the model context."""
    if not results:
        return "No relevant documents found."
    parts = []
    for r in results:
        parts.append(
            f"[Source: {r['origin']}  score={r['score']}]\n{r['content']}"
        )
    return "\n\n---\n\n".join(parts)
