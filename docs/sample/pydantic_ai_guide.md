# Pydantic AI Guide

## What is Pydantic AI?

Pydantic AI is a Python framework for building production-grade AI agents. It wraps the model API call-and-tool-loop in a type-safe, Pydantic-validated layer. You define tools as Python functions, specify the output type as a Pydantic model, and Pydantic AI handles the conversation loop until the model produces a valid structured output.

## Defining an Agent

```python
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel

agent = Agent(
    AnthropicModel("claude-sonnet-4-6"),
    output_type=MyOutputModel,
    system_prompt="You are a helpful assistant.",
)
```

## Tools

Tools are Python functions decorated with `@agent.tool_plain` (no context) or `@agent.tool` (receives a `RunContext`). The model sees the docstring as the tool description and the type annotations as the parameter schema.

```python
@agent.tool_plain
async def web_search(query: str) -> str:
    """Search the web for current information."""
    return await do_search(query)
```

## Structured Output

The `output_type` parameter tells Pydantic AI what shape the final answer must be. The framework instructs the model to produce JSON matching the schema and validates the result. If validation fails, it retries.

```python
class ResearchAnswer(BaseModel):
    summary: str
    sources: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
```

## Multi-Provider Support

Pydantic AI supports Anthropic, OpenAI, Gemini, and local models through a unified interface. You can swap models without changing tool or output logic.

## FallbackModel

A `FallbackModel` tries the primary model first and falls back to alternatives in order if the primary fails:

```python
from pydantic_ai.models.fallback import FallbackModel

model = FallbackModel(primary_model, fallback_1, fallback_2)
```

## Prompt Caching

For long, repeated system prompts (>1024 tokens), Anthropic supports prompt caching. Enable it via model settings:

```python
from pydantic_ai.models.anthropic import AnthropicModelSettings

result = await agent.run(
    question,
    model_settings=AnthropicModelSettings(
        anthropic_cache_instructions=True,
        anthropic_cache_tool_definitions=True,
    ),
)
```

The cached prefix is stored for 5 minutes (default) or 1 hour. Cached tokens are billed at 10% of the normal input token rate, and cache read tokens at 10% too. Long conversations with a fixed system prompt see 60–90% reduction in input token costs.

## Pydantic AI vs LangChain

| Feature | Pydantic AI | LangChain |
|---|---|---|
| Type safety | First-class (Pydantic v2) | Optional |
| Output validation | Built-in, with retries | Manual |
| Abstraction level | Thin, explicit | Heavy, opinionated |
| Observability | OpenTelemetry native | Plugin-based |
| Learning curve | Low | High |

Pydantic AI is a better fit when you want type-safe structured output, minimal magic, and full control over the tool loop. LangChain is better when you need a large ecosystem of pre-built integrations.

## Pydantic Evals

The `pydantic-evals` package provides a golden-dataset evaluation framework:

```python
from pydantic_evals import Dataset, Case
from pydantic_evals.evaluators import EqualsExpected, LLMJudge

dataset = Dataset(name="my_eval", cases=[...], evaluators=[EqualsExpected()])
report = await dataset.evaluate(my_task_fn)
report.print()
```

`LLMJudge` grades free-text output against a rubric using a separate model as judge.
