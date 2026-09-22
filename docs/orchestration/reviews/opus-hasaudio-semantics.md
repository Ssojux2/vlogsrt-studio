# Opus5 disposition — omitted `hasAudio` semantics for the P1 parser

Read-only. Sources: the exact installed schema
`/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources/FCPXMLv1_14.dtd`
(SHA-256 `33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5`, re-verified this session)
and Apple's primary `asset` documentation. No media opened, no library touched, no index/product/test
mutation. Answers root `msg_58db43475649`; `sol-no-audio-disposition.md` at `21857cd` line 88 defers
exactly this rule to this review.

---

## 1. What the schema literally says

```
<!ATTLIST asset hasVideo CDATA #IMPLIED>
<!ATTLIST asset hasAudio CDATA #IMPLIED>
```

`CDATA`, `#IMPLIED`, **no declared default, and not an enumeration**.

The schema states the optionality; that this omission is *deliberate design* rather than an
oversight is my **inference**, drawn from three consistent patterns in the same file:

1. **117 attributes in this DTD carry a declared default.** Apple declares defaults routinely.
2. **Apple uses `(0 | 1)` with an explicit default whenever it means a defaulted boolean** —
   `param enabled (0 | 1) "1"`, `md editable (0 | 1) "0"`, `tracking-shape offsetEnabled (0 | 1) "0"`,
   and ten-plus `adjust-* enabled (0 | 1) "1"`. `hasAudio` is neither enumerated nor defaulted.
3. **The other two inputs to our own audibility formula *do* have normative defaults**, which makes
   the asymmetry precise:

| audibility input | DTD declaration | normative default |
|---|---|---|
| `clip.enabled` | `%clip_attrs;` → `enabled (0 \| 1) '1'` | **yes — `1`** |
| `clip.srcEnable` | `asset-clip srcEnable (all \| audio \| video) "all"` | **yes — `all`** |
| `asset.hasAudio` | `asset hasAudio CDATA #IMPLIED` | **none** |

Two of the three may be safely defaulted from the schema. The third may not — that part is the
declaration itself, not inference. The DTD carries no comment on `hasAudio` anywhere.

## 2. What Apple's primary documentation says — this is the decisive part

Apple's [asset documentation](https://developer.apple.com/documentation/professional-video-applications/asset.md)
has a section titled "Implied Asset Attributes". It explains that these attributes are optional
**"because Final Cut Pro derives their values from the respective media file"**, warns that the file may
be offline when Final Cut Pro tries to read it, and for that reason recommends specifying them on every
asset. `hasAudio` is listed there, described as indicating whether the asset has audio, alongside
`hasVideo`, `audioSources`, `audioChannels`, `audioRate` and `videoSources`.

So Apple states the semantics of omission directly, and it is **none of "audio absent"**:

- absence does **not** mean no audio;
- absence does **not** mean audio;
- absence means the value is **derived from the media file** — i.e. *unknown, resolvable only by
  inspecting the media* — and Apple flags this as exactly the case that breaks when the file is offline.

That answers root's three-way question on the evidence rather than by preference: **absence is
normatively "unknown, requires the media file", not "no audio".**

## 3. Why neither default is acceptable for us

- **Absent → `False`** would silently skip STT on a file that may well carry speech. That is a
  region-is-not-speech decision taken from a missing attribute, which invariant rule 4 forbids and
  which is the D25 failure shape: quiet, plausible, and it eats real dialogue.
- **Absent → `True`** manufactures a PCM demand for genuinely video-only media and turns a legitimate
  project into a wall of `stream_unavailable` errors.
- Root's instruction is explicit and I agree with it: do not infer true by default, and do not treat
  unknown as successful-empty.

A `bool` cannot express three states, so the defect is not the type itself — it is a `bool` that
**silently absorbs omission**, because that is where a guess hides. Astra's disposition resolves this by
refusing the record instead of widening the type: with `unspecified` rejected at the asset path (R3),
the `bool` on `AssetResource`/`ResolvedMedia` is total and truthful, since it is only ever built from an
explicit `0` or `1`. What must not survive is any code path that reaches a `bool` from a missing
attribute.

## 4. Recommendation

**R1 — superseded by Astra's binding refinement; keep `has_audio: bool`.** I originally proposed a
tri-state `Literal["yes","no","unspecified"]` on `AssetResource`/`ResolvedMedia`. Astra's refinement is
tighter and I accept it: because R3 rejects `unspecified` **at the asset path, before any record is
constructed**, every `AssetResource` that exists has an explicit `0` or `1` by construction. A
propagated tri-state would therefore carry an unreachable variant — a dead state that every downstream
consumer would have to handle and no test could reach. The bool stays, and it stays *honest* precisely
because the parser refuses to build a record it cannot fill. Only an explicit `0` permits
`declared_no_audio`.

