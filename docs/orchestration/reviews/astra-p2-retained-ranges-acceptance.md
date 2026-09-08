# P2 retained ranges acceptance

Accepted candidate `7ac575b3da0518c3ebd1b795acc63261f9c52541`, integrated as `3760cf9148abfb03db2166f3665c904403ce702b` on 2026-09-08 UTC. P2 is complete for the approved offline scope.

`split_retained_ranges` converts retained decoded-audio-local intervals to inward-snapped sample ranges, validates range ordering before writes, and emits exact PCM WAV slices. Whole, intermediate, final, and repair intervals share the same sample clock. Deterministic `range-NNNN.wav` leaves no longer interpolate unrestricted source stems.

Root rejected initial `eaf155b` after reproducing `../outside` writing outside the requested directory. The corrected delta passed root's independent five-stem containment probe, including absolute, relative traversal, separator, Korean/spaces, and option-like stems; decoded WAV bytes matched the retained source sample span. All 126 PCM tests passed independently. Sol and Opus approved the exact final SHA; Opus approval `msg_afbcf3a2f00b` supersedes its withdrawn initial pass.

Integration `make check` passed: Ruff (58 files), mypy (30 modules), **1,250 tests**, and **19 invariant groups**. Existing P2 publication behavior was not reopened or changed.

Evidence limits: synthetic legacy segment-coordinate mismatch is reproduced; the historical 77-minute incident's cause, magnitude, and final-versus-intermediate distinction remain `PENDING_HUMAN`. Invalid range inputs produce no partial writes. Each slice is published atomically, but an I/O failure on a later slice can leave earlier slices; there is no batch transaction guarantee.
