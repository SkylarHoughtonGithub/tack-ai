import tomllib
from enum import Enum
from pathlib import Path
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "models.toml"


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
    """Uses gpt-4o-mini to classify the task — costs ~$0.0001 per route."""

    def __init__(self, openai_api_key: str) -> None:
        model = OpenAIChatModel(
            "gpt-4o-mini",
            provider=OpenAIProvider(api_key=openai_api_key),
        )
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
