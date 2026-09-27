# STAB-S1A — Sol independent acceptance evidence

Decision: PENDING IMPLEMENTATION AND VALIDATION. This document does not claim S1 or runtime acceptance.

## Scope and authority
- Approved plan: `docs/plans/2026-09-27-vlog-automation-stabilization.md`.
- Brief: `docs/orchestration/tasks/stab-s1a-entrypoint-cancellation.md`.
- Code baseline: `0d91da1`; parent S1A brief: `abbed48`; Sol integration addendum: `bd18b35`.
- GLM owns all product/test implementation; Sol has edited only briefs/checkpoint/review documentation.
- Nested Run `run_5426631698bf`, Task `task_713f37f60836`, Dispatch `ctx_e1c3232736f7`.
- Exact custom launcher requested: `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`; terminal readiness satisfied and worker-start turn start observed. Adopted-terminal Orca metadata reports model null, so model selection evidence is the actual launcher command, not a fabricated effective-model receipt.

## Independent static findings sent to GLM
1. Fresh bundle entrypoint copies staged media grant before workspace creation (parent reproduction).
2. Root cancellation is copied once; running engine checks workspace only, losing later app cancellation.
3. App cancellation reads App Group workspace revision even when development engine writes external workspace; all-zero fallback may be ignored.
4. Installed development wrapper is a separate flow and omits staged media grant; bundle-helper-only tests cannot prove development behavior.
5. Per-job delegate gates cannot prevent later chunks, rejected-parameter retries or transient retries within the same delegate call; SDK-internal retry configuration also requires scrutiny.
6. Broad exception adapters in transcription/repair/boundary can convert cancellation into an ordinary error/empty result unless propagation is explicit.

## Independent execution evidence
- Initial draft check: `.venv/bin/python -m pytest -q tests/project/test_engine_entrypoint.py tests/project/test_workflow_cancellation.py tests/project/test_development_engine_installer.py tests/project/test_workflow_analysis.py tests/project/test_workflow_protocol.py` — exit 0; log `/tmp/stab-s1a-sol-targeted-initial.log`. Run independently by Sol, output redirected without a pipe so the command exit is preserved.
- This check supports current entrypoint/delegate-level behavior only. At this snapshot, `LiveTranscriber` inner-call gates and the actual app external revision path are unchanged. Passing the selected tests does not close those requirements.
- Correction packet prepared: `docs/orchestration/tasks/stab-s1a-r1-integration-corrections.md`. No second writer dispatched while original GLM remains active. Final acceptance, Swift and full regression evidence remain pending.

## External acceptance and task boundary
No paid STT/vision APIs, actual Final Cut operation, original-media processing, binary supply or distributable signing is authorized/performed here. Those do not prevent offline S1A implementation and validation. Per updated user instruction, after S1A is accepted Sol records a <=60-line checkpoint, settles child ownership and the parent Dispatch, then idles for parent-issued compact and a fresh S1B Dispatch; no active worker/ask is compacted.
