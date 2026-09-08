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
def publish_package(destination: Path, document: ProjectDocument, media: Sequence[ResolvedMedia], plan: EditPlan, edit_captions: EditCaptionSet, source_srt: str, review: Mapping[str, object]) -> ExportPackage: ...
```

- Output names exactly `VLOG_AI_EDIT.fcpxml`, `VLOG_AI_EDIT.srt`, `VLOG_AI_SOURCE.srt`, `project_trace.json`, `review.json`; every structured artifact carries the same plan revision. `VLOG_AI_SOURCE.srt` is `BoundaryProjection.source_srt`, the input project timeline before automatic edit removal, not a concatenation of per-asset zero-based files.
- Create new project/event identity, `tcStart=0`, original-linked media refs, source order, supported static attributes/resources/UID, existing gaps, and disabled durations. XML-local resource IDs may be preserved or deterministically regenerated only as references inside the new document; never treat input `r2`-style IDs as durable media identity or cross-document rebind keys. Never link output to proxy as original.
- Split/ripple clips only at exact plan frame boundaries; preserve asset-start anchored source coordinates and valid refs/order/non-overlap/durations.
- Add `<marker>` children only for generated review items, at exact source-relative positions, value prefixed `VLOG_AI_REVIEW:`; keep marker duration one source frame and within its clip. Validate against local DTD when supplied and structural validator always.
- Stage all five files in a sibling temporary directory, fsync/close, validate hashes/revisions, then atomically replace the destination package. Failure publishes none and preserves prior complete package.
- Draft unresolved boundary/analysis warnings stay explicit and prevent a `final_accepted=true` field.

## Implementation steps

1. Write failing tests for original/proxy refs, new document identity/tcStart, reuse of raw IDs across unrelated inputs without cross-binding, repeated/disabled/gap/order, exact cuts, markers, mixed revision, atomic failure, stale previous package, and all five names.
2. Implement tree-copy plan writer preserving untouched attributes/time strings and atomic package publication.
3. Reparse/validate generated XML, run all P6/P1 tests, invariants, full check; commit owned paths.

## Acceptance

Synthetic package reparses with zero dangling refs/overlap/zero-frame identity mismatch, uses originals, shares one revision, and actually shortens the safe fixture. Atomic failure test leaves the prior package intact. `make check` passes. Actual FCP import/relink/playback remains `PENDING_HUMAN`.

## Failure route

Report artifact/revision, XML element path/ref/time, validator diagnostic, staging state, base/tests/diff. Never publish a partial/mixed package or flatten unsupported input. Structural/source-loss errors escalate immediately.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, package hashes/revision/duration, DTD status, pending live gate, and `ready-to-integrate`.
