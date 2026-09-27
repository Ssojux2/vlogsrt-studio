# STAB-S3A — Approved dialogue and B-roll candidate pipeline

Read [the common execution contract](stab-execution-contract.md) before edits; it fixes the required High Agency, mailbox, protected-file and verification workflow.

## Scope
After S2 acceptance, GLM integrates candidate production through the approved `project analyze -> review -> roughcut` path, reusing `runner.py`, `review.py`, `edit.py`, speech/VAD/vision protection and block logic. Change Python workflow/protocol and compatible Swift candidate decoding plus tests. Sol owns docs and review. Read the approved plan; current checkout, one GLM writer.

Impact: propagating | files<=18 | modules<=5 | boundary=shared-api

## Required decisions
1. Candidate kinds are silence, NG/retake and B-roll compression. Every candidate starts unselected; analysis never exports automatic deletions. Preserve existing minimums, handles and confidence thresholds, original speech input coverage, immutable subtitle spine and exact rational frame grids.
2. Distinguish successful no-speech/no-audio sources from transcription/VAD/vision failures. A confirmed no-speech or declared-no-audio video may produce B-roll candidates; unavailable/corrupt/failed analysis must preserve its affected interval and show failure status. Do not fabricate success to unlock locked guards.
3. Reuse legacy block partition/protection logic to enumerate safe B-roll windows, including whole no-speech clips and safe leading/trailing regions. Do not merely change `_internal_gaps` to treat all unprotected intervals as removable silence. Speech and scene protection still bind proposals.
4. Supply preceding/following scene context around candidates, with explicit distinction between removable span and context-only frames. Preserve unique events, actions, narrative transitions, uncertain evidence and motion continuity; no mandatory deletion ratio. Validate every model-selected span against enumerated candidate identity and bounds.
5. A B-roll compression choosing a retained middle segment can require deletions on both sides. Represent those as one atomic group with a stable group ID, shared evidence/decision revision and explicit member spans. Group approval selects all required members or none; the union must be safe, nonoverlapping and frame-aligned. Do not convert a model keep into a cut.
6. NG/retake proposals must retain unique utterances. Reuse existing retext/repair evidence without changing the legacy engine or dropping transcription input. Unresolved cues protect their spans. Silence, NG and B-roll proposals may overlap as alternatives; persist explicit conflicts for review and reject conflicting approvals.
7. Extend Python/Swift protocol (kind/group/conflicts) with conservative legacy decoding and coherent digests/versions. Persist representative evidence and transcript context for S3B. All partial/error paths obey S1 preservation and S2 call budgets/cache identity.

## Acceptance
- Synthetic integrated command analysis yields each kind, a successful no-speech whole-clip B-roll proposal and safe edge proposals; failed transcription/VAD/vision yields no deletion in its interval.
- Protect unique speech/event/action, uncertainty, motion boundary and handles; demonstrate no forced compression ratio.
- Atomic two-sided group review rejects half approval and conflicts; zero approvals export identity timing, valid approvals remove exactly the union of approved frames.
- App-engine candidate JSONL roundtrip decodes all kinds and legacy candidates. Run affected Python/Swift tests, Ruff/mypy and `make check`; commit explicit paths, report SHA/exits.

## Boundaries
Product/tests only; no env/secrets, real API fixtures, original media, live APIs/FCP, push or deployment. No unsupported threshold tuning. Sol will validate and dispatch corrections before S3B.
