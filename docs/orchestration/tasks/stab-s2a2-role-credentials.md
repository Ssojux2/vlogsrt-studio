# S2A-2 — separate role credentials (pre-dispatch refinement note)

Status: authorized by parent answer `msg_9aa93afe98b7` to canonical question `msg_7515ead490ec`; **not yet dispatched**. Finish this bounded brief after the requested parent compact/resume, before launching one fresh GLM writer. Foundation `f56545b` is accepted; its Task/Dispatch is settled and must not be reused. Sol's overall `task_35ab3e395efb` / `ctx_9dcc7d74611c` remains active.

Read AGENTS.md, [common contract](stab-execution-contract.md), [S2A-2 requirements](stab-s2a-2-credentials-preflight.md), and [foundation evidence](../reviews/stab-s2a2-unit1-sol.md). Use the exact `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto` launcher, actual readiness/adoption, direct High Agency read and an immutable estimate. One writer; Sol owns docs/independent acceptance. Expected ceiling to refine: 18 owned code/test/config paths across six credential/launch/app/diagnostic/test surfaces; propose a bounded split before exceeding it.

## Authorized decisions

- The app must support separately labeled STT and vision credentials through fake-testable storage/UI, preserving the existing engine account/service and adding a distinct vision account. Default provider contracts remain `OPENAI_API_KEY` for STT and `GOOGLE_API_KEY` for vision. Never alias one secret to both or silently select a different provider.
- A versioned, backward-compatible private paired carrier is authorized. Retain V1/single-key and markerless/direct CLI behavior. Whitelist logical fields, reject duplicates/unknown fields/wrong types/NUL/oversized data with payload-free errors, and bound the total encoded payload. Preserve the accepted framing/descriptor lifecycle and explicit-empty semantics.
- Replace or clear each managed role independently without reviving inherited/dotenv values. Do not mutate parent-global environment, place secrets in argv/files/defaults/JSONL, or change the other role when one stored credential is replaced/cleared. Storage operations in tests remain injected fakes.
- Map vision credentials through the existing configured credential-name contract (default Google); preserve selected model/base URL behavior. Resolve any reserved-name/collision ambiguity explicitly before mutation instead of accidentally overwriting tool environment or the STT role. Preserve legacy direct CLI configuration behavior.
- Redact both managed secrets across the actual error paths, including provider normalization/persistent diagnostics. Preserve nonsecret failure evidence and retry/cancellation behavior.
- Key-free estimation and roughcut stay available. Final paid preflight must check credentials required by configured operations/stages before any paid call; do not claim full preflight completion in this role unit.

## Required refinement before delegation

`vlogsrt.project.commands._vision_credentials` requires both the configured key and a nonempty configured base URL, default `GEMINI_BASE_URL`; it deliberately blocks fallback that would send a Google key to the public OpenAI endpoint. Paired keys alone do not make bundled setup usable. Specify an explicit, provider-safe app configuration/launch path for that endpoint, validate that it cannot smuggle credential material into a public setting, and preserve direct CLI defaults; never inspect a real dotenv/config credential or guess/silently switch endpoints. Decide its exact field/storage/transport contract before dispatch, keeping this unit bounded or splitting it explicitly.

## Acceptance and boundaries to retain

- Fake storage: independent save/replace/clear/missing/error status per role, no submitted/saved-value echo, existing account/service preserved.
- Both real launcher paths through the copied actual entrypoint and actual provider factories with fake clients: correct role-specific replacement, explicit absence, no cross-role contamination, correct selected endpoint, V1/direct compatibility, and both-key error redaction. Compare factory-captured values by synthetic digests; persist booleans only. Preserve actual carrier application; substitute only downstream analysis.
- Reuse the accepted same-interpreter guard and synthetic public-source copies. Any new child path gets read-only source approval, fail-closed startup/refusal and exact cwd/import proof before execution. Use explicit literal clean environments and external owned-process-group deadlines, capture original exits. No ambient discovery, real Keychain, protected files, incident raw logs, paid calls, original media/API fixtures, FCP, signing, push or deployment.
- Preserve S1/S2A-1 ownership/cancellation/drift/diagnostic/import protections. No progress/budget/import or general media-preflight expansion here.
- Targeted checks first; reuse valid foundation evidence. One final aggregate `make check` remains for final S2A-2 preflight acceptance, with Swift/scripts/native checks as affected. Explicit-path commit only, valid settlement, exact idle cleanup, and checkpoint at the accepted unit boundary.
- Read full mailbox bodies and ACK exact delivery IDs including ACK-returned batches; never pipe mailbox JSON through head. Wait with `check --wait --timeout-ms 55000`, not fixed long sleeps.
