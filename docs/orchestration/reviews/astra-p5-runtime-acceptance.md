# Astra acceptance: P5-01 pinned CPU VAD runtime

Accepted candidate `0a0c7119a5a3b536b45983310c466839d1f42e24`, based on `e23590475dfeea918bac23b05e24282aaf79191e`, integrated as `2440d6e760f43e5cc5fdb8f252dcfb25b3fd79c6`.

The package carries the pinned Silero v6.2.1 ONNX model and MIT license, discovers them through installed-package resources, verifies their bytes, and creates a CPU-only session with one inter-op and one intra-op thread. NumPy and ONNX Runtime remain lazy optional dependencies. Configuration pins have one owner; session revalidation checks actual bytes against the supplied artifact record rather than constructing another default configuration.

Both public entry points preserve contextual `VadUnavailable` errors, normalize other ordinary exceptions, and propagate `KeyboardInterrupt` and `SystemExit`. Tests cover missing/broken dependencies, corrupted resources, provider/tensor mismatches, resource discovery failures and session/metadata failures.

## Exact verification

- Candidate: `make check` passed 1,526 tests, Ruff (65 files), mypy (34 source files), all 19 invariant groups.
- Integration: `make check PY=.venv/bin/python` passed 1,616 tests, Ruff (67 files), mypy (35 source files), all 19 invariant groups.
- Root executed `/tmp/p5_iso_env6/bin/python -I` from `/tmp/p5_iso_cwd6`. Import resolved to `/private/tmp/p5_iso_env6/lib/python3.12/site-packages/vlogsrt/__init__.py`, outside the checkout.
- Installed `runtime.py` bytes equal the accepted Git blob exactly: SHA-256 `5653fcd28591ac9c1f8eadae54cd6ea4179f4be25fa716b04d90cfca98859133`.
- Model: 2,327,524 bytes, SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`; license: 1,075 bytes, SHA-256 `2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b`.
- Real CPU inference, not a fake session: float32 input `(1,576)`, float32 state `(2,1,128)`, int64 scalar 16000 returned finite probability `[[0.00166982]]` and finite state `(2,1,128)`. Provider and thread assertions passed.
- Sol exact approval: `msg_a09e4d0418dc`; Opus exact approval: `msg_ab4f07ea7573`.

P5-02 continuous channel-wise inference may start from this acceptance commit after the P5-01 worker reports completion and its terminal is accounted for. These checks validate runtime availability and packaging, not real-user speech detection quality or Final Cut roundtrip acceptance. Those external gates remain pending.

## Process cleanup

The user explicitly requested completed-process cleanup. Root matched completed Dispatch records to this project's terminals and closed 23 completed agent/idle-shell terminals through Orca, each with `ptyKilled=true` and absence confirmed in a fresh terminal list. Active managers and active implementation workers were preserved. Worktrees, branches and committed evidence were not deleted. Completed workers should now be closed after lifecycle settlement rather than left indefinitely at their prompts.
