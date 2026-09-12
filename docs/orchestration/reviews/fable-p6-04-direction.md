# P6-04 direction review (Fable 5.1, read-only) — candidate defe23f on base 924b5d1

Evidence: `vlogsrt/project/export.py` (1013 lines) and `tests/project/test_export.py` (1184 lines)
read in full; `edit.py` (`EditPlan`, `build_edit_plan`, `validate_edit_plan`, `_plan_revision`,
`_require_decision_linkage`, `_applied_candidate`, `_unavailable_issues`), `captions.py`
(`EditCaptionSet`, `_edit_representability`), `boundary.py` (`BoundaryProjection`,
`UnavailableInstance`), `vision/policy.py` (`VisionDecision`, `parse_decision`,
`_error_decision`), `vad/guard.py` (`InstanceGuard`), the `check_invariants.py` and `fcpxml.py`
diffs, the binding spec, my P6-02 §5 and P6-03 P7-gap sections, and the P6-04, P7-01, P7-02,
P7-03, P7-04 briefs. All 46 export tests and `check_invariants.py` pass on the candidate
(main checkout's 3.12 venv, `PYTHONPATH` at this worktree). Every claim marked *reproduced*
was run with `scratchpad/probe.py` against the candidate. Nothing in the repo was edited.

## 방향 판정: SOUND_WITH_CONCERNS — two of the concerns are P7 blockers

The contract shape is right and every P7 consumer can read it by lookup. But the two seams
the direction asked me to press hardest are the two that fail: **`final_accepted` is `true`
on a package whose vision analysis never happened or wholly failed** (rule 9, at the point
where it reaches the user), and **a killed or resumed writer can never republish the same
plan** (P7-02 resume, P7-01 idempotency). Both fixes are small and local to `export.py`.

## 1. 예측 착지

| Predicted (P6-02 §5, P6-03 P7 gap, brief Fable section) | Landed |
|---|---|
| Typed signatures, no `source_srt: str`, no `review: Mapping` | yes (`export.py:255`, `:590`; return is `str`, brief's old `bytes` is moot) |
| `final_accepted` computed only here, caller cannot inject | yes (`_review_payload`, no parameter) |
| `render_edit_fcpxml` calls `validate_edit_plan` first, refuses on any finding | yes (`_require_renderable`, test 441) |
| One `asset-clip` per **run**, gaps from `plan.gaps`, `m × fd`, `tcStart=0`, no ripple arithmetic | yes (`_rebuild_spine`, `_emit_run`; tests 555/565/583) |
| Marker owner = emitted run, half-open, asset-anchored clock, no relocation | yes (`_marker_owner`, `_attach_markers`; tests 600/618/636) |
| In-process-plan-only rule, no deserializer in the package | yes as convention (module docstring; no decoder exists) |
| Public record encoder pair, `"num/den"`, `[start, end]` | yes (`plan_record`, `edit_captions_record`) |
| `source.unresolved_cue_ids` in `final_accepted` | yes (`_final_blockers`) |
| Source identity triple `source == plan == document` | yes (`_require_source_identity`) |
| Unavailable records exact in both JSON files, retryable blocks final, declared-no-audio does not | yes (tests 878/971/980) |
| Invariant 20 widened to `export.py` | yes (`REMOVAL_LAYER_MODULES`) |

**What did not come, in order of weight:**
- `final_accepted` looks at four blockers only. The plan's own review codes and the state of
  the vision decisions are not consulted (§5).
- Same-revision republish is a hard conflict even when the prior attempt died before the
  pointer moved (§3, §4).
- The revision directory is deleted on *any* exception, including one raised after the
  pointer already points at it (§4).
- `VisionDecision` encoding is private (`_decision_record`); P7-01 must persist decisions at
  analyze time, long before any trace exists (§2).

## 2. P7 공급 가능성

**P7-01 read-back and validation: sufficient, with one guard to add.** *Reproduced*:
`plan_record` → `_json_bytes` → `json.loads` → hand decoder → `plan_record` → `_json_bytes`
is byte-identical; the decoded `EditPlan` compares equal to the original and
`validate_edit_plan` returns no findings; two builds of the same scene give the same
revision and the same record bytes. `revision`, `input_sha256`, `config_sha256`, and typed
`unavailable_instances` are all top-level in both `project_trace.json` and `review.json`, so
P7-01 validates them without decoding the plan body. `_json_bytes` is canonical (sorted
keys, `allow_nan=False`, trailing newline), so P7-01's "byte-identical → idempotent" rule can
be applied to these files as written.

The guard: the decode side is trivially writable and, once written, **a decoded plan
renders** (*reproduced*: `replace(decoded, revision="forged")` passes `render_edit_fcpxml`).
The in-process rule is documentation only. P7-01 must decode records into a plain validated
mapping or a record-only summary type, never into `EditPlan`, and `check_invariants.py`
should ban `EditPlan(` construction anywhere under `vlogsrt/` except `edit.py`. Cheap, and it
makes the rule mechanical before P7-01 exists.

Three smaller encoder points:
- `retained` rows are encoded with `reasons: [reason]` to share the run column names. A
  decoder has to know the asymmetry. Encode `reason` as-is or document the choice in the
  docstring; either is fine, but pick one now.
- `decision_record` should be public. P7-01 persists `VisionDecision`s as they return from
  the client (P7-02 "persist per request"); if the only encoder is `_decision_record` inside
  the plan encoder, P7-01 invents a second one — the exact split the pair was meant to avoid.
- `review.json` carries no decisions at all. The vision-failure state of a block is visible
  only in `project_trace.json → plan.decisions[].status`. The human-facing file should list
  pending/errored blocks explicitly (see §5).

**P7-02 budget/cancel/resume:** budget never touches export. Cancellation is cooperative and
export has no yield point, so a token cannot land inside `publish_package`; a SIGINT does
land and the `BaseException` handler removes only this call's revision directory — correct
except for the window in §4. Resume is the problem (§3).

**P7-03 CLI:** `review.json.final_accepted` (bool) plus `final_blockers` (machine codes) is
exactly the shape a CLI can print and map to exit codes (`retryable_unavailable_instances`
and the missing pending-vision blocker → 3 `needs_analysis`). `ExportPackage` paths are
absolute inside the revision directory; `artifacts` in the trace are keyed by file name with
digests, no paths, so the package is relocatable. One thing P7-03 has nowhere to get today:
**stale-lock recovery**. The brief forbids stealing by age and requires "explicit recovery",
but nothing defines it. The lock file holds only a pid. Put pid, hostname, and revision in it
so a P7-03 `--recover-lock` (or a documented manual step) can be informed; do not add
automatic stealing.

**P7-04:** does not consume this package (my P6-03 finding stands). Nothing to add.

## 3. 재개 정합 — the rule and the implementation disagree

The rule is right and rebuild is possible: `build_edit_plan` is deterministic from
(document, partition, guards, decisions, cfg), `partition_blocks` is deterministic from
(document, media, guards, locked, unavailable, cfg), decisions are canonicalised into the
revision by block order (P6-02b), and *reproduced* two independent builds agree byte-for-byte.
So P7-02 `plan`/`export` stages can rebuild offline and compare `revision` against the
persisted record. No conflict *in principle*.

Two things the implementation does not make possible:

1. **Republishing the rebuilt plan after a dead writer.** *Reproduced*: kill the writer
   during `_write_revision` (no `except` runs, as with SIGKILL) → `revisions/<rev>` exists
   with two of five files, no `current`; rebuild the same plan (same revision, by design) →
   `publish_package` refuses with "리비전 디렉터리가 이미 있다". The P7-02 brief says resume
   "reuses completed exact-key artifacts and schedules only pending/missing"; the P7-01 brief
   says "a same-key byte-identical completed artifact is idempotent; different bytes at the
   same key are a conflict". Export contradicts both: the same key is *always* a conflict,
   and `test_same_revision_conflict_is_deterministic` locks that in. The brief sentence
   "exclusive revision creation … a conflicting writer fails deterministically" was written
   for two *live* writers and is being applied to a *dead* one.

2. **What resume has to have persisted to rebuild at all**, which no P7 brief names as an
   artifact kind: `VisionDecision` per request key (encoder private, above), `InstanceGuard`
   per instance (no encoder anywhere; or `VadResult` + `SourceTrace` + boundary plan to
   re-derive through `project_guard`), and the boundary plan's `locked_instance_ids` +
   `unavailable_instances`. P7-01 lists `source_analysis`, `vision_analysis`, `plans`; guards
   fall in none of them. If guards are not persisted, "rebuild from saved analysis" requires
   re-running VAD on every resume, which is offline but is not "reuse".

## 4. 원자성

- **Pointer swap is atomic as claimed.** `os.symlink` to a pid-suffixed staging name, then
  `os.replace` onto `current`: `rename(2)` replaces the link inode, does not follow it, and a
  reader sees either the old or the new target. Returned paths are inside the revision
  directory, not through `current`, so a consumer never straddles a switch (test 1108).
- **Lock is correct for what it covers.** `O_CREAT|O_EXCL` is atomic on local filesystems;
  the lock spans pointer read → validity check → swap; only the owner unlinks it; no age
  stealing (tests 1068/1083). Two writers with *different* revisions serialise rather than
  conflict: both revisions are kept and the later swap wins. That is fine for a
  workspace-driven runner but is not the brief's "conflicting writer fails"; say so.
- **Crash after the switch deletes the published revision.** *Reproduced*: make
  `_fsync_directory(root)` raise after `os.replace` → `except BaseException` runs
  `shutil.rmtree(revision_dir)` → `current` dangles → every later publish is refused with
  "current 포인터가 없는 리비전을 가리킨다". The same path is reached if `lock.unlink()` in
  `finally` raises. Narrow window, deterministic brick. Fix: once the pointer has moved the
  revision is published and must not be removed.
- **Partial revision directories are reachable by name.** Because writing happens directly
  in `revisions/<rev>`, a dead writer leaves a directory that is indistinguishable by name
  from a complete one and blocks the same revision forever (§3). Writing into
  `revisions/.<rev>.<pid>.tmp` and `os.rename`-ing to `revisions/<rev>` only after
  `_write_revision` succeeds makes a complete directory complete by construction (rename of a
  directory onto a nonexistent name is atomic; onto an existing nonempty name it fails
  deterministically, which is the live-writer conflict the brief wants). Then the same
  revision can be idempotent: under the lock, if `revisions/<rev>` exists, verify the five
  digests against the freshly rendered payloads; equal → point `current` at it and return;
  different → conflict. `_require_managed_root` must then tolerate `.*.tmp` entries under
  `revisions/` as staging remnants (it already does at the root level).
- **P7-02 cancellation** never lands inside publish (no yield point); SIGINT does and is
  handled correctly apart from the post-switch window above.

## 5. 규칙 9의 마지막 관문 — `final_accepted` is `true` with the analysis undone

*Reproduced*, all with a plan that passes `validate_edit_plan` and a package that publishes:

| Case | `review.json.review` | `final_accepted` |
|---|---|---|
| A1 no vision decisions at all (nothing analysed) | `[]` | **true** |
| A2 every block `status="error"` (429 / malformed) | `[]` | **true** |
| A3 `decision_inconsistent` (two decisions on a block) | one issue | **true** |
| A4 `guard_overlap` (marker present in the XML) | one issue | **true** |

A1 and A2 are the defect. The spec says "API failures preserve affected block and explicit
review" and "Draft may have explicit unresolved warnings, never labeled final acceptance";
P7-02 treats errored/missing requests as *pending* work to be resumed; P7-03 says "Draft
warnings are never labeled final". Yet a package with zero successful vision calls is
published as final and its review list is empty — the failure is not even *explicit*, it is
only recoverable by reading `trace.plan.decisions[].status`. Root cause is in P6-02
(`_applied_candidate` returns no issue for a missing or `error` decision, contrary to its own
docstring), but the brief made P6-04 the sole owner of `final_accepted`, so the gate belongs
here: a block with no decision or an `error` decision is **pending analysis**, and pending
analysis is a blocker.

A3 and A4 are decisions to pin, not defects, and I recommend blocking on both. A3 is a
model-contract violation of the same class as `error`. A4 is a located marker the human is
being asked to look at; "final" with a review marker in the XML is a contradiction in terms.
The simplest rule that satisfies the spec sentence and the brief's declared-no-audio
exception: **every `captions.review` code except `unavailable_declared_no_audio` is a
blocker, plus `pending_vision_blocks` derived from `plan.decisions` against the plan's
blocks.** `_final_blockers` today enumerates four codes; enumerate the one exception instead.

Everything else at this gate is right: `unresolved_request_ids`, `unresolved_cue_ids`,
SOURCE-collapsed cues re-checked via flags (P6-03b asymmetry honoured), retryable
unavailable, unavailable review pairing by code multiset, source identity triple, and the
five-file digest self-check.

## 6. Opus 브리프 검토

The brief moved my §5 spec faithfully: signatures, run-based clips, marker owner as run,
encoder pair, `unresolved_cue_ids`, in-process rule, and the trust statement all read as I
wrote them, and the implementation followed the brief exactly. Three holes are the brief's,
and the implementation inherited each one:

1. **It enumerated `final_accepted` blockers** (boundary/analysis warnings, `subms_collapsed`,
   retryable unavailable) and never mentioned the plan's own review codes or the vision
   decision state. An enumerated allow-list of blockers is the wrong shape for a "never
   labeled final with anything unresolved" rule; it should have named the single exception.
   §5 is the direct consequence.
2. **"Exclusive revision creation … conflicting writer fails deterministically"** was written
   without the P7-01 idempotency sentence next to it, so the implementer (correctly) made
   same-revision a permanent conflict and wrote a test for it. The brief should have said:
   stage under a temporary name, rename to the revision name atomically, complete + identical
   is idempotent, live writers conflict on the rename.
3. **"Explicit recovery" for a lock is required but never defined**, and nothing in the P7-03
   brief takes it up. The brief should name the recovery affordance and the lock contents.

Minor: the brief's rule "remove only a lock token owned by the current call" is honoured, but
the brief did not say "never remove a revision the pointer already names", which is the
§4 crash window.

## 최소 수정 명세 (P6-04b, `export.py` + `test_export.py`; two brief edits)

1. `_final_blockers`: replace the enumerated review codes with "every `captions.review` code
   except `unavailable_declared_no_audio`" (codes deduplicated, sorted), and add
   `pending_vision_blocks` when any block of the plan has no decision or a decision with
   `status == "error"`. `review.json` gains `pending_blocks: [{block_id, status, reason}]` so
   the failure is explicit in the human-facing file. Tests: A1, A2, A3, A4 → `false`;
   declared-no-audio alone still `true` (existing test 971 stays green).
   Note: `EditPlan` carries no blocks; "block has no decision" needs the partition. Either
   `publish_package` receives the partition (it is in the call chain by the in-process rule),
   or P6-02b adds a review issue for missing/`error` decisions and export sees it through
   `captions.review`. The second is cleaner and fixes the P6-02 docstring lie; I recommend it,
   with the export-side rule in (1) making it a blocker automatically.
2. `publish_package`: set `switched = True` after `os.replace` inside `_switch_pointer`
   (return it), and skip `shutil.rmtree` when switched. Test: `_fsync_directory(root)` raises
   after the swap → revision directory survives, `current` resolves, next publish succeeds.
3. `_create_revision` → stage in `revisions/.<rev>.<pid>.tmp`; `_write_revision` writes
   there; after validation, `os.rename` to `revisions/<rev>` **under the publish lock**; on
   `FileExistsError`/`ENOTEMPTY`, compare the five digests with the existing directory's
   `project_trace.json → artifacts` and re-hash its files: identical → idempotent (point
   `current`, return the package, remove own staging), different → conflict. Extend
   `_require_managed_root` to tolerate `.*.tmp` under `revisions/`. Tests: killed writer then
   same-plan retry → success; complete identical revision → idempotent, pointer unchanged if
   already there; existing revision with different bytes → conflict, untouched. Rewrite
   `test_same_revision_conflict_is_deterministic` as the different-bytes case.
4. Lock file content: `pid`, `hostname`, `revision`, ISO timestamp — evidence for explicit
   recovery, never a stealing input.
5. Encoders: make `decision_record` public; encode `retained[].reason` as `reason` (or
   document the `reasons` wrap); add `check_invariants.py` check "no `EditPlan(` call outside
   `vlogsrt/project/edit.py`" (tests excluded).
6. Briefs: P7-01 adds artifact kinds for persisted `VisionDecision` (via `decision_record`)
   and `InstanceGuard` (or its inputs), decodes plan records into a record type, never
   `EditPlan`; P7-02 states "`plan`/`export` rebuild via `build_edit_plan` and compare
   `revision`; same-revision publish is idempotent"; P7-03 owns lock recovery and maps
   `pending_vision_blocks`/`retryable_unavailable_instances` to exit 3.

Nothing above changes the XML, the SRT bytes, the plan, ids, or thresholds. The existing 46
tests stay green except the one rewritten in (3).
