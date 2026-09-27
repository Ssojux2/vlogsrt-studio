# Stabilization worker execution contract

Applies to every STAB implementation task. The approved plan, current live Dispatch and latest AGENTS.md routing remain authoritative. Sol writes briefs/docs and independently tests/reviews; Claude-GLM alone writes product/tests. Use one writer in the current checkout with `claude-glm --model 'glm-5.3[1m]' --effort <task-appropriate-effort> --permission-mode auto`. Choose and record supported effort at the next task boundary; never interrupt an active task just to change it. STAB-TEST-ISOLATION retained its explicitly requested max launcher. Easy work uses Luna max, complex planning/acceptance Sol high; parent owns idle model selection for compact and must distinguish client selection from hidden compaction-model evidence.

## Before first code mutation
1. Read the live preamble and consume same-Dispatch follow-ups with its exact `orca orchestration check` command.
2. Read this task's brief, relevant contracts, and `/Users/ssojux2/.codex/plugins/cache/high-agency/high-agency/0.9.0/skills/high-agency-coding/SKILL.md` directly. Report the actual read and a compact immutable Impact estimate.
3. State the bounded behavior to prove and run a meaningful failing regression when a defect is reproducible. Once the target path, failing behavior and verification are known, implement; additional discovery must resolve a concrete uncertainty.
4. Do not read `.env`, `.env.example`, secret wrappers or key files, even with filtered grep. Use public configuration code and synthetic settings. No real API fixture/original media modification, paid/live APIs, FCP, push or deployment.

## During work
- Keep scope to the current task. No additional agents, manager layers or model swaps.
- Shared-checkout mitigation: Sol prepares briefs before GLM launch and keeps in-flight review notes/logs outside tracked work. During GLM's final verification/settlement, defer shared docs/checkpoint mutations until the worker's final turn is idle/closed; this avoids invalidating the High Agency whole-tree snapshot. Do not modify the installed plugin to bypass its guard.
- At each natural file/test checkpoint and immediately before settlement, consume pending follow-ups; acknowledge material scope changes explicitly through Orca.
- Read full message bodies; do not truncate them with grep/head or string slices. Process every returned message, reply where needed, then run `orca orchestration check --terminal <your live handle> --ack <the exact returned deliveryId> --json`. Process and acknowledge any new batch the same way. A heartbeat/status summary does not acknowledge a Delivery; without `--ack`, FIFO replay hides later guidance.
- Keep five-minute heartbeats outside blocking ask/check waits. A successful send is not proof of recipient acknowledgment.
- After edits, verify the touched behavior first, then affected/full gates where shared contracts require them. Preserve real command exits using redirection or pipefail; a trailing `tail` exit is not the test exit.
- One final `make check` satisfies the overlapping full Python/Ruff/mypy/invariant requirements in individual briefs. Do not run those broad checks separately and immediately repeat them through `make check` without a new edit/failure/concern. Add Swift and macOS script checks not covered by Makefile separately. Format first, then run final gates once.
- The test-only [dotenv isolation prerequisite](stab-test-isolation.md) is accepted at `01f8616`; preserve it on every later test run. Offline/network blocking alone does not suppress production dotenv lookup. New CLI subprocess tests must use the guarded launch setup and prove cwd, copied-package provenance and test-owned configuration before a broad run. Do not disable conftest for a full-file RED: use a fully synthetic isolated harness with no real fallback. Do not probe real protected files or inspect unsafe raw logs to investigate prior access; earlier numerical results retain their access uncertainty.
- On failure, record repro, expected/actual, sanitized error/exit, attempted change, fresh result and cause confidence. Never weaken assertions/thresholds or add skips merely to pass.

## Completion
- Commit explicit product/test paths only; do not stage user-owned or Sol documentation files.
- Report commit SHA/files, exact commands/exits, skill-read/application evidence, scope drift, unresolved errors and external acceptance.
- Send worker_done exactly once using both live lifecycle IDs and a three-sentence summary; then idle. Sol decides acceptance and corrections.
- Compact only after accepted settlement at the user-requested task boundary. Summarize/checkpoint first. Unused settled workers are released and, where external ownership remains, the exact proven idle terminal may be closed under the user's cleanup instruction.
