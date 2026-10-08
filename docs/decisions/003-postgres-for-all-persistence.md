# ADR-003: Consolidate all persistence in Postgres

**Status:** Accepted  
**Date:** 2025-10 (supersedes earlier SQLite audit trail)

---

## Context

Early phases stored the audit trail in SQLite. The memory/retrieval module required
Postgres for pgvector. Temporal (Phase 9) also requires Postgres. Running two
different databases for the same project added operational overhead without benefit.

## Decision

Use a **single Postgres instance** for all persistence:
- Audit log (`audit_log` table)
- Conversation memory (`conversation_turns`, `rolling_summaries` tables)
- Document embeddings (`document_chunks` table, pgvector extension)
- User accounts (`users` table)
- Temporal workflow history (separate Temporal-managed schema)

## Rationale

- **One database to operate:** Docker Compose starts one `postgres` container
  with the pgvector image. All services connect to it.
- **Migrations are centralized:** `migrations/001_init.sql` creates all
  application tables. Temporal manages its own schema on first run.
- **Hash-chain integrity:** The audit trail's hash chain requires strict
  ordering. Postgres provides serializable transactions and a reliable
  auto-incrementing ID; SQLite's WAL mode was adequate but added a dependency.
- **pgvector:** Required for the RAG retrieval module. There is no SQLite
  equivalent. Postgres already being required made the audit-trail migration
  zero-cost.

## Consequences

- `DATABASE_URL` is required for full functionality. If it is not set, the
  agent still runs (memory and document search are disabled with warnings) but
  audit records are not persisted. This is acceptable for local development.
- Docker Compose is the canonical dev setup. `task up` starts the database.
- Alembic / SQLModel migrations are not yet in use (tracked as P3 in the
  enhancements backlog). Schema changes are currently applied by re-running
  `migrations/001_init.sql` against a clean database.
