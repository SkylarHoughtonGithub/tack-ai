# Open Policy Agent (OPA) Guide

## What is OPA?

Open Policy Agent (OPA) is an open-source, general-purpose policy engine that decouples policy decisions from application code. Instead of scattering authorization logic across your codebase, you write policies in a declarative language called Rego and ask OPA to evaluate them at runtime.

OPA answers questions of the form: "Is this action allowed given this context?" It returns structured decisions — allow, deny, or a custom result — that your application enforces.

## Rego Basics

Rego is a logic-based query language. Policies are sets of rules. A rule evaluates to true if all its conditions hold. The default decision applies when no explicit rule matches.

```rego
package myapp

default allow := false

allow if {
    input.method == "GET"
    input.user.role == "admin"
}
```

The `input` document contains whatever context your application passes to OPA. The `data` document contains static or externally loaded policy data.

## Fail-Closed Design

A critical property of a safe policy engine is fail-closed: anything not explicitly permitted is denied. In OPA, you achieve this with a default rule:

```rego
default decision := "deny"
```

With this in place, a new tool or action that has no matching allow rule will be denied automatically. This prevents privilege escalation through omission.

## OPA in an AI Agent Harness

In an AI agent harness, OPA sits between the model and every tool call:

1. The model proposes a tool call with arguments.
2. The harness sends the tool name and arguments to OPA.
3. OPA evaluates the Rego policy and returns `allow`, `deny`, or `require_approval`.
4. For `require_approval`, a human is prompted before the call proceeds.

This keeps authorization deterministic and outside the model. A model can be persuaded through clever prompting; an OPA policy cannot.

## Policy Versioning

The policy version is embedded in the Rego file:

```rego
policy_version := "1.0.0"
```

Every audit record includes the policy version that governed it. This means you can reconstruct the exact rules that were in effect for any historical action.

## Testing Rego Policies

OPA has a built-in test runner. Test rules are prefixed with `test_`:

```rego
test_web_search_allowed if {
    decision == "allow" with input as {"tool_name": "web_search", "args": {}}
}
```

Run tests with: `opa test policies/ -v`

Every rule should have at least one test. Unknown tools should always be denied.
