# Skylab Platform Rework — tack-ai integration

Oct 12, 2026 · @Skylar

> tack-ai is pre-existing; this file captures only the integration work needed
> to make it the governed remediation agent in Skylab. The surrounding plan
> lives in `skylab-gitops/plan.md` and its siblings.

## Role in Skylab

| Layer | Tool | Owns | Never touches |
| --- | --- | --- | --- |
| Remediation | tack-ai | Proposing fixes as pull requests, behind policy and approval | Direct cluster changes |

HolmesGPT investigates and tack-ai writes the fix as a pull request; neither touches the cluster directly. Policy decides each fix: low-risk changes merge on their own, risky ones wait for a human, and denied ones are only logged.

tack-ai's chart lands in **skylab-gitops wave 7** under `charts/ai/`. This file lists the work needed on tack-ai itself.

## Integration work

- **Output is a PR, not an action.** Fixes land as PRs to Gitea `platform/tenants` (or skylab-gitops for platform issues) through tack-ai's connector, so Argo CD applies every change and Git holds the history.
- **Policy context:** feed Cedar and OPA the target environment, the entity owner from Backstage, the blast radius and the estimated cost. Example rule set: a replica bump in dev is allowed, anything in prod requires approval, and anything touching AppProjects or RBAC is denied.
- **Backstage as a tool:** use the MCP actions for ownership, SLO burn and recent sync failures, to decide scope and who approves. _MCP actions are defined in `skylab-backstage/plan.md`._
- **Telemetry:** point its existing OpenTelemetry output at Alloy and build one Grafana dashboard covering decisions, approval latency, token cost by model and PR merge rate.
- **Packaging:** deploy it with the `ai-agent` KubeVela component (defined in `skylab-gitops/charts/kubevela-defs`), so it becomes the reference workload for the AI agent template in Backstage.

## Done when

- [ ] A synthetic alert (for example, a crash-looping deployment) produces a Holmes summary, a tack-ai PR and a policy decision
- [ ] A dev fix merges automatically; a prod fix waits for approval
- [ ] Every step appears in Grafana and the audit log
- [ ] The README shows the run as a short recording or screenshot sequence
