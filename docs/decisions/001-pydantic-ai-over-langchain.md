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

Use **Pydantic AI** as the primary framework. LangGraph appears only in Phase 11
as a standalone comparison module that answers a specific tradeoff question
(explicit state machines vs. implicit agent loops).

## Rationale

- **Depth over breadth:** Pydantic AI exposes the mechanics of the agent loop
  (message history, tool dispatch, streaming) without hiding them behind
  high-level abstractions. Learning at this level produces transferable knowledge;
  knowing LangChain's DSL does not.
- **Type safety:** Tools are typed Python functions; the structured output is a
  Pydantic model. Mypy can check the entire path from tool args to agent result.
- **Resume value:** A demonstrable ability to build agents from the underlying
  primitives is more differentiated on a CV than "used LangChain".
- **Prompt caching:** Pydantic AI exposes `AnthropicModelSettings` so the system
  prompt (long, repeated on every call) hits the cache transparently.

## Consequences

- LangGraph's explicit graph structure (useful for branching multi-agent flows)
  is not available in the main path. The Phase 11 module shows how it would look.
- Pydantic AI is younger than LangChain; some features (e.g. batch API
  integration) required manual wiring.
- Any Pydantic AI upgrade is an explicit dependency bump, not a transitive
  framework pull.