**R2 — define the accepted lexical forms, because the type is `CDATA`, not `(0|1)`.** A validating
parser will happily accept `hasAudio="true"`, `"yes"`, `"2"`, or `""`. Accept exactly `"1"` → `yes`
and `"0"` → `no`; any other present value is a supported-subset rejection with XML location and
reason. Do not coerce. This matches how the project already rejects unsupported constructs rather
than normalising them, and it keeps a future Apple spelling from being silently misread as truthy.

**R3 — for v1, `unspecified` is a supported-subset rejection, not a guess.** Reject with location,
reason, and an actionable message that cites Apple's own recommendation to specify these attributes
for all assets. This guesses nothing, costs nothing, and is squarely the posture the binding spec
already takes (“Reject … with XML location/reason, never silently flatten”). It must be reported as a
**narrower supported-subset requirement**, not as malformed FCPXML — the document is schema-valid.

**R4 — pre-specify the upgrade path so adopting it later does not reopen the contract.** I could not
verify whether FCP 12.3 actually omits `hasAudio` on export: that needs a real export, and I have no
media and must not open a library. If real exports turn out to omit it, R3 becomes user-hostile, and
the authorised upgrade is to **measure, never default**:

- resolve `unspecified` by one ffprobe through the existing `preprocess.py` boundary that P2-01
  already adds (`probe_audio_stream`) — no new subprocess boundary;
- record provenance on the resolved value, e.g. `audio_presence_source: Literal["declared","probed"]`,
  so a measured value is never mistaken for a declared one;
- a probe that cannot run or cannot decide is `UnavailableSourceAudio(status="error",
  reason_code="stream_unavailable")` — a retryable failure, **never** `declared_no_audio` and never
  successful-empty.

Note the ordering consequence: probing costs a media read at *ingest* time, whereas today `has_audio`
is pure XML. If R4 is ever adopted, `inspect` stops being a zero-media operation for documents that
omit the attribute. Worth stating when the decision is made, not discovered afterwards.

**R5 — superseded: `hasVideo` gets a lexical check only, no model.** I proposed full symmetry. Astra's
refinement: `hasVideo` has no modeled or defaulted use in this project, so apply the R2 lexical `0`/`1`
check when the attribute is **present**, and leave it raw and uninterpreted when omitted. No field is
added. Modelling an attribute we never consume would be speculative, and an omitted `hasVideo` cannot
mislead anything that never reads it. The R2 check still prevents a present-but-unexpected spelling
from being silently accepted.

**R6 — the lifecycle distinction must survive this change.** `declared_no_audio` may only be produced
from an explicit `hasAudio="0"`. An `unspecified` attribute must never reach that reason code: under
R3 it is an ingest rejection, and under R4 it is either a probed `yes`/`no` with `probed` provenance
or a retryable `stream_unavailable`. This is what keeps `21857cd`'s completed-and-non-billable versus
retryable-failed split honest — a guessed `no` would be permanently cached as a completed success and
would never be retried.

## 5. Scope — recommendation plus Astra's disposition

This document is a **recommendation**; it freezes nothing on its own. Astra dispositioned it as follows,
and that disposition is what binds:

- **Accepted:** the primary evidence in §§1–3, and **R2** (lexical `0`/`1` only, no coercion), **R3**
  (`unspecified` is a supported-subset rejection at the asset path, reported as a narrower subset
  requirement rather than malformed FCPXML), and **R6** (`declared_no_audio` only from an explicit `0`).
- **Refined:** **R1** — `has_audio` stays a `bool` on successfully parsed `AssetResource`/`ResolvedMedia`,
  since R3 makes any third state unreachable; **R5** — `hasVideo` gets the present-value lexical check
  only, stays raw and uninterpreted when omitted, and gains no modeled field.
- **Not authorised:** **R4** (probe-based resolution). A future measured resolution requires a separate
  reviewed change, and the `inspect` media-access consequence is documented here so that decision is
  taken deliberately.

Owning briefs: P1-02 (parse, lexical check, reject `unspecified`), P1-03 (propagate the bool),
P3-02/P7 (R6 wording), P4-01 (`audible` reads the bool as before).

No live Final Cut, media, or model acceptance is claimed or implied.

## 6. Disposition on root's corrections to my earlier findings

- **B1 accepted as corrected.** Construction-ownership omission, not layering inversion. `21857cd`
  item 7 now has P7's runner catch contextual P2 failures and construct the P3-owned record, with P2
  staying audio-only, raising contextual errors, never importing P3. That is cleaner than either
  option I offered, and it needs no new models dependency.
- **C1 accepted as corrected.** The invariant is that **each resource resolves exactly once** to
  either its own unavailable record or a shared valid trace — not that each resource gets its own
  copy of a content-deduped `SourceTrace`. My earlier "exactly once" wording was ambiguous on that
  and would have read as a licence to duplicate traces per resource.
- **B2, C2, C3, C4 adopted** in `21857cd` (iff-rule enforcement, the equal-content/differing-declaration
  coalescing test at line 64, `estimate_cost` over `Sequence[SourceAnalysis]`, unknown-variant and
  schema-revision rejection before work), and the **error-retry versus completed-no-audio** distinction
  is now explicit at lines 55 and 84. Nothing outstanding from my previous review.
