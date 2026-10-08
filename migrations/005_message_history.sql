-- Native PydanticAI message history and code semantic search.
-- Replaces string-based conversation_turns / conversation_summaries (kept for
-- reference but no longer written to by the agent).

CREATE TABLE IF NOT EXISTS conversation_messages (
    session_id TEXT PRIMARY KEY,
    messages   JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Semantic index of source files; updated whenever write_file is called.
CREATE TABLE IF NOT EXISTS code_chunks (
    id           BIGSERIAL PRIMARY KEY,
    file_path    TEXT    NOT NULL,
    chunk_index  INTEGER NOT NULL,
    content      TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    embedding    vector(1536),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (file_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding
    ON code_chunks USING hnsw (embedding vector_cosine_ops);
