# Task P3-01: distinguish successful empty transcription from failures

## Authority

Binding spec `Source analysis, clocks, identity`; depends on Astra-integrated P1 and P2. This source-boundary change must preserve the legacy runtime while supplying an explicit adapter contract.

## Baseline and ownership

- Fresh `fcp-p3-transcription-outcome` worktree from the exact joint P1/P2 integration SHA.
- Modify `vlogsrt/transcribe.py`, `vlogsrt/pipeline.py`, `tests/test_transcribe.py`, `tests/test_pipeline.py`.
- No project adapter, fixture edits, configuration threshold changes, network calls, or protected paths.

## Contract

```python
class EmptyTranscript(RuntimeError): ...
class MalformedTranscript(RuntimeError): ...

@dataclass(frozen=True, slots=True)
class ChunkCoverage:
    index: int
    offset: float
    duration: float
    segment_count: int

@dataclass(frozen=True, slots=True)
class TranscriptionOutcome:
    status: Literal["success", "empty", "error"]
    segments: tuple[Segment, ...]
    coverage: tuple[ChunkCoverage, ...]
    error: str | None

def transcribe_outcome(transcriber: Transcriber, job: TranscriptionJob, cfg: Config) -> TranscriptionOutcome: ...
def require_nonempty(outcome: TranscriptionOutcome) -> tuple[Segment, ...]: ...
```

Additive job field:

```python
@dataclass(frozen=True, slots=True)
class TranscriptionJob:
    stem: str
    chunks: tuple[Chunk, ...]
    source: str = ""
    kind: str = FIXTURE_KIND
    namespace: Path | None = None
```

- A response counts as successful empty only when every requested response was received, each payload explicitly contains `segments` as a list, and all lists are empty. For nonempty success, `segments` is the draft returned by the existing `transcribe.build_spine` assembly; do not create a parallel segment parser/assembler.
- Missing `segments`, wrong response count/index, a missing/non-numeric/nonfinite segment value, a response-coverage mismatch, or a call exception yields `error`, never `empty`. Numeric `bool` values are invalid.
- Finite zero-duration or reversed legacy segment ranges remain valid input records for P3-02's existing build/repair/refine engine; this boundary must not silently normalize, discard, or relabel them as malformed.
- Coverage is one record per requested chunk in index order; a successful/empty outcome proves full request coverage.
- `require_nonempty(empty)` raises `EmptyTranscript`, a `RuntimeError` subclass. The legacy `pipeline.fetch_draft` keeps its established fixture path and raises `EmptyTranscript` when its existing `build_spine` result is empty; it does not route legacy fixture jobs with `chunks=()` through project coverage validation. Project code records the empty outcome without raising.
- Existing single `transcriptions.create` boundary, prompt ban, defaults, and fixture behavior remain unchanged.
- `namespace`, when present, is the explicit fixture/artifact directory for that job; `FixtureTranscriber` resolves `<namespace>/<stem>.<kind>.json`. When absent, the legacy transcriber's configured fixture directory behaves exactly as before. The stem itself is never replaced by a content hash, and no environment variable is mutated.
- A namespace-present `FixtureTranscriber` request may ask only a retry subset while the saved kind file contains the whole recorded set. In that case select exactly one response per requested `Chunk.index`, require its offset to equal that requested chunk's rendered offset, reject duplicate/missing requested identity, and return only the requested responses in deterministic chunk order. This selection belongs only to `FixtureTranscriber` with `namespace is not None`; `namespace=None` retains the legacy return-all behavior, and generic/live transcribers never silently filter extra or unmatched responses.

## Implementation steps

1. Add failing unit tests for explicit empty, missing segments, wrong type, response count/index mismatch, missing/non-numeric/nonfinite/bool segment values, finite zero/reversed ranges retained, malformed segment, call failure, mixed empty/nonempty, legacy `RuntimeError` behavior, two same-stem jobs resolving independently through two explicit namespaces without global env mutation, namespace-present retry-subset selection, duplicate/missing/offset-mismatched requested fixture identity, and unchanged namespace-absent return-all behavior.
2. Implement immutable outcome/coverage for explicit project jobs by retaining the received response coverage and routing valid responses through existing `build_spine`. Change only the legacy empty exception type in `fetch_draft`, leaving its valid fixture call flow unchanged.
3. Run focused source tests, invariants, `make check`; commit only owned files.

## Acceptance

Focused tests prove empty/error cannot be conflated and the legacy valid fixture produces exactly the prior spine. Baseline no-network and verification-is-output-read-only tests pass. `make check` passes with no fixture changes.

## Failure route

Report payload shape (synthetic only), chunk indices, expected/actual status/exception, base/diff/tests. Never catch a broad error and label it empty. Repeated failure follows the diagnostic escalation chain.

## Report

Report Orca/launch IDs, branch/base/commit, files, failing-first/status matrix, focused/full tests, and `ready-to-integrate`.
