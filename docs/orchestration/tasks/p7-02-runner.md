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
    unestimated_resource_ids: tuple[str, ...]
    unpriced_components: tuple[str, ...]
    limitations: tuple[str, ...]
    currency: str | None
    estimated_cost: Decimal | None

@dataclass(frozen=True, slots=True)
class SourceDurationEstimate:
    resource_scoped_id: str
    source_id: str
    seconds: Fraction
    provenance: str

@dataclass(frozen=True, slots=True)
class CancellationToken:
    path: Path

@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    stage: Literal["inspect", "analyze", "plan", "export", "captions", "complete", "cancelled", "error"]
    revision: str
    completed_request_ids: tuple[str, ...]
    pending_request_ids: tuple[str, ...]

def estimate_cost(inputs: ProjectDocument, media: Sequence[ResolvedMedia], references: Sequence[PcmReference], duration_estimates: Sequence[SourceDurationEstimate], analyses: Sequence[SourceAnalysis], boundaries: Sequence[BoundaryRequest], vision_requests: Sequence[VisionRequest], cfg: ProjectConfig) -> CostEstimate: ...
def run_stage(stage: str, workspace: Path, *, allow_network: bool, cancellation: CancellationToken, clients: Mapping[str, object]) -> RunCheckpoint: ...
```

- Estimate unique **remaining** full-source audio plus repair/verify/boundary audio and images before any call; deduplicate by content, policy, and exact request/cache keys rather than path, stem, resource position, or duration alone. Reuse a completed exact-key analysis as zero remaining work, but never discard its persisted evidence.
- Cold-run source quantity comes only from a validated matching `PcmReference` (`sample_count/sample_rate`) or an explicit `SourceDurationEstimate` joined by both resource-scoped and strong source identity. A duration estimate must be positive, finite, and have nonblank provenance. Never infer full-file duration from the FCPXML retained clip duration, fabricate PCM/`SourceTrace`, or treat missing measurement as zero/free. Declared-no-audio alone is zero-work; an audible resource without either measurement is listed in deterministic `unestimated_resource_ids` and makes `estimated_cost=None`.
- Multiply quantities only by the explicit optional Decimal assumptions in `ProjectConfig`, retaining source, repair/verify, boundary, and vision as separate components. A nonzero component with no applicable price is listed in `unpriced_components` and makes `estimated_cost=None`; a missing price for a zero-work component does not block a priced total. `currency` is the configured label when any price participates. Use Decimal arithmetic without float conversion.
- `limitations` explicitly states that estimates are budgeting evidence, not provider invoices: future repair/retry demand and provider billing units may be unknowable before responses. Do not hide a known quantity merely because it is unpriced.
- Before P3 aggregation, the runner maps each resolved resource exactly once. It constructs `UnavailableSourceAudio(status="no_audio", reason_code="declared_no_audio")` for a declared-no-audio resource, or catches a contextual P2 probe/extraction/reference failure and constructs the matching resource-scoped `status="error"` variant. P2 never imports P3 or constructs these records; no STT/VAD is scheduled until a real reference exists.
- Declared-no-audio is completed and non-billable. A pre-PCM error is a retryable failed attempt: explicit resume may rerun preprocessing under policy and must not reuse it as completed success forever. Operational failure after a real reference remains an error `SourceTrace` with real provenance.
- Cost estimation accepts the full `Sequence[SourceAnalysis]`. Only `UnavailableSourceAudio(status="no_audio")` is intrinsically zero work; a pre-PCM error is pending retry work and requires a measured/estimated duration, while a post-PCM error retains its real reference provenance and remaining request work. Resource-scoped unavailable records cannot suppress a valid shared content/analysis-key trace for another resource with equal bytes.
- Before plan/export/captions reuse, require the persisted P4/P6 `unavailable_instances` to agree exactly with the current resource-scoped analyses and locks. An unavailable instance schedules no boundary/VAD/vision/edit removal; declared-no-audio may complete as non-billable keep/review, while `stream_unavailable` or `pcm_unavailable` remains pending/error and prevents a `complete` checkpoint or final-accepted package until a successful retry replaces that exact resource disposition. Unrelated healthy resources continue.
- Only `analyze` (and full `run` while executing its analyze stage) may use clients/network, and only with `allow_network=True`. `inspect`, `plan`, `export`, `captions` hard-fail any attempted client use.
- Cancellation stops scheduling new requests, lets in-flight result checkpoint if it returns, then records pending IDs. Resume reuses completed exact-key artifacts and schedules only pending/missing.
- 429/timeout/malformed/model-missing errors persist per request and preserve affected content; lost response has no exactly-once billing claim.
- Stale/mixed revisions fail before scheduling.
- Unknown analysis variants/schema revisions and malformed resource/content identity reject before scheduling or client construction.

## Implementation steps

1. Write failing fake-client tests for cold run from measured PCM, cold run from explicit provenance-tagged duration, warm completed exact-key reuse, partial completed/pending work, unknown duration, unknown pricing, zero-work missing-price behavior, Decimal/currency totals, content+policy+request dedup over full `Sequence[SourceAnalysis]`, every stage network gate, cancellation before/between/in-flight requests, resume, declared-no-audio zero-call completed reuse, retryable pre-PCM failures, equal-content/differing-`has_audio` resources, post-PCM error provenance, unknown variant/schema pre-call rejection, 429/timeout/malformed/missing model, stale plan, and duplicate billing disclaimer state. Add exact unavailable-instance/analysis consistency, zero boundary/VAD/vision/edit scheduling, missing/contradictory metadata refusal, declared-no-audio completion, retryable failure preventing `complete`/final acceptance, and successful resource-scoped retry without disturbing an equal-content healthy sibling.
2. Implement deterministic stage/checkpoint scheduling over workspace interfaces.
3. Run focused P7/P6/invariant/full checks; commit owned files.

## Acceptance

Fake call counts exactly match pending request keys; cold/warm/partial estimates expose every unknown/unpriced component without calling it zero; offline stages make zero calls; cancellation/resume loses no completed artifact and schedules no post-cancel work; failures keep affected blocks/ranges. Unavailable metadata grants no removal authority, and retryable pre-PCM failure cannot become a complete/final run. `make check` passes.

## Failure route

Report stage/request IDs, expected/actual call count/checkpoint, cancellation point, revision, base/tests/diff. Any plan/export/captions network call is a hard escalation. Repeated failures use diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, call-count/cancel/resume matrix, and `ready-to-integrate`.
