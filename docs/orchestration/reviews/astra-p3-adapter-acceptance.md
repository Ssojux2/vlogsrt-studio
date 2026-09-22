# Astra acceptance: P3-02 source transcription adapter

Accepted candidate `5723ed5d6a5cf1b657509a500f5883ca3f0c3671`, based on `81fe02e0fb373665c9a3fd5ae36d2c964f7edd7a`, integrated as `0a9854e8bbf5cd8842fb2af386b422fde7358780` on `feat/final-cut-project`.

The adapter reuses the legacy repair/refine engine over exact PCM sample slices. It returns one SOURCE trace per analysis key and one unavailable record per resource in first-seen order. Analysis identity includes PCM identity and source configuration; durable SOURCE SRT publication uses an immutable analysis-keyed generation. Identical publication is idempotent, while conflicting bytes preserve the existing artifact and return an error.

Review corrections now enforce real WAV byte limits including headers, per-range duration caps, contextual rejection of sub-sample caps, literal-empty versus filtered-empty outcomes, audio-disposition preflight before calls, and completed-stage evidence preservation on failure. An error keeps the draft, settled spine, repairs and refined cues only for stages actually completed, with no successful SRT reference.

## Verification

- Candidate: 1,472 tests, Ruff (63 files), mypy (32 source files), all 19 invariant groups passed.
- Integration: `make check PY=.venv/bin/python` passed 1,569 tests, Ruff (64 files), mypy (33 source files), all 19 invariant groups.
- Root failure injection: refinement failure retained 3 draft and 3 settled records with no refined cues; publication failure retained all 3 refined cues as well. Both remained errors without an SRT reference, and repairs matched actual settle output.
- Opus independently checked actual WAV sizes, empty-state distinctions, configuration-dependent keys, exclusive publication, complete preflight and unique outputs. Its final delta review confirmed publication-conflict provenance and early-failure absence of invented evidence.
- Sol exact approval: `msg_b6b634e66d91`; Opus exact approval: `msg_8129cbf64d44`.
- Only the four assigned product/test paths were integrated; the user-owned `.env.example` was not read, edited or staged.

## Continuation

P3-03 may start from this acceptance commit after the P3-02 worker reports completion and its terminal is accounted for. VAD remains an independent active branch. Actual Final Cut import/re-export, real user-media checks and paid STT/vision remain `PENDING_HUMAN`; these offline checks do not satisfy them.

The resumed Sol manager is task `task_98e0a278d0fe` / dispatch `ctx_15eda062f5ec`, bound to existing child Run `run_8f41d4a91c5d`. Opus is task `task_1816b1e90bd0` / dispatch `ctx_4309d1be0a61`, with child Run `run_b6d40d052690`. Root brokers cross-Run reviews and retains merge authority.
