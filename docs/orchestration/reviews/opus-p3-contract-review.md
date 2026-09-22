# Historical Opus P3 contract review and corrections

**Status: review closed; archived 2026-09-09 KST.** This document preserves the
initial critique and the subsequent corrections/withdrawals. Earlier recommendations
are historical evidence, not implementation instructions. The accepted
[Sol disposition](sol-p3-seam-disposition.md) and current P3/P4 task briefs are
authoritative (Astra acceptance `msg_e4df6816b3ac`, folded briefs `6c6018e`,
status confirmation `32de778`). P3 implementation still waits for the P1/P2 joins.

The final contract uses an additive `repair.Source` namespace/cutter seam and a
project-owned transcription failure capture wrapper. Legacy floats and
serialization stay intact; source captions derive from already-refined cues,
with measured PCM origin added exactly once. Cut metadata uses actual snapped
SampleRange values, and project response coverage matches exact chunk identities.
Real synthetic initial/nested WAV bytes are required even when fixture responses
ignore the audio path. Claims below about returning the draft directly, deriving
presentation from raw spine, adding serialized failure fields, or matching raw
unsnapped span endpoints were corrected or withdrawn during review.

---

## Original review and chronological follow-ups

Read-only against manager docs `5c1cff4` (`git show 5c1cff4:docs/orchestration/tasks/…`) and current
product source. No index, code or test mutation. Accepted no-audio design and the `hasAudio`
disposition are **not** reopened. No generalized frameworks proposed — every item below is a concrete
cross-module contract blocker with an exact site and a minimal recommendation.

The briefs are in good shape; P4-03 in particular already solves the hardest clock problem correctly
(boundary-local decoded time, one exact conversion, no millisecond rounding before serialization).
Six items would otherwise be resolved by a GLM guessing, and two of them are silent-failure shaped.

---

## 1. Repair cutter injection has no call shape — hard blocker

**Where.** `vlogsrt/repair.py:102` `settle(draft, source, cfg, transcriber)`;
`vlogsrt/repair.py:254` `_cut(source, spans, cfg)` → `preprocess.cut_spans(source.audio, spans,
source.duration, source.workdir)`.

