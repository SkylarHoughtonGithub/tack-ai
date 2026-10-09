"""
Code chunk index: embed source files into pgvector for semantic retrieval.

embed_file() is called whenever write_file executes; search_code() is called
by EditNodes to retrieve relevant context without full-file dumps.
"""

from __future__ import annotations

import psycopg
from openai import AsyncOpenAI
from pgvector.psycopg import register_vector_async

from tack_ai.memory.ingestion import EMBED_MODEL, _sha256, chunk_text, embed_batch

TOP_K = 5


async def embed_file(
    file_path: str,
    content: str,
    db_url: str,
    openai_api_key: str,
) -> None:
    """Chunk a file and upsert embeddings into code_chunks. Skips unchanged chunks."""
    client = AsyncOpenAI(api_key=openai_api_key)
    raw_chunks = chunk_text(content)
    if not raw_chunks:
        return

    hashes = [_sha256(c) for c in raw_chunks]

    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await register_vector_async(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT chunk_index, content_hash FROM code_chunks WHERE file_path = %s",
                (file_path,),
            )
            existing = {idx: h for idx, h in await cur.fetchall()}

        new_indices = [i for i, h in enumerate(hashes) if existing.get(i) != h]
        if not new_indices:
            return

        vectors = await embed_batch([raw_chunks[i] for i in new_indices], client)

        async with conn.cursor() as cur:
            for i, vec in zip(new_indices, vectors):
                await cur.execute(
                    """
                    INSERT INTO code_chunks (file_path, chunk_index, content, content_hash, embedding)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (file_path, chunk_index) DO UPDATE
                        SET content      = EXCLUDED.content,
                            content_hash = EXCLUDED.content_hash,
                            embedding    = EXCLUDED.embedding,
                            updated_at   = NOW()
                    """,
                    (file_path, i, raw_chunks[i], hashes[i], vec),
                )
        await conn.commit()


async def get_file_chunks(file_path: str, db_url: str) -> list[str]:
    """Return ordered text chunks for a specific file, or [] if not indexed."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content FROM code_chunks WHERE file_path = %s ORDER BY chunk_index",
                (file_path,),
            )
            rows = await cur.fetchall()
    return [row[0] for row in rows]


async def search_code(
    query: str,
    db_url: str,
    openai_api_key: str,
    top_k: int = TOP_K,
) -> list[dict]:
    """Return the top-k most relevant code chunks for a query string."""
    client = AsyncOpenAI(api_key=openai_api_key)
    resp = await client.embeddings.create(model=EMBED_MODEL, input=[query])
    query_vec = resp.data[0].embedding

    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await register_vector_async(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT file_path, chunk_index, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM code_chunks
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, query_vec, top_k),
            )
            rows = await cur.fetchall()

    return [
        {
            "file_path": fp,
            "chunk_index": ci,
            "content": content,
            "score": round(float(score), 4),
        }
        for fp, ci, content, score in rows
    ]
