"""
One-time OpenFGA setup: create a store and write the authorization model.

Model:
  type run                        — an agent run identified by its run_id
  type tool
    relations
      define executor: [run]      — a run granted executor may call this tool

Grants are written at approval time (web UI approve button) and verified by
enforce() before the tool executes. No static permission tuples are seeded —
all grants are created dynamically at runtime.

Run after starting OpenFGA:
  docker compose up -d openfga
  uv run python scripts/setup_fga.py

Add the printed IDs to .env.
"""

import asyncio

from openfga_sdk import (
    ClientConfiguration,
    OpenFgaClient,
    WriteAuthorizationModelRequest,
)
from openfga_sdk.models import TypeDefinition, Userset

from tack_ai.core.config import Settings


FGA_MODEL = WriteAuthorizationModelRequest(
    schema_version="1.1",
    type_definitions=[
        TypeDefinition(type="run", relations={}),
        TypeDefinition(
            type="tool",
            relations={"executor": Userset(this={})},
            metadata={
                "relations": {
                    "executor": {
                        "directly_related_user_types": [{"type": "run"}]
                    }
                }
            },
        ),
    ],
)


async def main() -> None:
    settings = Settings()
    config = ClientConfiguration(api_url=settings.openfga_url)

    async with OpenFgaClient(config) as client:
        store = await client.create_store({"name": "tack-ai"})
        store_id = store.id

        config2 = ClientConfiguration(api_url=settings.openfga_url, store_id=store_id)
        async with OpenFgaClient(config2) as client2:
            model_resp = await client2.write_authorization_model(FGA_MODEL)
            model_id = model_resp.authorization_model_id

    print("Add to .env:")
    print(f"  OPENFGA_STORE_ID={store_id}")
    print(f"  OPENFGA_MODEL_ID={model_id}")


if __name__ == "__main__":
    asyncio.run(main())
