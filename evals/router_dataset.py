from pydantic_evals import Case, Dataset

from tack_ai.core.router import RouteTier

ROUTER_DATASET: Dataset[str, RouteTier, None] = Dataset(
    name="router",
    cases=[
        # ── Simple — factual, one-liner answers ──────────────────────────────
        Case(name="capital_france",    inputs="What is the capital of France?",                     expected_output=RouteTier.simple),
        Case(name="python_keyword",    inputs="Is 'yield' a reserved keyword in Python?",           expected_output=RouteTier.simple),
        Case(name="http_200",          inputs="What does HTTP status code 200 mean?",               expected_output=RouteTier.simple),
        Case(name="json_null",         inputs="How do you represent null in JSON?",                 expected_output=RouteTier.simple),
        Case(name="pi_digits",         inputs="What are the first five digits of pi?",              expected_output=RouteTier.simple),
        Case(name="tcp_port_443",      inputs="Which port does HTTPS use by default?",              expected_output=RouteTier.simple),
        Case(name="utc_meaning",       inputs="What does UTC stand for?",                           expected_output=RouteTier.simple),
        Case(name="git_pull",          inputs="What does 'git pull' do?",                           expected_output=RouteTier.simple),
        Case(name="base64_encryption", inputs="Is Base64 an encryption algorithm?",                 expected_output=RouteTier.simple),
        Case(name="jwt_acronym",       inputs="What does JWT stand for?",                           expected_output=RouteTier.simple),
        Case(name="docker_image",      inputs="What is a Docker image in one sentence?",            expected_output=RouteTier.simple),
        Case(name="iso8601",           inputs="What does ISO 8601 define?",                         expected_output=RouteTier.simple),
        Case(name="range_type",        inputs="What type does Python's range() return?",            expected_output=RouteTier.simple),

        # ── General — explanations, moderate tasks ────────────────────────────
        Case(name="explain_opa",       inputs="Explain how Open Policy Agent works.",               expected_output=RouteTier.general),
        Case(name="pydantic_validate", inputs="How does Pydantic validate data at runtime?",        expected_output=RouteTier.general),
        Case(name="async_python",      inputs="Explain async/await in Python with a short example.", expected_output=RouteTier.general),
        Case(name="hash_chain",        inputs="What is a hash chain and how does it detect tampering?", expected_output=RouteTier.general),
        Case(name="rego_deny_rule",    inputs="Write a Rego rule that denies all tool calls by default.", expected_output=RouteTier.general),
        Case(name="fastapi_post",      inputs="How do you add a POST endpoint in FastAPI?",         expected_output=RouteTier.general),
        Case(name="jwt_vs_session",    inputs="Compare JWT tokens with session cookies.",           expected_output=RouteTier.general),
        Case(name="docker_vs_vm",      inputs="What are the main differences between Docker containers and virtual machines?", expected_output=RouteTier.general),
        Case(name="llm_temperature",   inputs="What does the temperature parameter control in LLM sampling?", expected_output=RouteTier.general),
        Case(name="git_rebase_merge",  inputs="When should you use 'git rebase' vs 'git merge'?",  expected_output=RouteTier.general),
        Case(name="audit_properties",  inputs="What properties does a tamper-evident audit trail need?", expected_output=RouteTier.general),
        Case(name="write_pytest",      inputs="Write a pytest test for a function that adds two numbers.", expected_output=RouteTier.general),
        Case(name="otel_vs_logs",      inputs="How do OpenTelemetry traces differ from plain logs?", expected_output=RouteTier.general),
        Case(name="policy_decisions",  inputs="What is the difference between allow, deny, and require_approval in OPA?", expected_output=RouteTier.general),

        # ── Deep reasoning — complex analysis, multi-step ─────────────────────
        Case(
            name="agent_framework_compare",
            inputs=(
                "Compare Pydantic AI, LangChain, LangGraph, and CrewAI across five dimensions: "
                "ease of use, flexibility, observability, production-readiness, and community support. "
                "Give a recommendation for a research assistant use case."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="rego_budget_policy",
            inputs=(
                "Design a complete OPA policy in Rego for an AI agent harness that enforces per-user "
                "daily budget limits, rate limits, and model tier restrictions. Include the data model and all rules."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="rag_architecture",
            inputs=(
                "Design a retrieval-augmented generation architecture for a private document assistant "
                "that handles multi-tenant access control, chunking strategy, re-ranking, and context-window management."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="security_threat_model",
            inputs=(
                "Build a threat model for an AI agent that can read files, execute code, and send emails. "
                "Identify the top five attack vectors and the defensive controls for each."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="audit_compliance",
            inputs=(
                "Analyze what an append-only hash-chained audit trail must prove for SOC 2 Type II compliance. "
                "What fields are required, what verification procedures are needed, and what are the gaps in a typical implementation?"
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="router_eval_design",
            inputs=(
                "You have two AI task routers: a rule-based one that always returns 'general' and an LLM-based "
                "one using gpt-4o-mini. Design a methodology for evaluating router accuracy, including dataset "
                "construction, metrics, and statistical significance testing."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="pydantic_ai_vs_langgraph",
            inputs=(
                "You must decide whether to use Pydantic AI or LangGraph for a multi-step document analysis "
                "pipeline that requires approval gates, audit logging, and cost tracking. Write a detailed "
                "technical decision with pros, cons, and implementation risks for each option."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="context_window_strategy",
            inputs=(
                "Design a context window management strategy for an AI research assistant that must handle "
                "conversations lasting hours, reference documents up to 100k tokens, and maintain coherence "
                "across 50+ tool calls without losing critical context."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="durable_execution_design",
            inputs=(
                "Design a durable execution system for an AI agent that must survive process crashes, "
                "network failures, and API timeouts while ensuring exactly-once semantics for irreversible "
                "actions like sending emails."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="federated_auth_opa",
            inputs=(
                "Design a federated authorization system combining OPA with an external identity provider "
                "(OIDC) for a multi-tenant AI agent platform. Include token validation, attribute mapping, "
                "and policy structure."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="embedding_chunking",
            inputs=(
                "Compare five text chunking strategies (fixed-size, sentence, paragraph, semantic, recursive) "
                "for a retrieval system over technical documentation. Analyze the tradeoffs in retrieval "
                "precision, recall, and latency."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="red_team_ai_agent",
            inputs=(
                "Conduct a red-team analysis of an AI agent that can web-search, read files, execute code, "
                "and send emails. Design 10 adversarial prompts, explain the attack vectors, and propose "
                "mitigations for each."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
        Case(
            name="eval_methodology",
            inputs=(
                "Design a comprehensive evaluation methodology for an AI agent that routes tasks to different "
                "models, including metrics for routing accuracy, answer quality, cost efficiency, and latency. "
                "Specify the dataset requirements and statistical tests needed."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),

        # ── Retrieval questions (Phase 6) ─────────────────────────────────────
        Case(name="opa_fail_closed",    inputs="What does fail-closed mean in OPA?",                  expected_output=RouteTier.simple),
        Case(name="hash_chain_purpose", inputs="What does a hash chain prove about an audit trail?",  expected_output=RouteTier.simple),
        Case(name="prompt_caching",     inputs="How does Anthropic prompt caching reduce costs?",     expected_output=RouteTier.general),
        Case(name="rag_auth_filter",    inputs="How should a RAG system filter results by user permissions?", expected_output=RouteTier.general),
        Case(
            name="chunking_strategy",
            inputs=(
                "Compare fixed-size and heading-based chunking strategies for a retrieval system over "
                "technical documentation. When would you choose one over the other?"
            ),
            expected_output=RouteTier.general,
        ),
        Case(
            name="rag_full_design",
            inputs=(
                "Design a complete RAG pipeline for a private knowledge base with OpenFGA authorization. "
                "Cover document ingestion from multiple sources (local files and HTTP), chunking, embedding, "
                "permission-filtered retrieval, and conversation memory with summarization."
            ),
            expected_output=RouteTier.deep_reasoning,
        ),
    ],
)
