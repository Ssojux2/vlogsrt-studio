# Agent working policy

## User-directed delegation

- Delegate work with substantial implementation volume, repetitive edits, or bulk analysis to `claude-glm`.
- Use GPT-6 Sol with reasoning effort `high` for detailed plans, bounded task briefs, coordination, review, and independent acceptance tests.
- Claude-GLM is the product-code and implementation-test writer. Use the configured wrapper `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`; do not silently substitute plain Claude or another model.
- Large read-only investigations or document drafts may also be assigned to Claude-GLM. Sol verifies the evidence and owns the final plan and acceptance decision. Do not delegate away the independent tests required by the user.
- Break substantial changes into bounded tasks. Use Orca's actual Run/Task/Dispatch lifecycle. Keep one product-code writer at a time unless independence and file ownership are explicit.
- Use High Agency's outcome, bounded scope, evidence, and targeted verification principles. Have Claude-GLM read the applicable skill before a fresh task; verify actual use instead of treating message delivery as acknowledgment.

## Checkpoints and reporting

- After each accepted task, save what changed, commit/file references, test evidence, remaining issues, and the exact next action. Execute `/compact` in an idle worker terminal before reuse; never interrupt active work to compact.
- If an error cannot be resolved, report the failing stage, reproduction, expected/actual behavior, sanitized error and exit code, attempted fixes and their results, cause confidence, impact, and remaining dependency.
- Maintain `docs/reports/2026-09-27-high-agency-implementation-feedback.md` with product errors, tooling problems, skill-use evidence, and improvement proposals. Distinguish observations from causal conclusions.
- Summarize and close unused agents after their work settles. Keep active workers and unrelated user sessions intact.
- Do not read or modify `.env`, the user-owned `.env.example`, or credential files, including through shell searches. Preserve real API fixtures and original media without modification. Use explicit synthetic configuration for tests. Do not stage unrelated user changes.

The current approved implementation scope is in `docs/plans/2026-09-27-vlog-automation-stabilization.md`. Newer user instructions take precedence over historical workflow notes.
