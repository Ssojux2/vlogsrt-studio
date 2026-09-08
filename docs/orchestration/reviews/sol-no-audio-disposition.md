# Sol design disposition: assets with no audio

Date: 2026-09-09  
Status: proposed for Astra approval before shared-brief edits

## Problem

`AssetResource.has_audio` already records the XML declaration, but the drafted downstream interfaces assume that every resolved asset has a `PcmReference` and `SourceTrace`. An ordinary video-only resource must remain addressable for XML identity/export without inventing a PCM origin, calling STT/VAD, or relabeling the absence as a successful empty transcript. One such resource must not fail analysis of unrelated audio resources.

## Proposed minimal contract

1. Add `has_audio: bool` to `ResolvedMedia` in P1-03 and copy it from the exact matched `AssetResource`. This keeps the disposition attached to the resource even when two XML resources resolve to the same content bytes.
2. Keep `PcmReference` and `SourceTrace` total and audio-only. Do **not** make their hashes, sample counts, PCM origins, coverage, or transcript fields optional; do not construct either record when `ResolvedMedia.has_audio` is false.
3. Add one explicit P3-02 pre-PCM-unavailable variant rather than overloading a total `SourceTrace`:

   ```python
   @dataclass(frozen=True, slots=True)
   class UnavailableSourceAudio:
       variant: Literal["unavailable_source_audio"]
       resource_scoped_id: str
       source_id: str
       original_sha256: str
       status: Literal["no_audio", "error"]
       reason_code: Literal[
           "declared_no_audio",
           "stream_unavailable",
           "pcm_unavailable",
       ]
       reason: str

   SourceAnalysis = SourceTrace | UnavailableSourceAudio
   ```

   Add `variant: Literal["source_trace"]` to `SourceTrace` so persisted JSON is a stable discriminated union. Enforce `status == "no_audio"` iff `reason_code == "declared_no_audio"`; the two unavailable codes require `status == "error"`. Neither unavailable variant has PCM, origin, analysis-key, coverage, or transcript fields.

   `transcribe_source` stays audio-only. Freeze the aggregate interface as:

   ```python
   AudioSourceInput = tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]

   def transcribe_unique_sources(
       items: Sequence[AudioSourceInput],
       cfg: Config,
       transcriber: Transcriber,
       artifact_dir: Path,
   ) -> tuple[SourceAnalysis, ...]: ...
   ```

   Validate every pair by `resource_scoped_id`, `source_id`, and original hash. Pass through one unavailable record per resource with zero STT calls. Transcribe only real references and deduplicate those calls by `PcmReference.analysis_key`.
4. P3-03 accepts `Sequence[SourceAnalysis]` and adds `unavailable_audio: tuple[UnavailableSourceAudio, ...]` to `SourceCaptionSet`. For an audio-capable resource without an unavailable record it still requires one unambiguous `SourceTrace` and the measured origin, including successful-empty traces. For a resource-scoped unavailable variant it emits no cues for only that resource and carries the explicit keep/review disposition; it never fabricates an origin. `status="error"` prevents final-ready publication but does not prevent valid unrelated resources from being analyzed or included in an explicit draft.
5. P4-01 defines audibility as `media.has_audio and clip.enabled and clip.src_enable == "all"`. Video-only slices remain addressable with `audible=False`, preserve instances/blades, create no boundary requests, and cannot authorize automatic shortening.
6. P5 VAD consumes only real `PcmReference` values. A no-audio source produces no VAD call/result; its P4 slices are already inaudible and therefore yield no eligible P6 block. P6 records the resource/instances as keep-all review, not an analysis success.
7. P7 persists each `UnavailableSourceAudio` record as a completed, non-billable source-analysis disposition using its `variant`, `status`, and `reason_code`. Cost estimation and scheduling create zero PCM/STT/VAD requests for it; `inspect` reports either `no_audio: keep/review` or the pre-PCM error, while unrelated assets continue. Reprojection/export retain the XML instances unchanged.

## Resource/content join cases

- Two audio resources with the same `asset_content_id` and same PCM `analysis_key` share one `SourceTrace`, while each clip still joins through its own `ResolvedMedia.resource_scoped_id`.
- A no-audio resource and an audio resource may resolve to identical content bytes. The former keeps its resource-scoped `UnavailableSourceAudio(status="no_audio")`; the latter may still produce and consume a valid content-scoped `SourceTrace`. The unavailable record never participates in content-ID/analysis-key dedup and cannot suppress the valid trace.
- If one declared-audio resource has `stream_unavailable`/`pcm_unavailable` while another resource with the same content has a valid reference, only the failing resource is locked/reviewed; the valid resource may analyze. P3-03 checks a resource-scoped unavailable record before falling back to a content-scoped trace.

## Required brief ownership after approval

- P1-03: add/freeze `ResolvedMedia.has_audio` and propagation tests.
- P2-02: state that references exist only for `has_audio=True`; no optional/fake reference; declared-audio extraction failure remains an error.
- P3-02: own `UnavailableSourceAudio`, the stable variant discriminator, `SourceAnalysis`, exact pair joins, and zero-call tests.
- P3-03: accept the union and distinguish expected no-audio absence from missing audio analysis.
- P4-01 through P4-03: include `has_audio` in audibility and exclude no-audio from boundary/reference requirements.
- P5-03/P6-01: prove no-audio instances produce no eligible edit blocks and retain a review reason.
- P7-01/P7-02/P7-03/P7-06: persist/report the status, estimate zero audio cost, schedule zero audio work, and cover mixed audio/no-audio projects.

## Acceptance matrix

- Declared `has_audio=False`: no ffprobe/extraction/STT/VAD calls; no `PcmReference`, PCM origin, or `SourceTrace`; one immutable `UnavailableSourceAudio(status="no_audio", reason_code="declared_no_audio")`; clip retained and reviewable; export identity preserved.
- Declared `has_audio=True` with valid stream: unchanged measured-PCM and typed success/empty/error flow.
- Declared `has_audio=True` but stream missing/malformed: `UnavailableSourceAudio(status="error", reason_code="stream_unavailable")`; never downgraded to `no_audio` or `empty`; unrelated assets proceed.
- Declared audio whose extraction/reference persistence fails: `UnavailableSourceAudio(status="error", reason_code="pcm_unavailable")`; no fake path/hash/origin; unrelated assets proceed.
- Mixed/same-content project: valid audio assets still analyze; resource-scoped unavailable variants do not suppress a content-scoped valid trace; video-only assets remain unchanged; no global readiness failure solely because video-only resources exist.

This is intentionally conservative for v1: video-only B-roll is not automatically shortened. A later policy may make silent-video editing eligible only through a separately reviewed contract; this disposition grants no such authority.
