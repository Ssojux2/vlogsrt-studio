# Task P6-04: publish the original-linked FCPXML and SRT package atomically

## Authority

Binding spec `Input and output`, P6 export, and `Evidence at handoff`; depends on P6-03, P1 roundtrip/validation, and P3 SOURCE SRT. Opus phase review precedes Astra's P6 merge.

## Baseline and ownership

- Fresh `fcp-p6-export` worktree from exact P6-03-integrated SHA.
- Create `vlogsrt/project/export.py`, `tests/project/test_export.py`; modify `vlogsrt/project/fcpxml.py` only for plan application writer. Synthetic packages are inline or generated under pytest `tmp_path`; no fixture/golden file is added or modified.
- No network/workspace orchestration/CLI/UI, no source input rewrite, and no external fixture changes.

## Contract

```python
@dataclass(frozen=True, slots=True)
class ExportPackage:
    plan_revision: str
    fcpxml: Path
    edit_srt: Path
    source_srt: Path
    project_trace: Path
    review: Path

def render_edit_fcpxml(document: ProjectDocument, media: Sequence[ResolvedMedia], plan: EditPlan, captions: EditCaptionSet) -> bytes: ...
def publish_package(destination: Path, document: ProjectDocument, media: Sequence[ResolvedMedia], plan: EditPlan, edit_captions: EditCaptionSet, source_srt: str, review: Mapping[str, object], *, dtd_path: Path | None = None) -> ExportPackage: ...
```

- Output names exactly `VLOG_AI_EDIT.fcpxml`, `VLOG_AI_EDIT.srt`, `VLOG_AI_SOURCE.srt`, `project_trace.json`, `review.json`; every structured artifact carries the same plan revision. `VLOG_AI_SOURCE.srt` is `BoundaryProjection.source_srt`, the input project timeline before automatic edit removal, not a concatenation of per-asset zero-based files.
- Create new project/event identity, `tcStart=0`, original-linked media refs, source order, supported static attributes/resources/UID, existing gaps, and disabled durations. XML-local resource IDs may be preserved or deterministically regenerated only as references inside the new document; never treat input `r2`-style IDs as durable media identity or cross-document rebind keys. Never link output to proxy as original.
- Split/ripple clips only at exact plan frame boundaries; preserve asset-start anchored source coordinates and valid refs/order/non-overlap/durations.
- Add `<marker>` children only for generated, explicitly located `ReviewIssue` values; never parse human prose for coordinates. Create markers **after** splitting clips and select exactly one final owner with the same `instance_id` whose retained `source_frames` contain the same asset-start-anchored `source_frame` under half-open `[start, end)` semantics (a cut-boundary frame belongs to the later slice). The marker XML clock is `asset.start + source_frame * frame_duration`, not elapsed time since the trimmed/split clip starts; never subtract the final clip start to make a zero-relative marker timestamp, and require that XML coordinate to lie inside the final owner clip's half-open `[start, start + duration)`. Prefix value with `VLOG_AI_REVIEW:`, keep duration exactly one source frame and wholly within the owner, and insert it at the exact FCPXML 1.14 DTD child position. If the issue's source frame was deleted, emit no relocated marker; preserve the issue with its original provenance in `review.json`.
- Clock evidence: Apple's official [marker association example](https://developer.apple.com/documentation/professional-video-applications/associating-ratings-keywords-markers-and-metadata-with-media) places markers at source coordinates 6s/8s inside a clip whose `start` is 5s, and states that marker duration is one video frame. The installed FCPXML 1.14 DTD entries at lines 516 and 686–691 fix marker child order and require `start`/`value`; tests must exercise those exact local rules.
- Before publication, always run the existing supported-structure parser and P1-04 `validate_xml` on the generated FCPXML, passing `dtd_path` through when supplied. Invalid XML returns/fails publication with its bounded diagnostic; missing xmllint or requested DTD raises `XmlValidationUnavailable` and cannot publish a successful package.
- Write all five files into a new immutable revision directory, fsync/close, and validate its hashes/revisions before publication. Publish by one atomic replacement of a `current` symlink/pointer to that complete revision; returned `ExportPackage` paths resolve inside the selected immutable revision so one consumer cannot observe a mixed set while the pointer changes.
- First publish creates the managed revision root and pointer. If `destination` is an existing unmanaged file/directory or has an invalid pointer/layout, fail without renaming, deleting, or overwriting user content. Concurrent writers use exclusive revision creation plus an atomic exclusive publication lock covering the final pointer read/check/switch; a conflicting writer fails deterministically without touching `current`. Remove only a lock token owned by the current call. An existing/stale-looking lock is a conflict requiring explicit recovery — never steal it solely by age and never describe independent read plus replace as compare-and-swap. Never claim that replacing one nonempty directory with another is atomic.
- Draft unresolved boundary/analysis warnings stay explicit and prevent a `final_accepted=true` field.

## Implementation steps

1. Write failing tests for original/proxy refs, new document identity/tcStart, reuse of raw IDs across unrelated inputs without cross-binding, repeated/disabled/gap/order, exact cuts, post-split marker ownership at both sides of a boundary, nonzero `asset.start` plus a trimmed final owner proving marker `start` stays in the asset-anchored source clock, deleted-location review without marker relocation, DTD child order, mixed revision, validation unavailable/invalid refusal, first publish, unmanaged destination refusal, failure before pointer switch, stale prior revision, live/stale-lock refusal, concurrent-writer conflict under the exclusive lock, reader consistency across a switch, and all five names.
2. Implement tree-copy plan writer preserving untouched attributes/time strings and atomic package publication.
3. Reparse/validate generated XML, run all P6/P1 tests, invariants, full check; commit owned paths.

## Acceptance

Synthetic package reparses with zero dangling refs/overlap/zero-frame identity mismatch, uses originals, shares one revision, and actually shortens the safe fixture. Failure before switch leaves the prior pointer and complete revision intact; readers resolve all five returned paths from one revision, and an unmanaged destination is untouched. `make check` passes. Actual FCP import/relink/playback remains `PENDING_HUMAN`.

## Failure route

Report artifact/revision, XML element path/ref/time, validator diagnostic, staging state, base/tests/diff. Never publish a partial/mixed package or flatten unsupported input. Structural/source-loss errors escalate immediately.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, package hashes/revision/duration, DTD status, pending live gate, and `ready-to-integrate`.