**Trigger.** P3-02 says *"The project adapter injects a cutter backed by the same retained P2 PCM
reference"* and separately *"Extend `repair.Source` with the optional explicit namespace"*, but never
says where the cutter enters. `settle` has no such parameter and `_cut` reaches for `source.audio` /
`source.workdir` directly. Two dispatches depend on this seam (P3-02 and P4-03's nested repair), so a
guess here is a guess in two places.

**Recommendation.** Put it on `Source`, not on `settle`, so no legacy caller signature changes:

```python
cutter: Callable[[Sequence[Span]], list[Chunk]] | None = None
```

`_cut` uses it when present and keeps its current body otherwise. Legacy behaviour stays byte-identical
because the default is `None`, which is the compatibility property P3-02 already promises.

## 2. A failed injected cut degrades silently into an unrepaired "success" — correctness blocker

**Where.** `vlogsrt/repair.py:254-277`. `_cut` returns `[]` both when `preprocess.cut_spans` raises and
when the chunk count disagrees with the span count, logging a warning; `settle` then returns the draft
unchanged.

**Trigger.** For legacy that is deliberate graceful degradation — subtitles still ship. For the project
path the same silence produces a `SourceTrace(status="success")` whose repetition was never repaired,
and P3-02 declares those traces **immutable and cached**. The degradation is then permanent and
invisible, which is the failure shape invariant rule 9 exists to prevent.

**Recommendation.** State in P3-02 that when a cutter is injected, a cut failure is **not** degradable:
either the injected cutter raises and the adapter records `status="error"`, or the adapter detects
`plans > 0 and chunks == 0` and records `error`. Pick one explicitly; do not leave both plausible.

## 3. `SourceTrace` mixes two clocks with no label — interface blocker

**Where.** `SourceTrace.cues: tuple[Cue, ...]` and `SourceTrace.repairs: tuple[Repair, ...]` are the
**legacy** types: `vlogsrt/models.py` `Cue.start/end: float`, `Repeat.start/end: float`,
`Repair.span: Span = tuple[float, float]` — all audio-local floats. In the same record
`SourceSegment` carries explicit `audio_*` **and** `file_*` `Fraction`s.

**Trigger.** A consumer holding a `SourceTrace` cannot tell which clock `cues[i].start` is in. P3-03
must place cues on the input timeline and has two candidate sources for the same information, one exact
and one lossy.

**Recommendation.** State in P3-02 that `cues` and `repairs` are retained **verbatim legacy records in
audio-local float time, kept for provenance only**, and that P3-03 projects from `settled_spine`'s
`file_*` Fractions. Otherwise the float cues quietly become a second clock and the exact one is
decorative.

## 4. `refine.refine` duration is unspecified, and it changes output bytes

**Where.** `vlogsrt/refine.py:273` `refine(cues, cfg, duration=0.0)`; `duration` feeds
`_lead_out_room` and `_stretch_ceiling`, so it is not cosmetic.

**Trigger.** P3-02 pins the engine order but not this argument. Legacy passes the mp3
`preprocess.probe_duration`; the project has only PCM.

**Recommendation.** Pin `duration = float(Fraction(reference.sample_count, reference.sample_rate))`.
And state the consequence plainly: because the duration source differs and project origins are
sample-derived rather than MP3-segment-derived, the project `SOURCE.srt` is **not** expected to be
byte-identical to legacy output for the same media. Without that sentence someone will eventually
assert it against P0-03's locked bytes and treat the difference as a regression.

## 5. Nowhere is named for the sliced PCM input files

**Where.** `transcribe_source(media, reference, cfg, transcriber, artifact_dir)`; the engine needs
`TranscriptionJob.chunks` whose `Chunk.path` are real files.

**Trigger.** `<artifact_dir>/<asset_content_id>/` is defined as the durable namespace holding
`<stem>.SOURCE.srt` and the fixture files. Ephemeral slice WAVs have no declared home, so they will
land in the durable directory and be indistinguishable from artifacts.

**Recommendation.** Name a separate ephemeral directory, e.g.
`<artifact_dir>/<asset_content_id>/work/`, and state it is not durable and not content-addressed.
Also state the fact that makes offline testing cheap: `FixtureTranscriber.transcribe`
(`vlogsrt/transcribe.py`) reads only `<namespace>/<stem>.<kind>.json` and **never opens
`chunk.path`**, so offline fixture runs need no WAV files at all. P4-03 needs the same statement for
`…/boundary/<cache_key>/`.

## 6. The exact PCM origin is laundered through a float add — precision seam

**Where.** `vlogsrt/transcribe.py::parse_segments` computes `start = float(segment["start"]) + offset`
where `offset` is `ChunkResponse.offset: float`. P3-02 then says *"Convert legacy float segment
boundaries with `Fraction(str(value))`; add the measured `reference.pcm_sample0_file_time`"*.

**Trigger.** The exact `Fraction(range.start, sample_rate)` origin must be rendered to a float to become
`Chunk.offset`, added to the segment float inside `parse_segments`, and only then recovered with
`Fraction(str(...))`. The exactness P2 was built to provide is lost before P3 reads it, and the loss is
invisible.

**Recommendation.** Mirror what P4-03 already specifies for boundaries: run the engine in
**chunk-local decoded time** and add the exact source-file origin **once**, as
`Fraction(range.start, sample_rate) + Fraction(str(local_value))`, rather than re-deriving from a float
that already absorbed the offset. Note that `transcribe.build_spine` orders responses by
`response.offset` and bases segment ids on accumulated length, so the per-response `offset` must still
be the rendered float for ordering — the fix is to keep the exact value alongside it and project from
the local value, not to zero the offset. P3-02 and P4-03 should state the identical rule so the two
paths cannot drift.

---

## 7. Checked and clean — recorded so they are not re-raised

- **Operational error isolation is now specified.** P3-02: *"A failure after a real PCM reference
  exists is an error `SourceTrace` retaining the real reference identity, hashes, origin, and analysis
  key… and unrelated source analyses still proceed."* That closes the C1 concern from my earlier
  review; no further wording needed.
- **Shared trace joins are specified by validated keys, not position.** P4-03 and P4-04 both require
  resolution by `source_id` plus `pcm_analysis_key`, demand one unambiguous match, forbid `zip` and
  path/stem inference, and require shuffled-order tests. This is the right discipline and it is
  already explicit.
- **Engine order and single polish.** Both P3-02 and P4-03 pin
  `build_spine → repair.settle → spine_to_cues → refine.refine`, and both state that `refine` already
  invokes polish so it must not be called twice. Verified against `vlogsrt/refine.py:275`.
- **`TranscriptionJob.namespace` is additive and safe.** Defaulting to `None` preserves the legacy
  fixture path, and it does not disturb `check_no_prompt_to_whisper` or `check_one_transcription_call`,
  which inspect `_extras` and the single `transcriptions.create` site.
- **P3-01's empty/error split matches the code.** `parse_segments` raises on a missing `segments` key
  while an explicit empty list yields `[]` from `build_spine`, so the two states are genuinely
  distinguishable at the boundary rather than by convention.

---

## 8. Priority

Items 1 and 2 block P3-02 dispatch: the first has no answer in the brief, and the second decides
whether a silent degradation can be cached as an immutable success. Items 3, 4 and 6 are clock and
byte-compatibility contracts that are cheaper to state now than to reconcile after two phases have
committed against different readings. Item 5 is small and purely additive.

No live Final Cut, media or model acceptance is claimed or implied.

---

# Disposition and factual corrections (root review)

Root read the report against actual code and corrected two claims. Both corrections are right and I
verified them in source before accepting.

## Accepted by root

Item 1 (additive `Source` cutter shape), item 2's *strictness* requirement for an injected cutter,
item 4 (explicit `refine` duration) and item 5 (explicit scratch directory). No change to those.

## Correction to item 2 — my mechanism was wrong

I wrote that on a failed cut "`settle` then returns the draft unchanged". That is false.
`vlogsrt/repair.py:102-113` returns early **only** when `_plan` yields no plans. With `chunks == []`
it still calls `_rescue(plans, [], …)` and `_assemble(…)`, and:

- `_ask_whisper` (`repair.py:384`) builds a job with an empty chunk subset. `FixtureTranscriber`
  ignores `job.chunks` entirely and reads `<stem>.rescue.json`, so **fixture runs still rescue with no
  chunk files at all**;
- `_assemble` → `_apply` (`repair.py:458`) still applies policy: a repeat shorter than
  `repair_collapse_min_seconds` becomes `REPAIR_KEPT`, a longer one becomes `REPAIR_COLLAPSED`.

So the concern is not "repair silently does nothing". Stated correctly it is narrower and, I think,
still real: **a long repeat can be collapsed under a policy that means "we retranscribed and failed"
when the cut never happened, and the durable `Repair` record cannot tell the two apart.** That is the
same question root raises below, so it is folded into the taxonomy rather than kept as a separate item.

## Item 3 withdrawn — P3-03 must use the refined cues

Rejected correctly. P3-03 requires the already-refined `trace.cues` **including presentation margins**;
projecting from `settled_spine` would silently discard `refine`'s splits, merges and lead-in/lead-out
work. What survives from item 3 is only the labelling: mark `SourceTrace.cues` and `.repairs` as
**audio-local float** records, and retain `SourceSegment`'s exact provenance beside them. No change of
source of truth.

## Item 6 — engine rewrite withdrawn; one measured consequence reported

Root is right that the binding deliberately converts legacy floats via `Fraction(str(value))` and that
exact ranges/origins are retained separately, and right to ask for a concrete failure rather than a
rewrite. I measured instead of arguing — 200,000 randomized cases, sample-exact origins up to 80
minutes at 16 kHz, millisecond-precision local segment values:

```
worst |float-path − exact-path| = 1.0e-12 s
worst case: start_sample=71366283 local=76.967 → 4537.359687499999 vs 4537.3596875
half-up millisecond boundary flips: 1411 / 200000  (0.71%)
```

**The magnitude vindicates root: 1e-12 s is nothing, and no engine rewrite is warranted.** I withdraw
the chunk-local-time proposal.

The one thing worth recording is not magnitude but *tie behaviour*. When the exact time lands on a
half-millisecond tie, the float path can sit ~1e-12 below it and half-up then rounds the other way —
a 1 ms difference, in about 0.7% of ties. That only becomes observable if **two paths serialize the
same source boundary from different representations**: SOURCE SRT via P3-03's float `trace.cues`, and
EDIT SRT via P4-03's exact-`Fraction` projection. The binding spec's "same boundary same value" rule is
exactly about that case.

So the narrow, non-invasive question for Astra is not how the engine runs, but **which representation
the final serializer reads** when both exist. If SRT milliseconds are always taken from the retained
exact `Fraction` at the single serialization site, the discrepancy cannot occur and nothing else
changes. I am not proposing it as a defect — it is a prediction with a measurement attached, and it is
cheap to settle now and awkward after P6-03 exists.

## Operational failure versus intentional repair policy

Root's request: distinguish operational failures the engine already swallows from deliberate policy,
and propose a narrow way for project status to expose the former while legacy behaviour stays intact.

**Operational failures — currently logged and then indistinguishable from a failed rescue:**

| site | condition | current handling |
|---|---|---|
| `repair.py:262` | `preprocess.cut_spans` raises `RuntimeError` | `log.warn`, return `[]` |
| `repair.py:266` | chunk count ≠ span count | `log.warn`, return `[]` |
| `repair.py:392` | rescue `transcriber.transcribe` raises | `log.warn`, return `{}` |
| `repair.py:299` | arbiter `transcriber.transcribe` raises | `log.warn`, return `{}` |

**Intentional policy — correct, and must not change:**

- arbiter heard no recoverable speech (`_heard_speech` false) → stop paying, then keep or collapse;
- `_recovered` false after the allowed attempts → give up by design;
- repeat shorter than `repair_collapse_min_seconds` → `REPAIR_KEPT` deliberately (D28);
- long unrecovered repeat → `REPAIR_COLLAPSED` deliberately (D27).

The distinction already exists for one case and not the others: `Repair.arbiter_answered` was added
precisely so "no verdict" could not be read as "verdict: no speech" — its own docstring says the two
demand different fixes. There is no equivalent for cut or call failures, so all four rows above are
invisible in the durable record.

**Narrow proposal — one additive field, no behaviour change:**

```python
# vlogsrt/models.py :: Repair
failures: tuple[str, ...] = ()   # closed vocabulary, empty for every legacy path
#   "cut_failed" | "cut_count_mismatch" | "rescue_call_failed" | "arbiter_call_failed"
```

Populated only at the four sites that already emit a warning, so no control flow moves and no legacy
decision changes. `Repair.from_dict` already reads every optional field with `.get`, so existing traces
round-trip unchanged and the default empty tuple keeps `asdict` output byte-compatible for runs where
nothing failed.

P3-02 then surfaces it **without touching `status`**: a trace whose repairs carry any `failures` entry
is still `status="success"` — subtitles exist and are correct as far as they go — but P7 can report
"repair work was attempted and failed operationally" separately from "repair policy chose to keep or
collapse". That is the whole difference between a run that needs a retry and a run that is finished,
and it is currently unrecoverable from the trace.

This also resolves the corrected item 2: a collapse that followed a failed cut carries
`cut_failed`, and a collapse that followed a genuine unrecovered rescue does not.

---

# Second round of corrections (root review)

Root rejected the `Repair.failures` proposal and corrected my statistics. I verified both against the
real code and the real numbers before accepting; both corrections are right and one of my claims was
flatly wrong.

## `Repair.failures` withdrawn — my compatibility claim was false

I asserted that a `failures: tuple[str, ...] = ()` default "keeps `asdict` output byte-compatible".
That is wrong. `dataclasses.asdict` serializes **every** field regardless of default, which the real
model confirms:

```
asdict(Repair(...)) keys -> ['after', 'arbiter_answered', 'arbiter_clean', 'attempts',
                             'before', 'outcome', 'repeat', 'residual_seconds', 'span']   # 9 of 9
```

`arbiter_answered`, `span` and `residual_seconds` all carry defaults and all appear. So the proposed
field would add `"failures": []` to every repair record in every `trace.json`, changing the serialized
shape of legacy output. The proposal is correctly rejected, and it should not be revived in that form.

Sol's direction — project-owned capture around the injected cutter and `Transcriber`, using the
already-accepted error `SourceTrace` semantics while legacy decisions stay untouched — achieves the
same distinction without widening a legacy model. That is the better shape: the taxonomy in the
previous section stands as a description of *where* the four operational failures occur
(`repair.py:262`, `:266`, `:392`, `:299`), and the project captures them at its own boundary rather
than by changing what legacy records.

## Same-boundary serialization — concern withdrawn

Root: P3 derives the `Fraction` `SourceCue` **once** from the refined `trace.cues`, and EDIT reuses
that cue identity rather than recomputing it from the spine; a boundary retranscription is a *different
cue identity*, not a second representation of the original.

That dissolves the concern rather than answering it. My prediction required two paths serializing the
same boundary from different representations. With one derivation per cue identity, that situation
cannot arise, so there is nothing to decide at the serializer. Withdrawn.

## The tie experiment, correctly labelled

Root is right that I mislabelled the denominator, and correcting it changes the number materially. The
honest figures, recounted with ties identified explicitly:

```
sampled cases          : 200,000
ms flips (all cases)   : 1,411   = 0.706% of ALL sampled cases   <- what I previously called "of ties"
exact half-millisecond ties : 12,584
flips that were ties   : 1,411   (every flip was a tie)
flip rate among ties   : 11.2%
```

So "0.71% of ties" was wrong twice over: 0.706% is the share of all sampled cases, and the rate among
actual ties is 11.2%. This is recorded as **limited evidence from a synthetic random sample**, not as a
measured property of any real transcript: the local values were uniform random rounded to milliseconds,
which is not how whisper distributes segment boundaries, and no real media was involved. Given the
withdrawal above it has no consequence for the design; it is kept only so the measurement is not
remembered in its mislabelled form.

## Standing

Items 1, 4 and 5 remain as accepted. Item 2 stands only in its corrected, narrower form and is now
addressed by Sol's project-owned capture. Items 3 and 6 are withdrawn. No code scope expansion is
proposed. Awaiting the next exact-SHA review.

---

# Final consistency check of `sol-p3-seam-disposition.md` (`2d503a6`)

No contradictions with the accepted design. The seam matches item 1, the strictness rule is item 2 in
its corrected form and explicitly records the mechanism I had wrong, the clock rule matches root's
disposition, and the scratch tree is item 5 with better specificity. One concrete gap in the same seam,
one inert-value note, one correction of mine that Sol improved on.

## Gap — the injected cutter's offsets are load-bearing but unconstrained

`vlogsrt/repair.py:403` `_by_span` attaches each response to a plan by **nearest `chunk.offset`**, and
rejects only when `abs(chunk.offset - response.offset) > chunk.duration`. Plan indices come from that
matching, so the returned chunks' `offset` and `duration` decide which span's retranscribed text lands
at which position.

The disposition constrains only the **count**: *"a returned chunk count different from the nonempty
span count raises"*. A cutter returning the right number of chunks with wrong offsets would pass that
check and then silently mis-attribute responses across spans — the same silent-loss shape `_cut`'s own
docstring warns about for count mismatch (`repair.py:256-259`), for which offsets currently have no
equivalent guard.

**Recommendation, one line in the same strict clause:** on the injected path also require each returned
chunk's `offset` to equal its requested span start and its `duration` to equal the span length, and
raise on mismatch. Because project slicing is exact sample arithmetic rather than ffmpeg frame
boundaries, the strict path can assert equality instead of relying on the legacy tolerance of a whole
chunk length.

## Note — `Source.duration` is inert on the injected path

Verified: `source.duration` is read at exactly one site, `repair.py:263`, inside `_cut`'s legacy body
(`preprocess.cut_spans`). `_plan` does not use it, and `repair.py:421` reads `chunk.duration`, not
`source.duration`. So setting it to `float(Fraction(sample_count, sample_rate))` is consistent and
harmless but has no effect once a cutter is injected. Worth recording only so nobody later chases a
phantom mismatch there.

## Correction of mine that Sol improved on

In item 5 I noted that `FixtureTranscriber` never opens `Chunk.path`, and framed it as "offline fixture
runs need no WAV files at all". True of the transcriber, but Sol is right that it would have licensed
weak tests: ignoring `Chunk.path` is not evidence the adapter produced the correct retained PCM.
Requiring real synthetic WAV slices with asserted frames, channels, byte ranges, offsets and durations
for initial and nested repair cuts is the stronger position, and it supersedes my framing.

Otherwise: closure acknowledged. Awaiting the corrected P1/P2 exact-SHA review.

## Correction — my equality target was wrong

Root's refinement, accepted. The count-only observation stands, but my proposed fix — *"require each
returned chunk's `offset` to equal its requested span start and its `duration` to equal the span
length"* — is wrong, and would have falsely rejected every correct cut.

`p2-02-pcm-reference.md:51` converts ranges with `ceil(start * sample_rate)` and
`floor(end * sample_rate)`, clamped to the real per-channel frame count. Those endpoints are
**intentionally snapped**, so a correctly produced chunk legitimately differs from the raw requested
span by up to a sample at each end. Asserting equality against the raw span would fail on exactly the
behaviour P2-02 specifies.

The correct form is root's: the project cutter validates its metadata against the **actual
`SampleRange` offsets and duration** — the snapped values, not the raw request — and strict response
matching uses **requested chunk identity** rather than `_by_span`'s nearest-offset / whole-duration
tolerance. That is stronger than what I proposed, because it removes the nearest-match heuristic from
the strict path altogether instead of merely tightening its tolerance. Sol is folding it in, along with
namespace-present fixture-subset behaviour on repair retries.

No further broad review. Awaiting the corrected P1/P2 exact-SHA review.
