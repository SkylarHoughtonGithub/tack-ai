# run_code loop — diagnosis & fix plan

## Diagnosis

The SSE log shows `run_code` completing in ~200 ms every iteration. That timing
means `subprocess.run()` raises `FileNotFoundError` immediately — the docker
binary is never found, no process is started. Despite the "Do not retry"
directive in the error string, the model (gpt-4o-mini / haiku in fallback mode)
ignores it and calls the tool again. The `approved_tools` short-circuit in
`_web_request_approval` lets every call through without an approval prompt, so
the loop runs silently until the `tool_calls_limit=15` guard fires.

Two root causes must both be fixed:

---

## Fix 1 — Find docker reliably

`shutil.which("docker", path=...)` at call time with an extended PATH is still
returning `None`, which means docker is not at `/opt/homebrew/bin/docker` or
`/usr/local/bin/docker`.

On macOS with Docker Desktop, the binary is typically at one of:
- `/Applications/Docker.app/Contents/Resources/bin/docker`
- `~/.docker/bin/docker`
- `/usr/local/bin/docker` (symlink created by Docker Desktop)

**Action:** probe all four locations in order; surface the resolved path in the
error message so the user can see exactly what was tried.

```python
_DOCKER_CANDIDATES = [
    "/opt/homebrew/bin/docker",
    "/usr/local/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    str(Path.home() / ".docker/bin/docker"),
]

def _find_docker() -> str | None:
    for p in _DOCKER_CANDIDATES:
        if Path(p).exists():
            return p
    return shutil.which("docker")  # last-ditch system PATH
```

Call `_find_docker()` at the top of `_run_in_docker`. If it returns `None`,
return the full list of tried paths in the error string and include "Do not
retry run_code".

---

## Fix 2 — Mechanical loop prevention

The model cannot be relied on to obey "Do not retry" text. We need a hard stop
in the approval layer.

**Action:** remove `run_code` from the `approved_tools` remembered set so every
call requires explicit user approval. High-risk tools (code execution,
send_email) should always prompt; only low-risk tools (web_search,
search_documents) earn the permanent-per-run grant.

In `web.py` `approve_action`:

```python
HIGH_RISK_TOOLS = {"run_code", "send_email", "delete_file"}

async def approve_action(run_id, approval_id, ...):
    ...
    if tool_name not in HIGH_RISK_TOOLS:
        run_state.approved_tools.add(tool_name)  # only remember low-risk tools
    ...
```

This means each `run_code` call shows the approval banner with the code
visible. The user can deny if it's the same broken code, breaking the loop
immediately instead of waiting for the usage-limit guard.

---

## Files to change

| File | Change |
|------|--------|
| `src/tack_ai/agent.py` | Replace `_run_in_docker` path resolution with `_find_docker()` probing all four candidates; log the resolved path to stderr |
| `src/tack_ai/web.py` | Add `HIGH_RISK_TOOLS` set; skip `approved_tools.add()` for those tools in `approve_action` |

---

## Quick test after fix

1. Submit a task: "run print('hello')"
2. Approve → should print "hello" and succeed
3. Submit a task that uses a bad docker state → approval banner shows each retry
   instead of looping silently
