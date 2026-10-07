"""
OpenFGA authorization for per-run tool grants, and UserManager for web auth.
"""

from __future__ import annotations

import bcrypt
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
        async with OpenFgaClient(self._config) as client:
            await client.write(body=ClientWriteRequest(writes=[
                ClientTuple(user=f"run:{run_id}", relation="executor", object=f"tool:{tool_name}"),
            ]))

    async def revoke_tool(self, run_id: str, tool_name: str) -> None:
        async with OpenFgaClient(self._config) as client:
            await client.delete_tuples(body=[
                ClientTuple(user=f"run:{run_id}", relation="executor", object=f"tool:{tool_name}"),
            ])

    async def can_execute(self, run_id: str, tool_name: str) -> bool:
        async with OpenFgaClient(self._config) as client:
            resp = await client.check(ClientCheckRequest(
                user=f"run:{run_id}",
                relation="executor",
                object=f"tool:{tool_name}",
            ))
            return bool(resp.allowed)


class UserManager:
    """
    DB-backed user store with env-var admin fallback.

    If DATABASE_URL is not set, only the single env-var admin account works.
    When the DB is available, user records are read from the `users` table
    and the env-var admin continues to work as a bootstrap account.
    """

    def __init__(self, db_url: str | None, admin_username: str, admin_password: str) -> None:
        self._db_url = db_url
        self._env_admin = admin_username
        # Pre-hash once at startup so login checks don't generate a new salt.
        self._env_hash: bytes = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt())

    async def authenticate(self, username: str, password: str) -> str | None:
        """Return the user's role on success, or None on failure."""
        pw = password.encode()
        if self._db_url:
            try:
                row = await self._fetch_user(username)
                if row and bcrypt.checkpw(pw, row["password_hash"].encode()):
                    return str(row["role"])
            except Exception:
                pass
        if username == self._env_admin and bcrypt.checkpw(pw, self._env_hash):
            return "admin"
        return None

    async def create_user(self, username: str, password: str, role: str = "viewer") -> None:
        if not self._db_url:
            raise RuntimeError("DATABASE_URL is required for user management")
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        import psycopg  # noqa: PLC0415
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            await conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (username, pw_hash, role),
            )
            await conn.commit()

    async def list_users(self) -> list[dict]:
        if not self._db_url:
            return [{"username": self._env_admin, "role": "admin", "source": "env"}]
        try:
            import psycopg  # noqa: PLC0415
            from psycopg.rows import dict_row  # noqa: PLC0415
            async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT username, role, created_at FROM users ORDER BY created_at"
                    )
                    rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def delete_user(self, username: str) -> None:
        if username == self._env_admin:
            raise ValueError("Cannot delete the env-var admin account")
        if not self._db_url:
            raise RuntimeError("DATABASE_URL is required for user management")
        import psycopg  # noqa: PLC0415
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            await conn.execute("DELETE FROM users WHERE username = %s", (username,))
            await conn.commit()

    async def _fetch_user(self, username: str) -> dict | None:
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415
        async with await psycopg.AsyncConnection.connect(self._db_url) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT username, password_hash, role FROM users WHERE username = %s",
                    (username,),
                )
                return await cur.fetchone()
