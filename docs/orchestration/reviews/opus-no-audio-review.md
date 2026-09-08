# Opus5 review — `sol-no-audio-disposition.md` (revised) and its P3–P7 consumers

> Historical review checkpoint. Sol reconciled the findings in `21857cd` and
> propagated the consumer contracts in `5c1cff4`. Astra's final disposition keeps
> unavailable-record construction in P7 and content-trace deduplication intact;
> the original B1/C1 wording below is superseded. Opus confirmed all remaining
> findings closed in section 6 of `opus-hasaudio-semantics.md`. This archive is
> review evidence, not a list of still-open implementation requests.

Read-only review at `ddf7c14`, against the **revised** disposition carrying `UnavailableSourceAudio`.
No git index changes, no product or test edits, no media or API calls. Astra's finding — that a
declared-audio pre-PCM failure cannot ride inside a total `SourceTrace` — is addressed by that
revision and is not re-litigated here. The settled checker work is not reopened.

I drafted this review against the previous version and re-ran it against the revision before sending.
Five of my findings were closed by the revision and are recorded as closed rather than sent as
findings; what follows is only what still stands, plus two gaps the revision itself introduces.

---

## A. Closed by the revision — recorded so they are not re-raised

- **Same-content resources with differing `has_audio`.** The new *Resource/content join cases*
  section resolves this exactly: the unavailable record is resource-scoped, never participates in
  content-ID or analysis-key dedup, cannot suppress a valid content-scoped trace, and P3-03 checks a
  resource-scoped unavailable record **before** falling back to a content-scoped trace. That
  resolution order is the load-bearing part and it is now written down.
- **Two homes for "error".** `reason_code` with the iff-rule (`status == "no_audio"` iff
  `declared_no_audio`; `stream_unavailable`/`pcm_unavailable` require `status == "error"`) draws the
  pre-PCM / post-PCM line explicitly, leaving `SourceTrace(status="error")` for post-PCM failures that
  can still carry origin fields.
- **Persisted discriminator.** `variant: Literal["source_trace"]` on `SourceTrace` plus `variant` on
  the unavailable record makes the persisted JSON a stable discriminated union.
- **Free-text-only reason.** `reason_code` is now a closed `Literal` with free-text `reason` beside
  it — assertable in tests, aggregable in `inspect`.
- **`has_audio` correctly excluded from content identity.** It is an XML declaration, not an audio
  policy, so it belongs on `ResolvedMedia` and in resource-scoped addressing, not in
  `asset_content_id` or `analysis_key`. Folding it into either would break legitimate dedup between
  two resources over the same bytes. Recorded so a later worker does not "fix" it.

---

## B. Introduced by the revision

**B1 — layering inversion: P2-02 must now produce a P3-02-owned type.**

The frozen aggregate input is

```python
AudioSourceInput = tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]
```

so an `UnavailableSourceAudio` carrying `stream_unavailable` or `pcm_unavailable` must already exist
*before* `transcribe_unique_sources` is called. Those two codes describe P2 extraction and reference
persistence failures. But the ownership list assigns `UnavailableSourceAudio` to **P3-02**, while
P2-02 is told only to "state that references exist only for `has_audio=True`". As written, P2-02 must
construct a type it does not own and cannot import without depending on P3.

Two clean resolutions; either works, but one must be chosen before both briefs freeze:

1. Move `UnavailableSourceAudio` (and `SourceAnalysis`) down into the shared project models layer that
   P2 and P3 both already depend on, with P3-02 owning only the aggregation logic; or
2. Keep the type in P3-02 and have P2-02 return its own narrow failure record that P3-02 lifts into
   `UnavailableSourceAudio` at the boundary — at the cost of a second near-identical type.

(1) is simpler and matches how `PcmReference` already crosses the same seam. Whichever is chosen, say
which component constructs `stream_unavailable` and which constructs `pcm_unavailable`; today neither
brief claims them.

**B2 — three discriminators with an invariant that nothing enforces.**

