import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field
from pydantic_ai import Agent
import genai_prices
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from tack_ai.config import Settings
from tack_ai.router import RuleBasedRouter, LLMRouter, Route, ExecutionPath, load_model_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = PROJECT_ROOT / "logs"


class ResearchAnswer(BaseModel):
    summary: str
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


settings = Settings()
settings.check_providers(required=["anthropic"])

# Base agent — model is overridden per-run based on routing decision
agent: Agent[None, ResearchAnswer] = Agent(
    AnthropicModel(
        "claude-sonnet-4-6",
        provider=AnthropicProvider(api_key=settings.get_key("anthropic")),
    ),
    output_type=ResearchAnswer,
    system_prompt=(
        "You are a research assistant. Use your tools to answer questions. "
        "Always cite sources. Be concise."
    ),
)


@agent.tool_plain
def web_search(query: str) -> str:
    """Search the web for current information on a topic."""
    # Stub — replaced with real search in Phase 7
    return (
        f"[stub] Search results for '{query}':\n"
        "1. Example result A — example.com\n"
        "2. Example result B — example.org"
    )


@agent.tool_plain
def read_file(filename: str) -> str:
    """Read a file from the project folder. Only files within the project are accessible."""
    target = (PROJECT_ROOT / filename).resolve()
    if not str(target).startswith(str(PROJECT_ROOT)):
        return "Error: access outside the project folder is not allowed."
    if not target.exists():
        return f"Error: file '{filename}' not found."
    return target.read_text()


def _build_model(model_str: str):
    provider, model_name = model_str.split(":", 1)
    if provider == "anthropic":
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(api_key=settings.get_key("anthropic")),
        )
    if provider == "openai":
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(api_key=settings.get_key("openai")),
        )
    raise ValueError(f"Unknown provider: {provider}")


def _build_model_with_fallback(route: Route, model_config: dict):
    tier = route.tier.value
    primary_str = model_config["tiers"][tier]
    fallback_strs = model_config.get("fallbacks", {}).get(tier, [])
    primary = _build_model(primary_str)
    fallbacks = [_build_model(s) for s in fallback_strs]
    if not fallbacks:
        return primary, primary_str
    return FallbackModel(primary, *fallbacks), primary_str


def _estimate_cost(usage, model_str: str) -> float:
    model_name = model_str.split(":", 1)[1]
    try:
        calc = genai_prices.calc_price(usage, model_name)
        return float(calc.total_price)
    except Exception:
        # fall back to Sonnet rates if model not in genai-prices yet
        return (
            (usage.input_tokens or 0) * 3.0
            + (usage.output_tokens or 0) * 15.0
        ) / 1_000_000


def _log_route(question: str, route: Route, model_str: str, cost_usd: float) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question[:200],
        "tier": route.tier.value,
        "reasoning_effort": route.reasoning_effort.value,
        "execution_path": route.execution_path.value,
        "model": model_str,
        "reason": route.reason,
        "cost_usd": cost_usd,
    }
    with open(LOGS_DIR / "routing.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


async def run(question: str) -> None:
    model_config = load_model_config()

    # 1. Route — LLM router by default, rule-based fallback if unavailable
    if settings.router_type == "llm" and settings.openai_api_key:
        try:
            route = await LLMRouter(openai_api_key=settings.get_key("openai")).route(question)
        except Exception as e:
            print(f"LLM router unavailable ({e.__class__.__name__}: {e}), falling back to rule-based.")
            route = RuleBasedRouter().route(question)
    else:
        route = RuleBasedRouter().route(question)

    model, model_str = _build_model_with_fallback(route, model_config)

    print(f"\nQuestion: {question}")
    print(f"Route:    {route.tier.value} | {route.reasoning_effort.value} effort | "
          f"{route.execution_path.value} | {route.reason}")
    print(f"Model:    {model_str}")
    if route.execution_path == ExecutionPath.batch:
        print("          (batch path noted — executing realtime; full batch API in Phase 9)")

    # 2. Run (pydantic-ai will raise UsageLimitExceeded if output tokens overflow budget)
    result = await agent.run(question, model=model)

    # 3. Cost via genai-prices (live pricing data, no manual table)
    cost = _estimate_cost(result.usage, model_str)
    _log_route(question, route, model_str, cost)

    if cost > settings.task_budget_usd:
        print(f"\nWarning: run cost ${cost:.5f} exceeded budget ${settings.task_budget_usd:.5f}")

    # 4. Output
    print("\n=== Tool-call loop ===")
    for msg in result.all_messages():
        print(f"  [{msg.__class__.__name__}]")
        for part in msg.parts:
            print(f"    {part.__class__.__name__}: {str(part)[:120]}")

    print("\n=== Answer ===")
    print(f"Summary:    {result.output.summary}")
    print(f"Sources:    {result.output.sources}")
    print(f"Confidence: {result.output.confidence}")

    print("\n=== Usage ===")
    print(f"Input tokens:   {result.usage.input_tokens}")
    print(f"Output tokens:  {result.usage.output_tokens}")
    print(f"Cache read:     {result.usage.cache_read_tokens}")
    print(f"Estimated cost: ${cost:.5f}  (budget: ${settings.task_budget_usd:.2f})")


if __name__ == "__main__":
    asyncio.run(run("What is the Model Context Protocol and why does it matter?"))
