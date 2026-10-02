# Task Routing

## Why Route?

Not every task needs the same model. A simple factual question ("What does HTTP 200 mean?") can be answered by a fast, cheap model. A complex research task ("Compare five chunking strategies across retrieval precision, recall, and latency") needs stronger reasoning. Routing dispatches tasks to the right model tier based on their complexity.

## Routing Tiers

The harness defines three tiers:

| Tier | Use case | Reasoning effort | Default model |
|---|---|---|---|
| `simple` | Factual questions, short lookups | low | gpt-4o-mini |
| `general` | Explanations, moderate tasks, code | medium | claude-sonnet-4-6 |
| `deep_reasoning` | Complex analysis, research, multi-step | high | claude-sonnet-4-6 |

## Execution Paths

Each route also specifies an execution path:

- **realtime** — the user is waiting; respond immediately.
- **batch** — the task is non-interactive; defer to the Anthropic Batch API (50% cheaper, async settlement).

The router sets `batch` for tasks over 80 words or clearly non-interactive. The batch path is noted in the audit trail.

## The Two Routers

### RuleBasedRouter

A safe fallback that always returns `general` with `medium` reasoning effort. No API calls, no cost, always available. Used when the LLM router is unavailable or the OpenAI key is not set.

Short questions like "do it" or "continue" can be arbitrarily complex, so text heuristics are unreliable. Defaulting to `general` is safer than guessing.

### LLMRouter

Uses `gpt-4o-mini` to classify the task. Costs approximately $0.0001 per route call. Returns a structured `Route` object with tier, reasoning effort, execution path, and a one-sentence explanation.

The LLM router is significantly more accurate than the rule-based router on the 40-case golden dataset: approximately 75% vs. 35% accuracy.

## Routing and the Budget Gate

Deep-reasoning runs estimated to cost over $2 USD require explicit human approval before executing. This is enforced by an OPA policy rule, not by the router itself. The separation ensures the gate cannot be bypassed by manipulating the routing decision.

## Fallback Chain

If the primary model for a tier fails, the harness tries the configured fallbacks in order. The fallback model is recorded in the audit trail.
