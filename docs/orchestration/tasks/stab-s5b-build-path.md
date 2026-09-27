# STAB-S5B — Verifiable bundled-engine and FFmpeg build path

Read [the common execution contract](stab-execution-contract.md) before edits; it fixes the required High Agency, mailbox, protected-file and verification workflow.

## Scope
GLM completes local app packaging/build tooling and tests after S5A; Sol updates README/board/acceptance docs after independent validation. Inspect `scripts/build_engine.py`, `scripts/verify_ffmpeg.py`, macOS project resources/launchers and engine supply tests. Product/config/scripts/tests may be edited only as needed; no dependency or binary download/install, signing credential access or deployment.

Impact: propagating | files<=14 | modules<=4 | boundary=shared-api

## Required behavior
1. Provide a reproducible local path that packages the engine entrypoint and required Python/VAD resources, verified FFmpeg/ffprobe and app/extension resources. Both development and bundled modes implement all approved operations, preflight, environment/tool resolution and result paths.
2. Verify architecture, executable permissions, required resources, manifest hashes, tool supply metadata and signatures at the correct stage. Detect missing/mismatched binaries/resources with precise failure, not a fake successful bundle. Do not overwrite user-provided supply metadata or substitute system binaries silently.
3. Keep signing modes explicit: local verification/ad-hoc where already supported vs distributable signing/notarization with external identity. No claim of distributable/real-FCP readiness without those receipts. A unit test mocking codesign is not signing evidence.
4. Exercise packaging order/manifest/launch contracts with synthetic staged trees and fake tools in tests; where installed tools allow, run an offline development entrypoint smoke and macOS build without real FCP/API calls. Do not fetch unavailable binary inputs or stop other offline implementation because they are absent.
5. Before completion provide an evidence matrix: implemented path, automated pass/fail, installed-tool build result, and pending external binary/signing/FCP/API acceptance. Sol integrates that into docs; GLM must report exact commands/exits and missing prerequisites.

## Acceptance
Synthetic supplied/missing/invalid tool/resource cases cover the actual build orchestration. Required app operations resolve in development and bundled plans. Run all Python/Swift/static/invariant checks, `scripts/build_engine.py --verify`, FFmpeg verifier and supported local app build commands; record failures truthfully. Commit explicit paths only, no binaries/secrets/protected env/real fixtures/media, push or deployment. Send one worker_done when all feasible implementation/tests are complete, with external acceptance named.
