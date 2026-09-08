# Sol final P3 engine-seam proposal

Date: 2026-09-09  
Status: proposed to Astra after Opus review; do not dispatch P3 until accepted and folded into briefs

## Minimal legacy-safe seam

Extend `repair.Source` additively; do not change `repair.settle`:

```python
@dataclass(frozen=True, slots=True)
class Source:
    stem: str
    duration: float
    audio: Path | None = None
    workdir: Path | None = None
    namespace: Path | None = None
    cutter: Callable[[Sequence[Span]], list[Chunk]] | None = None
```

`_job` copies `namespace` to every repair and arbiter `TranscriptionJob`. `_cut` calls `source.cutter(spans)` when present and otherwise executes its current legacy body. `None` preserves all existing legacy calls and their graceful fallback byte-for-byte.

The injected cutter itself is strict without another public repair mode or result field. It must return one chunk per nonempty span or raise a contextual `RuntimeError`; `_cut` never catches an injected-cutter exception or routes its count mismatch through the legacy empty-chunk fallback. The project adapter catches it and returns `SourceTrace(status="error")` with real provenance. The default legacy cutter retains its current catch/log/fallback behavior.

Repair/arbiter call and payload failures use a project-owned capture wrapper around the injected `Transcriber`, not a new `Repair.failures` field and not changed legacy catch semantics. The wrapper records a sanitized stage error before rethrowing a delegate exception; legacy repair may catch that exception as it does today, but after `settle` the project adapter observes the captured error and refuses to publish/cache success. Before returning responses to repair it also validates the same total-outcome rules as P3-01 and exact requested-chunk coverage: one unambiguous matching response per requested chunk; duplicate, malformed, unmatched, or missing requested coverage records an error and raises. Extra fixture responses outside a retry subset may be deterministically filtered, never paired by list position. A syntactically valid covered response with `segments=[]`, repetitive content, or an arbiter result of no speech remains an ordinary engine outcome and may follow the existing kept/collapsed logic.

No capture object crosses the project boundary or enters a legacy dataclass/serialization. P3-02 owns its private wrapper; P4-03 mirrors the same tested rule for boundary jobs. After any captured operational error, the adapter returns an error trace with the real reference identity/hash/origin/analysis key, sanitized failing stage, already-completed draft/coverage evidence, no completed settled cues/SRT artifact, and no cached-success disposition; unrelated sources continue. Boundary analysis returns its typed error record and preserves original source text for unresolved projection.

This corrects the silent-failure shape without changing the legacy `Source(cutter=None)` or repair/arbiter exception behavior. It also accounts for current `repair.settle`: an empty `_cut` result does not simply return the draft, because `_rescue` and `_assemble` still run and can consume fixture responses or collapse a long repeat.

## Clock and presentation rule

Do not change `transcribe.parse_segments`, `transcribe.build_spine`, `Segment`, `Cue`, `Repair`, or their float compatibility merely to remove float arithmetic. No concrete wrong-offset reproduction justifies shared response-model churn.

- Legacy `Segment`, `Cue`, and `Repair` times remain decoded-audio-local floats. `SourceTrace.cues` and `.repairs` retain those immutable legacy records for provenance; their clock is stated explicitly.
- `SourceSegment.audio_start/audio_end` are `Fraction(str(legacy_segment.start/end))`. `file_start/file_end` add `PcmReference.pcm_sample0_file_time` exactly once.
- P3-03 projects the already-refined `SourceTrace.cues`, not `settled_spine`, because refined cue margins, text, flags, split/merge, and polish are presentation authority. Each cue endpoint becomes exact file time as `pcm_sample0_file_time + Fraction(str(cue_endpoint))`, then uses the exact XML mapping. It never rebuilds presentation from segments or polishes twice.
- Exact P2 `SampleRange` values and PCM origin remain separate immutable provenance. Converting the already-existing legacy float with `Fraction(str(...))` preserves its decimal value but does not claim to reconstruct a pre-float rational.
- Source `repair.Source.duration` and `refine.refine(..., duration=...)` both receive `float(Fraction(reference.sample_count, reference.sample_rate))`. A boundary run uses `float(Fraction(range.end - range.start, reference.sample_rate))` for both. Origin is not added to a local duration.
- P0's locked legacy SOURCE bytes constrain unchanged legacy inputs and paths. Newly extracted project PCM timing is not required to produce byte-identical SRT to the legacy MP3 pipeline.

## Real PCM scratch and acceptance

Source analysis writes every initial and nested repair WAV beneath the explicit ephemeral scratch directory `<artifact_dir>/<asset_content_id>/work/<analysis_key>/`. Its durable fixture/artifact namespace remains `<artifact_dir>/<asset_content_id>/`, and scratch files are never declared content-addressed completed artifacts or reused without the P2 reference/hash contract. Boundary analysis mirrors this at `<artifact_dir>/<asset_content_id>/boundary/<cache_key>/work/`, with its parent as the namespace.

Offline tests must create actual synthetic WAV slices and assert their exact frames, channels, byte ranges, offsets, and durations for initial plus nested repair cuts. `FixtureTranscriber` may ignore `Chunk.path`, but that is not evidence that the adapter produced the right retained PCM. Tests separately prove namespace lookup and zero network calls.

## Required red cases before dispatch

- Legacy `Source()` default behavior and P0 byte lock unchanged.
- Injected cutter exception, empty/count mismatch, malformed/duplicate/unmatched/missing repair coverage, and repair/arbiter call failure become typed project error rather than immutable success.
- Valid covered empty/repetitive/arbiter-no-speech responses retain current kept/collapsed semantics.
- Nonzero/negative PCM origins, nonzero chunk offsets, refined cue lead margins, and boundary-local offsets are each added exactly once; no projection from settled-spine boundaries.
- Initial and repair WAV bytes come only from retained PCM in the named scratch tree; cleanup/failure cannot overwrite durable SOURCE artifacts.

No live model or Final Cut acceptance is claimed.
