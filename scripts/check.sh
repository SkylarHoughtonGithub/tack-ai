#!/usr/bin/env bash
# CI gate: policy tests + router evals.
# Run before merging any change to prompts, models, or policies.
#
# Usage:  bash scripts/check.sh
# Exit:   0 = all pass  |  non-zero = something failed

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0

_header() { echo; echo "━━━ $* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
_ok()     { echo "✓  $*"; PASS=$((PASS + 1)); }
_fail()   { echo "✗  $*"; FAIL=$((FAIL + 1)); }

# ── 1. OPA policy unit tests ──────────────────────────────────────────────────
_header "OPA policy tests"
if opa test policies/ -v; then
    _ok "All OPA tests passed"
else
    _fail "OPA tests failed"
fi

# ── 2. Router accuracy evals ──────────────────────────────────────────────────
_header "Router evals"
if uv run python evals/run_router_evals.py; then
    _ok "Router evals passed"
else
    _fail "Router evals failed (accuracy regression or error)"
fi

# ── 3. Answer quality evals (skipped when API keys are absent) ─────────────────
_header "Answer quality evals"
if uv run python evals/run_answer_evals.py; then
    _ok "Answer evals passed (or skipped — missing API keys)"
else
    _fail "Answer evals failed"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "━━━ Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Passed: $PASS   Failed: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    echo "  FAIL — fix the above before merging."
    exit 1
fi

echo "  PASS"
exit 0
