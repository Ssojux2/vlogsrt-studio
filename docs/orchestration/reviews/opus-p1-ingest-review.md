# Opus5 exact-SHA review — P1-02 FCPXML ingest

Reviewed commit: `05b6e3ed167d368974218afa22c4f8289b12c30a` on base
`07e4dac41888ae115157ed4a68d6dfd5fbd5b233`, worktree
`/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p1-ingest`, tree clean. Diff touches exactly
`vlogsrt/project/models.py`, `vlogsrt/project/fcpxml.py`, `tests/project/test_fcpxml.py`.
Read-only: no code, test or index change, no media read, no broad DTD search. I reviewed `05b6e3e`,
not the initial `eaee32b`.

**Verdict: one concrete blocker, everything else clear.** Root's six prior blockers are genuinely
corrected — I verified each by executing inline temp-XML probes against the parser rather than by
reading it, and cross-checked every probe document against the installed 1.14 DTD with `xmllint` so
that "accepted" and "rejected" are measured against real schema validity, not my own invention.

---

## Blocker — a DTD-invalid asset with no `media-rep` is accepted

The installed schema declares `<!ELEMENT asset (media-rep+, metadata?)>`: at least one `media-rep` is
**required**. An asset with none is rejected by `xmllint`:

```
element asset: validity error : Element asset content does not follow
```

but `parse_project` accepts it and produces `AssetResource(media_reps=())`.

