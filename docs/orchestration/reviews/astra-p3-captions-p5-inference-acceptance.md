# Astra acceptance: SOURCE placement and channelwise VAD

Accepted on 2026-09-10 KST after recovering the stalled coordinator handoff.

| Task | Exact candidate | Integration commit |
|---|---|---|
| P3-03 SOURCE captions | `d1c3205f99895a756b912146fee715fc73f07498` | `6d5155d5e828907dd6c6aba2320e4d4ce216529e` |
| P5-02 VAD inference | `a578018130835126a0d7686a4be0769b9f5765f4` | `31bac0b518fac6393458b0afbe54fd1402622fff` |

SOURCE placement preserves source cue identity, text, provenance and exact rational time. It joins unavailable audio by resource before sharing content analysis, adds the measured PCM origin once, and maps into each authored instance. Boundary clamps and millisecond-collapsed placements retain explicit evidence; invalid zero-millisecond rows are omitted from the draft SRT without discarding the underlying placement.

VAD reads verified stored PCM separately per channel, normalizes int16 samples to float32, carries 64-sample context and recurrent state through 512-sample windows, and resets at each channel/asset. Tail padding is excluded from reported sample ranges. Source identity mismatch, malformed output, nonfinite probability/state and runtime failures produce an asset error without partial successful windows.

## Verification

- Root reran candidate `make check`: P3 1,605 tests, Ruff 66 files, mypy 34 files, all 19 invariant groups; P5 1,645 tests, Ruff 69 files, mypy 36 files, all 19 groups.
- Root's P5 integration passed 1,645 tests and the complete gate.
- Root's combined integration passed **1,681 tests**, Ruff 71 files, mypy 37 files, all 19 invariant groups.
- Root independently tested 24 exact SOURCE clock combinations: all eight supported frame rates, positive/zero/negative PCM origins, nonzero XML epochs, unchanged source traces.
- P5 inference's 29 focused tests passed with actual NumPy from `/tmp/p5_iso_env6`, using the candidate checkout through explicit `PYTHONPATH`. The base dev environment may use the test's fake array implementation; its full gate alone is not real NumPy evidence.
- A separate root smoke used actual NumPy and the pinned CPU ONNX session with 1,031 synthetic phase-opposed stereo samples. Both channels produced `[0,512)`, `[512,1024)`, `[1024,1031)`, finite probabilities, and identical results on a second inference call, confirming reset and real-tail accounting.
- Sol exact reviews: `msg_9da22200a479` (P3), `msg_2c35f860885e` (P5). Opus corrected final verdict `msg_e7422ef19aa4`: both candidates and docs `c438a850804ba9b09a27fe115e375c738a4cc606` PASS.

## Review disposition

Opus's proposed repeated-ID blocker was withdrawn after reproduction showed correct behavior: repeated placements intentionally share a source cue ID, but retain separate instance IDs and flags. Only the collapsed placement is omitted. `unresolved_cue_ids` summarizes flagged placements; downstream code must derive active failures from those placements, not use the summary as an emission blacklist. Root independently reproduced the same scenario. No schema expansion or product fix was required.

The P4–P7 brief corrections in `c438a85` are accepted: explicit resource identity and asset epoch, authored audibility separate from analysis availability, typed unavailable-instance propagation, and a real PCM reference for guard projection. Finite VAD values remain unmodified at inference; threshold policy belongs downstream.

## Coordination recovery

The previous Sol manager settled as failed while waiting for root-only integration. Root resumed and started manager task `task_88db49bbe723`, dispatch `ctx_7d23138a62b5`, terminal `term_6e6d1137-3111-4ff6-b49c-7ae7dc6d53a7`; it recovered child Run `run_8f41d4a91c5d` without duplicate coding work.

Completed coding workers must now report their bounded deliverable and stop polling while root reviews preserved commits. Manager/root own the review wait. Release settled workers immediately; close confirmed completed external terminals under the user's cleanup authorization. Preserve branches, worktrees and evidence. Send root reports to `run:run_73768f7c4dec`, not a worker's own Dispatch mailbox.

P4-01 and the independent P5-04 task may start from this acceptance commit after their predecessor workers settle and are cleaned up. Actual Final Cut import/re-export, original-media relink/playback and real-user speech/removal audits remain pending; no paid API or real-media acceptance is claimed.
