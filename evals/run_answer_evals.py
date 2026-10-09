"""
Answer quality eval — LLM-as-judge on 10 short factual/practical questions.

Worker model:  claude-haiku-4-5-20251001  (Anthropic — the system being tested)
Judge model:   gpt-4o-mini                (OpenAI   — different provider, different model)

Usage:
    uv run python evals/run_answer_evals.py

Requires: ANTHROPIC_API_KEY and OPENAI_API_KEY in .env

Exit codes:
    0  all checks pass (or skipped due to missing keys)
    1  any judged case fails
"""

import asyncio
import sys

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import LLMJudge

from tack_ai.core.config import Settings

settings = Settings()

ANSWER_RUBRIC = (
    "The answer addresses the question directly. "
    "It is factually accurate with no hallucinated details. "
    "It is concise (no unnecessary padding), yet complete enough to be useful. "
    "Technical terms are used correctly."
)

ANSWER_DATASET: Dataset[str, str, None] = Dataset(
    name="answer_quality",
    cases=[
        Case(
            name="opa_purpose",
            inputs="What is Open Policy Agent and why is it used for AI agent authorization?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="hash_chain_tamper",
            inputs="How does a hash chain detect tampering in an audit trail?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="pydantic_ai_agent",
            inputs="What is Pydantic AI and how does it differ from LangChain?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="fail_closed_policy",
            inputs="What does 'fail-closed' mean in a policy engine?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="otel_traces",
            inputs="What is OpenTelemetry and what does a trace represent?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="rag_retrieval",
            inputs="Explain retrieval-augmented generation (RAG) in three sentences.",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="batch_vs_realtime",
            inputs="When should an AI agent use batch processing instead of realtime execution?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="token_budget",
            inputs="Why is tracking per-run token cost important in a production AI agent?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="require_approval",
            inputs="What is a human-in-the-loop approval gate in an AI agent harness?",
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
        Case(
            name="policy_vs_model",
            inputs=(
                "Why is it safer to enforce tool authorization in a policy engine like OPA "
                "rather than relying on the language model to self-police?"
            ),
            evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True)],
        ),
    ],
)


async def main() -> int:
    missing = []
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")

    if missing:
        print(f"Skipping answer evals: {', '.join(missing)} not set.")
        return 0

    worker: Agent[None, str] = Agent(
        AnthropicModel(
            "claude-haiku-4-5-20251001",
            provider=AnthropicProvider(api_key=settings.get_key("anthropic")),
        ),
        output_type=str,
        system_prompt="Answer the question concisely and accurately in 2–4 sentences.",
    )

    # Give the LLMJudge an explicit gpt-4o-mini instance so it uses our key.
    judge_model = OpenAIChatModel(
        "gpt-4o-mini",
        provider=OpenAIProvider(api_key=settings.get_key("openai")),
    )
    dataset = Dataset(
        name="answer_quality",
        cases=[
            Case(
                name=c.name,
                inputs=c.inputs,
                evaluators=[LLMJudge(rubric=ANSWER_RUBRIC, include_input=True, model=judge_model)],
            )
            for c in ANSWER_DATASET.cases
        ],
    )

    print("\n── Answer quality eval (worker: claude-haiku-4-5 / judge: gpt-4o-mini) ─")

    async def get_answer(question: str) -> str:
        result = await worker.run(question)
        return result.output

    report = await dataset.evaluate(get_answer, name="answer_quality", max_concurrency=3)
    report.print()

    avg = report.averages()
    if avg and avg.assertions is not None:
        pass_rate = float(avg.assertions)
        if pass_rate < 1.0:
            print(f"\nFAIL: {pass_rate:.0%} of answers passed the judge's rubric.")
            return 1
        print(f"\nPASS: {pass_rate:.0%} of answers passed the judge's rubric.")
    else:
        print("\nNo assertion averages available — check report output above.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
