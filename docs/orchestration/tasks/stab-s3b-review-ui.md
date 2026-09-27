# STAB-S3B — Evidence-rich review and atomic approval

## Scope and requirements
GLM implements Swift extension/app/core candidate review and Python preview/result production as needed after S3A; Sol owns docs/review. Read the approved plan and S3A schema. Current checkout, product/tests only, one writer.

Impact: propagating | files<=12 | modules<=4 | boundary=shared-api

1. Show candidate kind, reason, timeline start/end, exact deletion duration, relevant transcript and representative image. Show group total deletion and all member spans. A missing preview is explicit, never a broken image treated as evidence.
2. Preview image identity/hash is bound to candidate/input revision and confined to the job artifacts. Render the actual sampled image, not an invented placeholder. Move-playhead action uses exact candidate timeline start and existing timeline bridge.
3. All candidates/groups begin unchecked. Group checkbox approves both required B-roll deletions atomically. Show overlapping alternatives/conflicts, prevent contradictory selection, and preserve current selection only for unchanged candidate/decision revision.
4. Approval serialization is authoritative: validate groups/conflicts again before writing approved IDs; Python review still rejects malformed requests. No UI-only safety gate. Partial analysis warnings remain visible alongside available safe candidates.
5. Long lists remain usable through scrolling, accessible labels and concise per-row details. UI exposes product meaning, not internal hashes/JSON plumbing unless needed for troubleshooting.

## Acceptance and settlement
Swift tests exercise the real review model/controller serialization for unchecked defaults, atomic groups, conflicts, refresh identity and playhead coordinates; Python tests verify the emitted approval produces exact expected timing. Use synthetic local preview images, no real media. Run Swift suite, affected Python tests and required static checks; commit explicit paths and report SHA/commands/exits. Real FCP visual acceptance remains pending. No protected env/fixture access, live calls, push or deployment.
