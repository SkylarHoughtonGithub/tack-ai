from pydantic_settings import BaseSettings, SettingsConfigDict


_PROVIDER_KEY_INFO = [
    ("anthropic_api_key", "ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
    ("openai_api_key",    "OPENAI_API_KEY",    "https://platform.openai.com/api-keys"),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    logfire_token: str | None = None
    router_type: str = "llm"  # "llm" (default) or "rule_based" (fallback, no OpenAI key needed)
    task_budget_usd: float = 0.10

    # Phase 6 — RAG and memory
    database_url: str | None = None          # e.g. postgresql://tack_ai:tack_ai@localhost/tack_ai
    openfga_url: str = "http://localhost:8080"
    openfga_store_id: str | None = None
    openfga_model_id: str | None = None

    # Phase 7 — MCP
    mcp_gateway_url: str | None = None          # e.g. http://localhost:8082/sse
    mcp_filesystem_root: str | None = None      # root for filesystem MCP server (defaults to project root)

    # Phase 9 — durable execution
    temporal_host: str = "localhost:7233"        # Temporal gRPC endpoint

    def available_providers(self) -> list[str]:
        return [
            attr.replace("_api_key", "")
            for attr, _, _ in _PROVIDER_KEY_INFO
            if getattr(self, attr)
        ]

    def check_providers(self, required: list[str]) -> None:
        """Print all missing keys once and exit if any required ones are absent."""
        required_attrs = {f"{p}_api_key" for p in required}
        missing = [
            (attr, env_var, url)
            for attr, env_var, url in _PROVIDER_KEY_INFO
            if not getattr(self, attr)
        ]
        if not missing:
            return

        missing_required = [m for m in missing if m[0] in required_attrs]
        missing_optional = [m for m in missing if m[0] not in required_attrs]

        if missing_required:
            print("\nMissing required API key(s):")
            for _, env_var, url in missing_required:
                print(f"  {env_var}  →  {url}")

        if missing_optional:
            print("\nOptional API key(s) not set (needed for multi-provider routing):")
            for _, env_var, url in missing_optional:
                print(f"  {env_var}  →  {url}")

        if missing_required or missing_optional:
            print("\nAdd keys to your .env file and restart.\n")

        if missing_required:
            raise SystemExit(1)

    def get_key(self, provider: str) -> str:
        """Return the API key for a provider (call check_providers first)."""
        return getattr(self, f"{provider}_api_key")  # type: ignore[return-value]
