# Agent working policy

## User-directed execution

- Latest user instruction (2026-10-07): GPT-6 Astra performs all planning, product code, implementation tests, debugging, and acceptance verification. This supersedes the historical Claude-GLM / Sol / Luna split.
- Use GPT-6 Astra `high` or `xhigh` according to difficulty. Verify requested runtime model and effort where exposed; distinguish requested settings from served-model attestation. Do not silently substitute another model. Delegation changes only the worker model, not the root model.
- Work as one Astra writer, in bounded tasks, and verify implementation in a separate acceptance pass. No additional model workers are required. Existing unrelated sessions remain untouched.
- Use High Agency's outcome, bounded scope, evidence, and targeted verification principles. Read the applicable skill before a fresh task and record actual use.

## Checkpoints and reporting

- After each accepted task, save what changed, commit/file references, test evidence, remaining issues, and the exact next action. Execute `/compact` in an idle worker terminal before reuse; never interrupt active work to compact.
- If an error cannot be resolved, report the failing stage, reproduction, expected/actual behavior, sanitized error and exit code, attempted fixes and their results, cause confidence, impact, and remaining dependency.
- Maintain `docs/reports/2026-09-27-high-agency-implementation-feedback.md` with product errors, tooling problems, skill-use evidence, and improvement proposals. Distinguish observations from causal conclusions.
- Summarize and close unused agents after their work settles. Keep active workers and unrelated user sessions intact.
- Do not read or modify `.env`, the user-owned `.env.example`, or credential files, including through shell searches. Preserve real API fixtures and original media without modification. Use explicit synthetic configuration for tests. Do not stage unrelated user changes.

The current approved implementation scope is in `docs/plans/2026-09-27-vlog-automation-stabilization.md`. Newer user instructions take precedence over historical workflow notes.
