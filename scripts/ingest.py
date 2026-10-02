"""
Ingest documents from all configured sources into pgvector.

Usage:
  docker compose up -d postgres
  uv run python scripts/ingest.py [--source local|http|all]

Sources:
  local — docs/sample/ (markdown files)
  http  — https://ai.pydantic.dev/ (pydantic-ai docs, depth 1)
  all   — both (default)
"""

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tack_ai.config import Settings
from tack_ai.ingestion import ingest_source
from tack_ai.sources import HttpSource, LocalFolderSource

LOCAL_SOURCE = LocalFolderSource(root=PROJECT_ROOT / "docs" / "sample")
HTTP_SOURCE = HttpSource(
    seed_urls=["https://ai.pydantic.dev/"],
    max_depth=1,
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="all", choices=["local", "http", "all"])
    args = parser.parse_args()

    settings = Settings()
    if not settings.database_url:
        print("ERROR: DATABASE_URL not set in .env")
        sys.exit(1)
    if not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set (needed for embeddings)")
        sys.exit(1)

    sources = []
    if args.source in ("local", "all"):
        sources.append(LOCAL_SOURCE)
    if args.source in ("http", "all"):
        sources.append(HTTP_SOURCE)

    for source in sources:
        print(f"\nIngesting {source.source_id} …")
        docs = source.list_documents()
        print(f"  Found {len(docs)} documents")
        result = await ingest_source(
            source,
            db_url=settings.database_url,
            openai_api_key=settings.get_key("openai"),
        )
        print(f"  New chunks: {result['new']}  |  Skipped (unchanged): {result['skipped']}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
