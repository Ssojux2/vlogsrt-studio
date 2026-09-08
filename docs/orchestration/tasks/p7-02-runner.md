# Task P7-02: orchestrate budgeted resumable project analysis

## Authority

Binding spec `Persistence, CLI and UI`; depends on P7-01 and integrated P6 pipeline. This is product orchestration, distinct from Orca orchestration.

## Baseline and ownership

- Fresh `fcp-p7-runner` worktree from exact P7-01-integrated SHA.
- Create `vlogsrt/project/runner.py`, `tests/project/test_runner.py`.
- No CLI/UI, live API execution, workspace contract changes, or fixture recording.

## Contract

```python
@dataclass(frozen=True, slots=True)
class CostEstimate:
    source_audio_seconds: Fraction
    repair_verify_seconds: Fraction
    boundary_audio_seconds: Fraction
    vision_images: int
    estimated_cost: Decimal

@dataclass(frozen=True, slots=True)
class CancellationToken:
    path: Path

@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    stage: Literal["inspect", "analyze", "plan", "export", "captions", "complete", "cancelled", "error"]
    revision: str
    completed_request_ids: tuple[str, ...]
    pending_request_ids: tuple[str, ...]

def estimate_cost(inputs: ProjectDocument, traces: Sequence[SourceTrace], boundaries: Sequence[BoundaryRequest], vision_requests: Sequence[VisionRequest], cfg: ProjectConfig) -> CostEstimate: ...
def run_stage(stage: str, workspace: Path, *, allow_network: bool, cancellation: CancellationToken, clients: Mapping[str, object]) -> RunCheckpoint: ...
```

- Estimate unique full-source audio plus repair/verify/boundary audio and images before any call; deduplicate by strong request key.
- Only `analyze` (and full `run` while executing its analyze stage) may use clients/network, and only with `allow_network=True`. `inspect`, `plan`, `export`, `captions` hard-fail any attempted client use.
- Cancellation stops scheduling new requests, lets in-flight result checkpoint if it returns, then records pending IDs. Resume reuses completed exact-key artifacts and schedules only pending/missing.
- 429/timeout/malformed/model-missing errors persist per request and preserve affected content; lost response has no exactly-once billing claim.
- Stale/mixed revisions fail before scheduling.

## Implementation steps

1. Write failing fake-client tests for cost dedup, every stage network gate, cancellation before/between/in-flight requests, resume, 429/timeout/malformed/missing model, stale plan, and duplicate billing disclaimer state.
2. Implement deterministic stage/checkpoint scheduling over workspace interfaces.
3. Run focused P7/P6/invariant/full checks; commit owned files.

## Acceptance

Fake call counts exactly match pending request keys; offline stages make zero calls; cancellation/resume loses no completed artifact and schedules no post-cancel work; failures keep affected blocks/ranges. `make check` passes.

## Failure route

Report stage/request IDs, expected/actual call count/checkpoint, cancellation point, revision, base/tests/diff. Any plan/export/captions network call is a hard escalation. Repeated failures use diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, call-count/cancel/resume matrix, and `ready-to-integrate`.
