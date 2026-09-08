# Sol design disposition: assets with no audio

Date: 2026-09-09  
Status: proposed for Astra approval before shared-brief edits

## Problem

`AssetResource.has_audio` already records the XML declaration, but the drafted downstream interfaces assume that every resolved asset has a `PcmReference` and `SourceTrace`. An ordinary video-only resource must remain addressable for XML identity/export without inventing a PCM origin, calling STT/VAD, or relabeling the absence as a successful empty transcript. One such resource must not fail analysis of unrelated audio resources.

## Proposed minimal contract

1. Add `has_audio: bool` to `ResolvedMedia` in P1-03 and copy it from the exact matched `AssetResource`. This keeps the disposition attached to the resource even when two XML resources resolve to the same content bytes.
2. Keep `PcmReference` and `SourceTrace` total and audio-only. Do **not** make their hashes, sample counts, PCM origins, coverage, or transcript fields optional; do not construct either record when `ResolvedMedia.has_audio` is false.
3. Add one explicit P3-02 absence record rather than overloading `empty` or `error`:

   ```python
   @dataclass(frozen=True, slots=True)
   class NoAudioSource:
       resource_scoped_id: str
       source_id: str
       original_sha256: str
       status: Literal["no_audio"]
       reason: str

   SourceAnalysis = SourceTrace | NoAudioSource
   ```

   `transcribe_source` stays audio-only. Change `transcribe_unique_sources` to accept the full resolved-media sequence plus the available PCM-reference sequence, join by strong IDs/hashes, return a `NoAudioSource` for every `has_audio=False` resource, and transcribe/deduplicate only `has_audio=True` resources. A resource that declares audio but has no selected stream/reference is an `error`, not `no_audio`.
4. P3-03 accepts `Sequence[SourceAnalysis]`. For an audio-capable resource it still requires one unambiguous `SourceTrace` and the measured origin, including successful-empty traces. For a no-audio resource it requires the matching `NoAudioSource` by `resource_scoped_id`, emits no cues, and carries the explicit keep/review disposition; it never fabricates an origin.
5. P4-01 defines audibility as `media.has_audio and clip.enabled and clip.src_enable == "all"`. Video-only slices remain addressable with `audible=False`, preserve instances/blades, create no boundary requests, and cannot authorize automatic shortening.
6. P5 VAD consumes only real `PcmReference` values. A no-audio source produces no VAD call/result; its P4 slices are already inaudible and therefore yield no eligible P6 block. P6 records the resource/instances as keep-all review, not an analysis success.
7. P7 persists the `NoAudioSource` record as a completed, non-billable source-analysis disposition. Cost estimation and scheduling create zero PCM/STT/VAD requests for it; `inspect` reports `no_audio: keep/review`, while unrelated assets continue. Reprojection/export retain the XML instances unchanged.

## Required brief ownership after approval

- P1-03: add/freeze `ResolvedMedia.has_audio` and propagation tests.
- P2-02: state that references exist only for `has_audio=True`; no optional/fake reference; declared-audio extraction failure remains an error.
- P3-02: own `NoAudioSource`, `SourceAnalysis`, full-media/partial-reference join, and zero-call tests.
- P3-03: accept the union and distinguish expected no-audio absence from missing audio analysis.
- P4-01 through P4-03: include `has_audio` in audibility and exclude no-audio from boundary/reference requirements.
- P5-03/P6-01: prove no-audio instances produce no eligible edit blocks and retain a review reason.
- P7-01/P7-02/P7-03/P7-06: persist/report the status, estimate zero audio cost, schedule zero audio work, and cover mixed audio/no-audio projects.

## Acceptance matrix

- Declared `has_audio=False`: no ffprobe/extraction/STT/VAD calls; no `PcmReference`, PCM origin, or `SourceTrace`; one immutable `NoAudioSource`; clip retained and reviewable; export identity preserved.
- Declared `has_audio=True` with valid stream: unchanged measured-PCM and typed success/empty/error flow.
- Declared `has_audio=True` but stream missing/malformed: explicit per-resource error/keep-review; never downgraded to `no_audio` or `empty`; unrelated assets proceed.
- Mixed project: valid audio assets still analyze; video-only assets remain unchanged; no global readiness failure solely because video-only resources exist.

This is intentionally conservative for v1: video-only B-roll is not automatically shortened. A later policy may make silent-video editing eligible only through a separately reviewed contract; this disposition grants no such authority.
