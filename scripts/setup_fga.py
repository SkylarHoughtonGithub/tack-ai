"""
One-time OpenFGA setup: create a store, write the authorization model, and seed
permission tuples for two test users.

  alice — can view all sources (local + http)
  bob   — can view only the http source

Run after starting OpenFGA:
  docker compose up -d openfga
  uv run python scripts/setup_fga.py

Outputs OPENFGA_STORE_ID and OPENFGA_MODEL_ID — add these to your .env.
"""

import asyncio
import sys

from openfga_sdk import (
    ClientConfiguration,
    OpenFgaClient,
    WriteAuthorizationModelRequest,
)
from openfga_sdk.client.models import ClientTuple
from openfga_sdk.models import TypeDefinition, Userset, Usersets

from tack_ai.auth import _fga_key
from tack_ai.config import Settings
from tack_ai.sources import LocalFolderSource, HttpSource
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SOURCE = LocalFolderSource(root=PROJECT_ROOT / "docs" / "sample")
HTTP_SOURCE = HttpSource(seed_urls=["https://ai.pydantic.dev/"])


FGA_MODEL = WriteAuthorizationModelRequest(
    schema_version="1.1",
    type_definitions=[
        TypeDefinition(type="user", relations={}),
        TypeDefinition(
            type="source",
            relations={
                "viewer": Userset(
                    this={},
                    union=Usersets(child=[]),
                ),
            },
            metadata={
                "relations": {
                    "viewer": {
                        "directly_related_user_types": [{"type": "user"}]
                    }
                }
            },
        ),
    ],
)


async def main() -> None:
    settings = Settings()
    openfga_url = settings.openfga_url

    config = ClientConfiguration(api_url=openfga_url)
    async with OpenFgaClient(config) as client:
        # Create store
        store = await client.create_store({"name": "tack-ai"})
        store_id = store.id
        print(f"OPENFGA_STORE_ID={store_id}")

        # Write authorization model
        config2 = ClientConfiguration(api_url=openfga_url, store_id=store_id)
        async with OpenFgaClient(config2) as client2:
            model_resp = await client2.write_authorization_model(FGA_MODEL)
            model_id = model_resp.authorization_model_id
            print(f"OPENFGA_MODEL_ID={model_id}")

            local_key = _fga_key(LOCAL_SOURCE.source_id)
            http_key = _fga_key(HTTP_SOURCE.source_id)

            # alice can view both sources
            await client2.write(body={"writes": [
                ClientTuple(user="user:alice", relation="viewer", object=f"source:{local_key}"),
                ClientTuple(user="user:alice", relation="viewer", object=f"source:{http_key}"),
            ]})

            # bob can only view the http source
            await client2.write(body={"writes": [
                ClientTuple(user="user:bob", relation="viewer", object=f"source:{http_key}"),
            ]})

            print("\nPermissions written:")
            print(f"  alice → viewer → {local_key}")
            print(f"  alice → viewer → {http_key}")
            print(f"  bob   → viewer → {http_key}")
            print("\nAdd to .env:")
            print(f"  OPENFGA_STORE_ID={store_id}")
            print(f"  OPENFGA_MODEL_ID={model_id}")


if __name__ == "__main__":
    asyncio.run(main())