`variant`, `status` and `reason_code` are mutually constrained by the iff-rule, but a frozen dataclass
performs no validation, so `UnavailableSourceAudio(status="no_audio", reason_code="pcm_unavailable")`
constructs happily and then persists. Require the rule to be enforced in `__post_init__` (raising, not
asserting, so `-O` cannot strip it) and add the two negative construction tests. This is cheap and it
is the difference between a documented invariant and an enforced one.

---

## C. Still open from the original review

**C1 — per-resource coverage and failure isolation are still prose, not interface.**

The revision improves this: pre-PCM failures now arrive *as inputs*, paired per resource, and the
function passes one unavailable record through per resource with zero STT calls. What remains
unstated is the behaviour for a **post**-PCM failure part-way through a batch. `transcribe_unique_sources`
returns `tuple[SourceAnalysis, ...]`, and nothing says that a raise inside one source is caught and
recorded rather than aborting the remaining sources. Two additions make the acceptance matrix's
"unrelated assets proceed" actually checkable:

- the function never propagates a per-source exception — each source yields a record, and only a
  programming error aborts;
- the result contains **each input `resource_scoped_id` exactly once**, so a consumer can assert
  totality rather than trust it.

**C2 — no coalescing regression for the mixed same-content case.**

P4-01 coalesces on "same source content, contiguous source and timeline ranges, and identical audible
policy", and `LogicalSlice.source_id` is the **content** id. For two adjacent slices over the same
bytes where one resource declares audio and the other does not, the *only* thing preventing a wrong
merge is the `audible` comparison — which is correct now that item 5 derives `audible` from
`media.has_audio`, but is entirely load-bearing and untested. Name the regression in P4-01: same
content, contiguous ranges, differing `has_audio` ⇒ **no** coalescing.

**C3 — `estimate_cost` still does not accept the union.**

`p7-02-runner.md:35` reads `estimate_cost(inputs, traces: Sequence[SourceTrace], ...)`. Item 7 requires
zero PCM/STT/VAD requests to be estimated for an unavailable source, which that signature cannot
express. Widen to `Sequence[SourceAnalysis]` and add the test that a project of only video-only
resources estimates zero audio cost instead of failing.

**C4 — union schema version and unknown-variant handling.**

`variant` fixes the *shape* discriminator, but P7-01 persists `source_analysis` as
`tuple[ArtifactRef, ...]` and nothing states that an unrecognised `variant` is rejected loudly, or
that the union carries a schema version. `Trace.from_dict` already applies exactly this discipline via
`TRACE_VERSION`, and P7 already rejects stale plans; the union should not be the one record that
degrades silently across versions.

---

## D. Lower priority

**D1 — `resource_scoped_id` on `LogicalSlice` / `SourceCue` / `PlacedSourceCue`.** All three carry
only the content `source_id`. A consumer can still reach the resource indirectly through
`instance_ids` and the document, and because C2's `audible` comparison keeps slices homogeneous, that
indirection is unambiguous — so this is a directness and readability improvement rather than a
correctness gap. Worth adding while the P4-01 signature is still unfrozen; not worth reopening later.

**D2 — declared audio with no audible instance is a real, deliberate cost.** A resource with
`has_audio=True` whose every instance is `enabled=0` or `srcEnable="video"` still receives a full
transcription. That is the right conservative default — the user may re-enable, and invariant rule 4
forbids concluding a region is not speech — but it is billable and currently unmentioned. One line in
P7-02 so it reads as chosen rather than overlooked.

---

## E. Verdict

The revised disposition is substantively right, and the resource-scoped-before-content-scoped
resolution order is the correct core of it. Before P2-02, P3-02, P3-03 and P4-01 freeze, close **B1**
(who owns and who constructs `UnavailableSourceAudio`) and **B2** (enforce the iff-rule), then **C1**
and **C3**, which are small and make two acceptance-matrix promises testable rather than aspirational.
B1 is the one that will otherwise stall a worker mid-task, because it cannot be resolved from inside
either brief.

No live Final Cut, media or model acceptance is claimed or implied anywhere in this review.
