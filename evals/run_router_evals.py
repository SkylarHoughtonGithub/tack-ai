"""
Router accuracy eval — compares RuleBasedRouter and LLMRouter on 40 golden cases.

Usage:
    uv run python evals/run_router_evals.py

Exit codes:
    0  all checks pass
    1  LLMRouter accuracy is below the regression threshold (60 %)
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pydantic_evals import Dataset
from pydantic_evals.evaluators import EqualsExpected

from router_dataset import ROUTER_DATASET
from tack_ai.core.config import Settings
from tack_ai.core.router import LLMRouter, RuleBasedRouter, RouteTier

# Minimum acceptable accuracy for LLMRouter — a regression below this exits 1.
LLM_ROUTER_MIN_ACCURACY = 0.60

dataset: Dataset[str, RouteTier, None] = Dataset(
    name=ROUTER_DATASET.name,
    cases=list(ROUTER_DATASET.cases),
    evaluators=[EqualsExpected()],
)

settings = Settings()


def _accuracy(report) -> float:
    avg = report.averages()
    if avg is None:
        return 0.0
    return float(avg.assertions) if avg.assertions is not None else 0.0


async def main() -> int:
    n = len(dataset.cases)
    tier_counts = {t: sum(1 for c in dataset.cases if c.expected_output == t) for t in RouteTier}
    print(f"\nDataset: {n} cases  "
          f"({tier_counts[RouteTier.simple]} simple / "
          f"{tier_counts[RouteTier.general]} general / "
          f"{tier_counts[RouteTier.deep_reasoning]} deep_reasoning)")

    # ── Rule-based router ────────────────────────────────────────────────────
    print("\n── RuleBasedRouter ─────────────────────────────────────────────────────")
    rule_router = RuleBasedRouter()

    def rule_task(question: str) -> RouteTier:
        return rule_router.route(question).tier

    rule_report = await dataset.evaluate(rule_task, name="RuleBasedRouter")
    rule_report.print()
    rule_acc = _accuracy(rule_report)
    print(f"Accuracy: {rule_acc:.0%}  "
          f"(expected ~{tier_counts[RouteTier.general] / n:.0%} — "
          f"always returns 'general')")

    # ── LLM router ───────────────────────────────────────────────────────────
    if not settings.openai_api_key:
        print("\n── LLMRouter ───────────────────────────────────────────────────────────")
        print("Skipped: OPENAI_API_KEY not set.\n")
        return 0

    print("\n── LLMRouter ───────────────────────────────────────────────────────────")
    llm_router = LLMRouter(openai_api_key=settings.get_key("openai"))
    t0 = time.perf_counter()

    async def llm_task(question: str) -> RouteTier:
        return (await llm_router.route(question)).tier

    llm_report = await dataset.evaluate(llm_task, name="LLMRouter", max_concurrency=5)
    elapsed = time.perf_counter() - t0
    llm_report.print()
    llm_acc = _accuracy(llm_report)

    # gpt-4o-mini: ~$0.15/1M input + $0.60/1M output; routing prompt ≈ 300 in / 60 out
    est_cost = n * (300 * 0.15 + 60 * 0.60) / 1_000_000
    print(f"Accuracy: {llm_acc:.0%}")
    print(f"Elapsed:  {elapsed:.1f}s  |  Est. cost: ${est_cost:.4f} "
          f"({n} × ~300 in / ~60 out tokens @ gpt-4o-mini pricing)")

    if llm_acc < LLM_ROUTER_MIN_ACCURACY:
        print(f"\nFAIL: LLMRouter accuracy {llm_acc:.0%} is below threshold {LLM_ROUTER_MIN_ACCURACY:.0%}")
        return 1

    print(f"\nPASS: LLMRouter accuracy {llm_acc:.0%} ≥ threshold {LLM_ROUTER_MIN_ACCURACY:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
