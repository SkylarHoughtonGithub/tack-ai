"""
OpenFGA authorization wrapper for RAG retrieval.

Authorization model:
  type user
  type source
    relations
      define viewer: [user]

A user who has the "viewer" relation on a source can receive chunks from it.
The setup script (scripts/setup_fga.py) creates the store, writes the model,
and seeds permission tuples.
"""

from __future__ import annotations

from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import ClientCheckRequest


class FGAClient:
    def __init__(self, api_url: str, store_id: str, model_id: str) -> None:
        self._config = ClientConfiguration(
            api_url=api_url,
            store_id=store_id,
            authorization_model_id=model_id,
        )

    async def can_view(self, user: str, source_id: str) -> bool:
        """Return True if `user` has viewer on `source_id`."""
        async with OpenFgaClient(self._config) as client:
            resp = await client.check(
                ClientCheckRequest(
                    user=f"user:{user}",
                    relation="viewer",
                    object=f"source:{_fga_key(source_id)}",
                )
            )
            return bool(resp.allowed)

    async def grant_view(self, user: str, source_id: str) -> None:
        """Grant viewer on source_id to user.  Idempotent."""
        from openfga_sdk.client.models import ClientTuple
        async with OpenFgaClient(self._config) as client:
            await client.write(
                body={"writes": [
                    ClientTuple(
                        user=f"user:{user}",
                        relation="viewer",
                        object=f"source:{_fga_key(source_id)}",
                    )
                ]}
            )

    async def revoke_view(self, user: str, source_id: str) -> None:
        from openfga_sdk.client.models import ClientTuple
        async with OpenFgaClient(self._config) as client:
            await client.delete_tuples(
                body=[
                    ClientTuple(
                        user=f"user:{user}",
                        relation="viewer",
                        object=f"source:{_fga_key(source_id)}",
                    )
                ]
            )


def _fga_key(source_id: str) -> str:
    """Sanitize source_id into a valid OpenFGA object ID (no colons/slashes)."""
    return source_id.replace(":", "_").replace("/", "_").replace(".", "_")
