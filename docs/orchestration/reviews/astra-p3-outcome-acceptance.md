# P3-01 transcription outcome acceptance

Accepted GLM candidate `df2d3fda28efdfe95524c36d735464e11af24e16`, integrated as `46ad02b32fd2f201dabf7ad1466ae28aa8518fa1` on 2026-09-08 UTC. P3-02 may now consume the reviewed outcome and fixture contracts; P3 as a whole is not yet complete.

The additive outcome separates successful, explicitly empty, and failed transcription while preserving the legacy segment builder. It rejects malformed requests before delegating and requires exact response coverage, finite numeric timing, and explicit string segment text. Valid nonempty responses filtered to no usable text remain successful with no segments; `require_nonempty` rejects that unusable draft without treating it as evidence of silence.

Namespaced fixtures require raw chunk identities before coercion, select the exact requested subset, and validate stem/kind as filename components before reading. Unicode, spaces, and ordinary dots remain valid. The legacy `namespace=None` loader remains unchanged. This is scoped filename validation, not a filesystem sandbox.

Root independently reproduced eight invalid request cases with zero delegate calls, six malformed text cases, two valid blank-text drafts, fourteen unsafe namespace components, and three valid Unicode/dotted/option-shaped names. All passed after the three initial findings were corrected. Sol approved the exact repository SHA in `msg_7299fa6b672d`; Opus independently approved in `msg_65c6fe3cda2f`. Mistyped full hashes in earlier status messages were corrected against Git before integration.

Root candidate `make check` passed **1,352 tests**, Ruff (60 files), mypy (31 modules), and 19 invariant groups. Integrated `make check` passed **1,382 tests**, Ruff (61 files), mypy (31 modules), and all 19 invariant groups; the extra 30 tests come from the previously integrated P1 roundtrip lane. No paid transcription or fixture recording was used.

P3-02/P4-03 briefs at `578afb82b9d5658badf5559a454ff77daeb0a5a0` explicitly distinguish segment jobs from text-only arbiter/verify jobs. Their private capture wrapper must not feed valid text-only answers into the public segment-oriented outcome validator. P5-00 configuration remains an authorized independent parallel lane.
