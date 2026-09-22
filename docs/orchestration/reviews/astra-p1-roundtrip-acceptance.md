# P1 identity roundtrip acceptance

Accepted GLM candidate `5f8885ad7c3b82d02b97b2dc350465a38708b0dd`, integrated as `d5a60e03a8f41603d9a2be9ebcd7373b3f156d04` on 2026-09-08 UTC. P1 and P2 are now complete for the approved offline scope.

`write_identity` atomically copies original XML bytes through a same-directory temporary file. The caller supplies an existing destination parent. `validate_roundtrip` delegates to P1 XML validation, reparses the copy, compares semantic fields independently of hash-scoped IDs, and rejects byte inequality even when semantics match. Mismatches produce bounded invalid verdicts; tool/DTD unavailability propagates separately. This is identity-copy validation, not the future edited XML writer.

Root independently constructed eight actual-DTD-valid inputs, including CRLF, Korean text, static metadata, nonzero asset/clip/project epochs, repeated resources, gaps, and disabled/video-only clips. All eight retained byte identity; modified semantic records and changed XML bytes rejected; an injected replacement failure preserved both the previous destination and original input. The installed DTD SHA-256 was verified as `33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5`.

Root reviewed the product delta and passed the worker and integrated `make check`: **1,304 tests**, Ruff (61 files), mypy (31 modules), and **19 invariant groups**. Sol approved exact SHA in `msg_ad8b174db4bc`; Opus independently approved it in `msg_3e2406079b5f`. No blocking findings remain in P1.

Actual Final Cut import/re-export, relink, and playback remain `PENDING_HUMAN`. P3-01 runs independently on legacy transcription/pipeline files from `1fe07e5`; P3-02 still waits for that reviewed integration. P5-00 configuration may occupy the independent second coding slot from this P1 integration without consuming unfinished P3 interfaces.
