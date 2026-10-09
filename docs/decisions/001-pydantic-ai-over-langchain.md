# ADR-001: Use Pydantic AI as the primary agent framework

**Status:** Accepted  
**Date:** 2025-10

---

## Context

The agent loop needs: structured output (typed results), tool call dispatch, streaming
support, model fallback, and prompt caching. Several frameworks could provide this.
LangChain and LangGraph are the most widely deployed; Pydantic AI is newer and
narrower in scope.

## Decision

Use **Pydantic AI** as the primary framework throughout, including `pydantic_graph`
for the coding workflow. A `langgraph_approval.py` stub is retained for reference
but is not in the main execution path.

## Rationale

- **Type safety:** Tools are typed Python functions; the structured output is a
  Pydantic model. Mypy can check the entire path from tool args to agent result.
- **Integrated ecosystem:** `pydantic_graph`, `pydantic-evals`, and the Capabilities
  framework are first-class parts of the same library — no impedance mismatch.
- **Multi-provider:** Native support for Anthropic and OpenAI with identical tool
  interfaces; provider-specific settings applied at routing time.
- **Prompt caching:** Built-in cache tracking across providers via `result.usage`.

## Consequences

- `pydantic_graph` handles graph-structured workflows (Plan → Edit → Test → Evaluate)
  without a separate LangGraph dependency.
- Any Pydantic AI upgrade is an explicit dependency bump, not a transitive
  framework pull.
