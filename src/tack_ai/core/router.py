import tomllib
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_ai import Agent

if TYPE_CHECKING:
    from tack_ai.core.config import Settings

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.toml"


class RouteTier(str, Enum):
    simple = "simple"
    general = "general"
    deep_reasoning = "deep_reasoning"


class ReasoningEffort(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ExecutionPath(str, Enum):
    realtime = "realtime"
    batch = "batch"


class Route(BaseModel):
    tier: RouteTier
    reasoning_effort: ReasoningEffort
    execution_path: ExecutionPath
    reason: str


def build_model(model_str: str, settings: "Settings") -> Any:
    """Construct a PydanticAI model object from a 'provider:name' string."""
    provider, model_name = model_str.split(":", 1)
    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(api_key=settings.get_key("anthropic")),
        )
    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(api_key=settings.get_key("openai")),
        )
    if provider == "typesafe":
        from pydantic_ai.models.typesafe import TypeSafeModel
        from pydantic_ai.providers.typesafe import TypeSafeProvider

        return TypeSafeModel(
            model_name,
            provider=TypeSafeProvider(api_key=settings.get_key("typesafe")),
        )
    raise ValueError(f"Unknown provider '{provider}' in model string '{model_str}'")


def make_run_settings(model_str: str) -> Any | None:
    """Return provider-appropriate ModelSettings for a run, or None if not needed."""
    provider = model_str.split(":", 1)[0]
    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        return AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
        )
    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModelSettings

        # 24h extended retention; parallel tool calls are on by default in the OpenAI API.
        return OpenAIChatModelSettings(openai_prompt_cache_retention="24h")
    # typesafe: no special settings needed — Jev has no caching knobs exposed here.
    return None


def load_model_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


class RuleBasedRouter:
    """Safe fallback used only when the LLM router is unavailable.

    Short questions can be arbitrarily complex ("do it", "continue") so no
    text heuristic is reliable. Rather than guess wrong, this router defaults
    to general tier and lets the worker model handle whatever complexity exists.
    """

    def route(self, question: str) -> Route:
        return Route(
            tier=RouteTier.general,
            reasoning_effort=ReasoningEffort.medium,
            execution_path=ExecutionPath.realtime,
            reason="Fallback router — defaulting to general (LLM router unavailable).",
        )


class LLMRouter:
    """Classifies tasks using the model configured at router.decision in models.toml."""

    def __init__(self, model_str: str, settings: "Settings") -> None:
        model = build_model(model_str, settings)
        self._agent: Agent[None, Route] = Agent(
            model,
            output_type=Route,
            system_prompt=(
                "Classify the user's question into a routing decision.\n"
                "tier: simple (factual, short answer), general (moderate complexity), "
                "deep_reasoning (complex analysis, research, multi-step reasoning).\n"
                "reasoning_effort: low for simple, medium for general, high for deep_reasoning.\n"
                "execution_path: batch if the task is non-interactive and over 80 words or bulk; "
                "realtime otherwise.\n"
                "reason: one sentence explaining your choice."
            ),
        )

    async def route(self, question: str) -> Route:
        result = await self._agent.run(question)
        return result.output