That is a false acceptance of malformed FCPXML — the one class this parser exists to prevent, and
narrower than the subset question, since the document is not schema-valid at all. It also moves the
failure downstream: P1-03 would later report "no original media" for what is really a malformed input,
losing the XML location the binding spec asks for (*"Reject unresolved refs … with XML location/reason,
never silently flatten"*).

**Fix:** require `media_reps` to be non-empty in `_parse_media_reps`, rejecting with the element path
and a reason naming `media-rep+`. One condition, and it belongs with the existing resource checks.

**Not a defect, recorded so it is not "fixed" by mistake:** a **proxy-only** asset (one `proxy-media`,
no `original-media`) is DTD-valid and is correctly accepted at ingest. P1-03 owns the rule that a
missing original fails readiness even when a proxy exists, so leaving that to P1-03 is the right
layering.

---

## Root's six prior blockers — all verified corrected by probe

Each row is an executed probe. `dtd` is the installed-schema verdict for that exact document.

**1. `original-media` / `proxy-media` vocabulary translation**

| probe | dtd | result |
|---|---|---|
| `kind="original-media"` | valid | accept ✅ |
| `kind="proxy-media"` | valid | accept ✅ |
| `kind` omitted (schema default `original-media`) | valid | accept ✅ |
| `kind="original"` | **invalid** | reject, names both legal spellings ✅ |
| `kind="proxy"` | **invalid** | reject ✅ |

The internal `MediaRep.kind` stays `original`/`proxy` and translation happens at the XML boundary,
which is what the schema evidence required.

**2. `audioStart` / `audioDuration` / `audioOffset` split edits** — all three rejected with a
split-edit reason. `audioStart`/`audioDuration` are DTD-**valid**, so this is a genuine subset
narrowing rather than schema enforcement, and it is reported as such.

**3. Clip and spine `format` overrides**

| probe | result |
|---|---|
| `asset-clip format` equal to sequence grid | accept ✅ |
| `asset-clip format` 1/30 vs sequence 1001/30000 | reject, names both rates, refuses to resample ✅ |
| `spine format` 30 vs 29.97 | reject ✅ |
| asset `format` mismatched with sequence | reject ✅ |
| unresolved `format` on clip or asset, unresolved `ref` | reject, names the id ✅ |

**4. Nonzero spine coordinate and lane** — `lane="0"` accepted as "inside the parent"; `lane="1"` and
`lane="-1"` rejected as connected clips; `spine lane="1"` and a nonzero `spine offset` rejected as
connected-storyline coordinates; a `lane` attribute on `gap` rejected (also DTD-invalid, since gap has
no lane).

**5. Metadata and defaults** — `note`, `marker`, `metadata` children accepted; `adjust-transform`
rejected; omitted `offset`/`start` resolve to exactly `0`, confirmed on the parsed model; omitted asset
`name` accepted as optional.

**6. `hasAudio` explicit 0/1, `hasVideo` lexical-only** — this is where the earlier schema work pays
off, and the probes confirm the code matches the disposition exactly:

| probe | dtd | result |
|---|---|---|
| `hasAudio="1"` / `"0"` | valid | accept ✅ |
| `hasAudio="true"` / `"yes"` / `"2"` / `""` | **valid** | reject, "must be 0 or 1" ✅ |
| `hasAudio` omitted | **valid** | reject, "explicit 0 or 1 required in this subset" ✅ |
| `hasVideo="1"` / `"0"` | valid | accept ✅ |
| `hasVideo="true"` | valid | reject ✅ |
| `hasVideo` omitted | valid | accept, no modeled field ✅ |

Every one of the rejected `hasAudio` spellings is schema-valid, because the attribute is `CDATA`. That
is precisely the gap the semantics review predicted, and it is closed lexically without inventing a
default. `hasVideo` is checked when present and carries no model field, matching the refinement.

---

## False-acceptance sweep — no further findings

`version="1.13"` rejected by name; connected `video` child, nested `spine`, and `transition` rejected
with distinct reasons; duplicate resource id rejected; two `project` elements and two `sequence`
elements rejected on cardinality; `.fcpxmld` bundle resolved to `Info.fcpxml` with both paths retained;
a non-FCPXML suffix rejected; `srcEnable="audio"` rejected as outside the subset and `srcEnable="none"`
rejected as an invalid value; `enabled="0"` and `srcEnable="video"` accepted and retained rather than
dropped.

Clock and identity semantics check out on the parsed models:

- **Repeated instances** of one asset yield two items with **distinct `instance_id`** and a **shared
  `resource_scoped_id`** — the property P4 depends on for instance-scoped replacement.
- **Identity is scoped to the input document**: changing unrelated bytes changes both the scoped
  resource id and the instance ids, so neither is a cross-document key.
- **`raw_xml` is byte-exact**, which is the basis for P1-05's identity roundtrip.
- **Nonzero epochs** work end to end: with `tcStart` and `asset.start` both nonzero, two clips produce
  project-elapsed `0` and `1001/100`, matching
  `input_project_elapsed = clip.offset − sequence.tcStart`.
- Off-grid `offset`, overlapping items, reversed order, and a timeline range outside
  `[tcStart, tcStart+duration)` are each rejected with the offending values named.

## Method note

Three of my probes initially reported failures that were my own test errors, not parser defects: a
`10s` duration that is genuinely off-grid at 1001/30000, two multi-item storylines left with a
one-clip sequence duration, and a nonzero-`tcStart` case whose clip sat before `tcStart`. In each case
the parser was right and my document was wrong; I corrected the probes and re-verified rather than
reporting them. Worth recording because in all three the parser's rejection message named the exact
values, which is what made the test error obvious.

## Recommendation

Approve `05b6e3e` once the empty-`media-rep` acceptance is closed. Nothing else in the sweep argues
against this SHA, and the six prior blockers are properly fixed rather than papered over.

No live Final Cut, media or model acceptance is claimed or implied; DTD validation here was `xmllint`
against the installed schema on synthetic temp XML only.

---

## Root disposition and a framing correction

Root accepted the bounded non-empty `media-rep` check, with empty and metadata-only negative tests and
a proxy-only positive control, and routed it to Sol for a same-Dispatch GLM correction. All other
reviewed behaviour is closed.

**Framing correction, which I should have made myself.** I justified the finding partly as "DTD-invalid
input accepted", and that reads as asking `parse_project` to duplicate the schema. It should not.
`parse_project` is a **semantic subset parser**, and P1-04 owns general schema validation — that
separation is the whole reason the xmllint boundary exists as its own task. The correct justification
is narrower and stands on its own: at least one `media-rep` is a **structural prerequisite** for
everything downstream, since P1-03 cannot resolve media from `media_reps=()`, and catching it here is
cheap and reports the XML location. Where I cited `xmllint` above, read it as evidence that the shape
is genuinely malformed rather than a subset choice — not as a claim that this parser owes schema
validation.

Next review is scoped to the small follow-up diff at Sol's returned SHA, not another exploratory sweep.

---

## Follow-up diff review — `b96684e041a02825fd0aa0ed43c46fa1363c0308`

**PASS. I approve this exact SHA for integration.**

Diff relative to `05b6e3e` is exactly the accepted correction: `tests/project/test_fcpxml.py` (+43/−1)
and `vlogsrt/project/fcpxml.py` (+10/−1). Nothing else. Scoped to the follow-up as instructed — no
second exploratory sweep.

**The change.** `_parse_asset` now resolves `media_reps` before constructing the resource and rejects an
empty result with the element path and a reason naming the `media-rep+` content model. The reason text
also draws the line root asked for: it says the *file's* readiness remains P1-03's gate, so the message
cannot be misread as this parser taking on schema validation.

**Verified by probe, not by reading:**

| probe | dtd | result |
|---|---|---|
| zero `media-rep` | **invalid** | reject ✅ |
| `metadata`-only, no `media-rep` | **invalid** | reject ✅ |
| proxy-only positive control | valid | accept, `media_reps=['proxy']` ✅ |
| original + proxy | valid | accept, `['original','proxy']` ✅ |
| baseline original-media | valid | accept ✅ |

Rejection quality: `location=/fcpxml[1]/resources[1]/asset[1]`, reason naming `media-rep+` and
deferring file readiness to P1-03.

**No regression.** Spot-checked four previously verified behaviours — omitted `hasAudio` still
rejected, `kind="original"` still rejected, `audioStart` still rejected, `lane="1"` still rejected.
Suites at this SHA: `tests/project/test_fcpxml.py` **139 passed**, full suite **987 passed**, invariant
checker **19 groups, exit 0**, worktree clean.

One consequential edit worth naming: the pre-existing `test_has_audio_zero_is_preserved` fixture had an
asset with no `media-rep` and was updated to include one. That is a necessary consequence of the new
rule rather than scope creep — the old fixture was itself DTD-invalid — and the assertion it makes is
unchanged.

Nothing further from me on P1-02.
