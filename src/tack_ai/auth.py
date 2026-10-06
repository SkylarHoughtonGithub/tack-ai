"""
OpenFGA authorization for per-run tool grants.

Model:
  type run
  type tool
    relations
      define executor: [run]

When a tool call requiring approval is approved, a tuple is written granting
that specific run executor access on that tool. enforce() verifies the grant
exists before proceeding. This makes approvals auditable and revocable: a grant
can be deleted mid-run from the approvals page to stop execution even after
the initial approval.
"""

from __future__ import annotations

from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import ClientCheckRequest, ClientTuple, ClientWriteRequest

_client: FGAClient | None = None


def get_fga_client() -> FGAClient | None:
    """Return singleton FGAClient if configured, else None."""
    global _client
    if _client is not None:
        return _client
    from tack_ai.config import Settings
    s = Settings()
    if s.openfga_store_id and s.openfga_model_id:
        _client = FGAClient(
            api_url=s.openfga_url,
            store_id=s.openfga_store_id,
            model_id=s.openfga_model_id,
        )
    return _client


class FGAClient:
    def __init__(self, api_url: str, store_id: str, model_id: str) -> None:
        self._config = ClientConfiguration(
            api_url=api_url,
            store_id=store_id,
            authorization_model_id=model_id,
        )

    async def grant_tool(self, run_id: str, tool_name: str) -> None:
        """Grant executor on tool_name to run_id (called on approval)."""
        async with OpenFgaClient(self._config) as client:
            await client.write(body=ClientWriteRequest(writes=[
                ClientTuple(user=f"run:{run_id}", relation="executor", object=f"tool:{tool_name}"),
            ]))

    async def revoke_tool(self, run_id: str, tool_name: str) -> None:
        """Revoke executor on tool_name from run_id (called on denial or run cleanup)."""
        async with OpenFgaClient(self._config) as client:
            await client.delete_tuples(body=[
                ClientTuple(user=f"run:{run_id}", relation="executor", object=f"tool:{tool_name}"),
            ])

    async def can_execute(self, run_id: str, tool_name: str) -> bool:
        """Return True if run_id holds executor on tool_name."""
        async with OpenFgaClient(self._config) as client:
            resp = await client.check(ClientCheckRequest(
                user=f"run:{run_id}",
                relation="executor",
                object=f"tool:{tool_name}",
            ))
            return bool(resp.allowed)
