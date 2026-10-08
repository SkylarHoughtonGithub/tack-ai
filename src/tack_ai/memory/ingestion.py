"""
Ingestion pipeline: chunk → embed → upsert to pgvector.

The pipeline is source-agnostic: it calls list_documents() and read_document()
from any DocumentSource and stores results in the document_chunks table.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg
from openai import AsyncOpenAI
from pgvector.psycopg import register_vector_async

from tack_ai.memory.sources import DocumentSource

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536
CHUNK_SIZE = 800      # characters
CHUNK_OVERLAP = 100   # characters


@dataclass
class Chunk:
    source_id: str
    source_type: str
    origin: str
    chunk_index: int
    content: str
    content_hash: str


def chunk_text(text: str) -> list[str]:
    """Fixed-size chunking with overlap."""
    if not text.strip():
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def embed_batch(texts: list[str], client: AsyncOpenAI) -> list[list[float]]:
    if not texts:
        return []
    resp = await client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in resp.data]


async def ingest_source(
    source: DocumentSource,
    db_url: str,
    openai_api_key: str,
    *,
    batch_size: int = 20,
) -> dict[str, int]:
    """
    Ingest all documents from a source into pgvector.
    Skips chunks whose content_hash is already stored (idempotent).
    Returns {"new": n, "skipped": n} counts.
    """
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    docs = source.list_documents()

    new_count = 0
    skipped_count = 0

    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await register_vector_async(conn)

        for doc in docs:
            text = source.read_document(doc.id)
            raw_chunks = chunk_text(text)
            chunks = [
                Chunk(
                    source_id=doc.source_id,
                    source_type=doc.source_type,
                    origin=doc.origin,
                    chunk_index=i,
                    content=c,
                    content_hash=_sha256(c),
                )
                for i, c in enumerate(raw_chunks)
            ]

            # Filter to only chunks that need embedding.
            existing_hashes = set()
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT content_hash FROM document_chunks WHERE source_id = %s AND origin = %s",
                    (doc.source_id, doc.origin),
                )
                rows = await cur.fetchall()
                existing_hashes = {r[0] for r in rows}

            new_chunks = [c for c in chunks if c.content_hash not in existing_hashes]
            skipped_count += len(chunks) - len(new_chunks)

            for i in range(0, len(new_chunks), batch_size):
                batch = new_chunks[i : i + batch_size]
                embeddings = await embed_batch([c.content for c in batch], openai_client)
                async with conn.cursor() as cur:
                    for chunk, vec in zip(batch, embeddings):
                        await cur.execute(
                            """
                            INSERT INTO document_chunks
                                (source_id, source_type, origin, chunk_index,
                                 content, content_hash, embedding)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (source_id, origin, chunk_index) DO UPDATE
                                SET content      = EXCLUDED.content,
                                    content_hash = EXCLUDED.content_hash,
                                    embedding    = EXCLUDED.embedding,
                                    ingested_at  = NOW()
                            """,
                            (
                                chunk.source_id, chunk.source_type, chunk.origin,
                                chunk.chunk_index, chunk.content, chunk.content_hash,
                                vec,
                            ),
                        )
                await conn.commit()
                new_count += len(batch)

    return {"new": new_count, "skipped": skipped_count}
