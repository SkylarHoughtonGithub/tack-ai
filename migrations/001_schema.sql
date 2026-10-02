-- Phase 6: vector store, conversation memory
-- Runs automatically via docker-entrypoint-initdb.d on first start.

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Document chunks ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS document_chunks (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT    NOT NULL,   -- e.g. "local:docs/sample" or "http:https://example.com"
    source_type TEXT    NOT NULL,   -- "local" | "http"
    origin      TEXT    NOT NULL,   -- absolute file path or full URL
    chunk_index INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    content_hash TEXT   NOT NULL,   -- SHA-256 of content; used to skip re-embedding unchanged chunks
    embedding   vector(1536),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (source_id, origin, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_source
    ON document_chunks (source_id);

CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- ── Conversation memory ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS conversation_turns (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,       -- "user" | "assistant"
    content    TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (session_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_turns_session
    ON conversation_turns (session_id, turn_index);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    session_id TEXT    PRIMARY KEY,
    summary    TEXT    NOT NULL,
    up_to_turn INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
