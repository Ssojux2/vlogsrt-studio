# Task P5-05: validate saved vision decisions without granting edit authority

## Authority

Binding spec `Visual scoring and deterministic roughcut`; depends on P5-04. This task builds/parses requests and adds the sole vision network call inside `transcribe.py`, but tests remain fake/offline.

## Baseline and ownership

- Fresh `fcp-p5-vision-policy` worktree from exact P5-04-integrated SHA.
- Create `vlogsrt/project/vision/policy.py`; modify `vlogsrt/project/vision/__init__.py`, `vlogsrt/transcribe.py`, `tests/test_transcribe.py`; create `tests/project/test_vision_policy.py`.
- No live calls, block partition/edit application, model fallback, VAD, or fixtures/API edits.

## Contract

```python
@dataclass(frozen=True, slots=True)
class VisionCandidate:
    candidate_id: str
    keep_start: Fraction
    keep_end: Fraction

@dataclass(frozen=True, slots=True)
class VisionRequest:
    block_id: str
    frame_ids: tuple[str, ...]
    candidates: tuple[VisionCandidate, ...]
    adjacent_context: str

@dataclass(frozen=True, slots=True)
class VisionDecision:
    block_id: str
    choice: str
    reason: str
    evidence_frame_ids: tuple[str, ...]
    confidence: float
    status: Literal["accepted", "keep_all", "error"]

def build_payload(request: VisionRequest, samples: Sequence[FrameSample], cfg: ProjectConfig) -> dict[str, object]: ...
def parse_decision(request: VisionRequest, raw: Mapping[str, object], cfg: ProjectConfig) -> VisionDecision: ...
def create_vision_client(*, api_key: str, base_url: str | None) -> object: ...  # transcribe.py
def request_vision(client: object, *, model: str, payload: Mapping[str, object], online: bool = False) -> dict[str, object]: ...  # transcribe.py
```

- Payload uses Base64 `image_url` data, <=16 unique frames, 1 fps/block endpoints/three candidate centers as supplied, plus adjacent scene/transcript context. It exposes no XML path, mute, or edit authority.
- Choice must be one known candidate ID, `keep_all`, or `uncertain`; evidence IDs must be sampled; confidence finite `[0,1]`; unknown/NaN/malformed/exception becomes error/keep-all review.
- Only accepted candidate with confidence >=0.8 and an explicit repetitive ordinary B-roll reason can later be applied. Unique events or uncertainty normalize to `keep_all` regardless of numeric confidence.
- Model is exactly `cfg.vision_model`; no silent replacement. 429/timeout/call failures remain explicit error artifacts.
- `request_vision` is the only added vision-call boundary and accepts a provided vision client. It fails closed unless `online=True`. `create_vision_client` accepts explicit values and never reads environment/files itself; the later authorized online composition resolves P5-00's distinct vision credential names and never reuses legacy STT helpers. No key value is read in this task, and no network occurs during tests or inspect/plan/export/captions operations.

## Implementation steps

1. Write failing payload/parser tests for every valid choice and malformed/unknown/NaN/evidence/confidence/unique-event/error case; mock client call/model. Add behavioral network-spy tests proving default/offline requests and inspect/plan/export/captions paths issue zero client calls, without imposing a blanket transitive-import ban.
2. Implement strict pure builder/parser and minimal transcribe network adapter without importing `vlogsrt.project` into legacy source.
3. Run focused tests, P0 invariants, full check; commit owned files.

## Acceptance

Malformed and failed responses always preserve block (`keep_all`) with review; known high-confidence ordinary B-roll candidate alone becomes accepted. Tests prove explicit offline gating, separate provider configuration, zero live calls, and no model fallback. `make check` passes. Live account capability remains `PENDING_HUMAN`.

## Failure route

Report sanitized raw shape, request candidate/frame IDs, expected/actual normalized decision, client exception, base/tests/diff. Never loosen schema or assume API success. Repeated failures use diagnostics; live account errors are external blockers.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, decision matrix, zero-live-call evidence, pending live capability, and `ready-to-integrate`.
