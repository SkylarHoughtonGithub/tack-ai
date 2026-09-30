import asyncio
from pathlib import Path
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from tack_ai.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ResearchAnswer(BaseModel):
    summary: str
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


settings = Settings()
settings.check_providers(required=["anthropic"])

_model = AnthropicModel(
    "claude-sonnet-4-6",
    provider=AnthropicProvider(api_key=settings.get_key("anthropic")),
)

agent: Agent[None, ResearchAnswer] = Agent(
    _model,
    output_type=ResearchAnswer,
    system_prompt=(
        "You are a research assistant. Use your tools to answer questions. "
        "Always cite sources. Be concise."
    ),
)


@agent.tool_plain
def web_search(query: str) -> str:
    """Search the web for current information on a topic."""
    # Stub — returns fake results until a real search API is wired in Phase 7
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


async def run(question: str) -> None:
    print(f"\nQuestion: {question}\n")

    result = await agent.run(question)

    print("=== Tool-call loop ===")
    for msg in result.all_messages():
        print(f"  [{msg.__class__.__name__}]")
        for part in msg.parts:
            print(f"    {part.__class__.__name__}: {str(part)[:120]}")

    print("\n=== Answer ===")
    print(f"Summary:    {result.output.summary}")
    print(f"Sources:    {result.output.sources}")
    print(f"Confidence: {result.output.confidence}")

    usage = result.usage
    print("\n=== Usage ===")
    print(f"Input tokens:   {usage.input_tokens}")
    print(f"Output tokens:  {usage.output_tokens}")
    print(f"Cache read:     {usage.cache_read_tokens}")
    print(f"Total tokens:   {usage.total_tokens}")
    # claude-sonnet-4-6: $3/M input, $15/M output, $0.30/M cache read
    cost = (
        (usage.input_tokens or 0) * 3
        + (usage.output_tokens or 0) * 15
        + (usage.cache_read_tokens or 0) * 0.30
    ) / 1_000_000
    print(f"Estimated cost: ${cost:.5f}")


if __name__ == "__main__":
    asyncio.run(run("What is the Model Context Protocol and why does it matter?"))
