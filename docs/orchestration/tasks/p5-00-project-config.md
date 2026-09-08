# Task P5-00: freeze project-only configuration

## Authority

Binding spec `VAD guard`, `Visual scoring and deterministic roughcut`, and `All new tunables in project config`; depends on Astra's P3 merge. This small shared-contract task precedes both P5 branches.

## Baseline and ownership

- Fresh `fcp-p5-config` worktree from exact P3 merge SHA.
- Modify the P0-created `vlogsrt/project/config.py` and top-level `vlogsrt.project.toml`; create `vlogsrt/project/analysis_models.py` and `tests/project/test_project_config.py`.
- No VAD artifact/runtime, vision implementation, edit policy, CLI, legacy Config changes, or root config file.

## Contract

```python
@dataclass(frozen=True, slots=True)
class ProjectConfig:
    schema_version: int = 1
    vad_model_version: str = "v6.2.1"
    vad_model_url: str = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/src/silero_vad/data/silero_vad.onnx"
    vad_model_sha256: str = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
    vad_license_url: str = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/LICENSE"
    vad_license_sha256: str = "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
    vad_sample_rate: int = 16000
    vad_window_samples: int = 512
    vad_context_samples: int = 64
    vad_positive: float = 0.35
    guard_padding_seconds: Fraction = Fraction(1, 2)
    vision_model: str = "glm-5.3-flash"
    vision_api_key_env: str = "ZAI_API_KEY"
    vision_base_url_env: str = "ZAI_BASE_URL"
    vision_long_edge: int = 768
    vision_sample_fps: Fraction = Fraction(1)
    vision_max_images: int = 16
    block_min_seconds: Fraction = Fraction(6)
    block_max_seconds: Fraction = Fraction(10)
    keep_seconds: Fraction = Fraction(3)
    vision_apply_confidence: float = 0.8
    pricing_currency: str | None = None
    source_audio_cost_per_minute: Decimal | None = None
    repair_verify_audio_cost_per_minute: Decimal | None = None
    boundary_audio_cost_per_minute: Decimal | None = None
    vision_cost_per_image: Decimal | None = None

    def to_dict(self) -> dict[str, object]: ...
    def sha256(self) -> str: ...

def load_project_config(path: Path | None = None, overrides: Mapping[str, object] | None = None) -> ProjectConfig: ...

AnalysisStatus = Literal["success", "empty", "error"]

@dataclass(frozen=True, slots=True)
class AnalysisFailure:
    kind: str
    message: str
    retryable: bool
```

- Model names and every new threshold live only here; consumers may not duplicate numeric literals.
- Extend P0's same `ProjectConfig`, `ProjectConfigError`, `DEFAULT_PROJECT_CONFIG_PATH`, top-level `schema_version`, and cwd-relative/default-versus-explicit-path loading semantics. Keep `schema_version = 1`; the optional `overrides` argument is an additive explicit layer, not a second root or environment search.
- Serialize Fractions canonically as `numerator/denominator`; reject unknown keys, NaN/inf, wrong sample rate/window/context, nonpositive dimensions, min > max, keep > min, confidence outside `[0,1]`.
- Extend the P0 loader without adding a second config root. No implicit `.env`/working-directory search and no mutation of legacy `Config`.
- Vision provider credentials are distinct from source STT credentials: never reuse `OPENAI_API_KEY` or `OPENAI_BASE_URL`. Configuration stores only the separate environment-variable names, never their values; credential lookup belongs to the explicit online runner.
- Pricing fields are optional user-supplied budgeting assumptions, all defaulting to `None`; they are not asserted current provider tariffs and never come from environment discovery or a legacy-config fallback. Source-full, repair/verify, boundary, and vision components remain separately priced so one model's rate cannot silently price another. TOML supplies Decimal prices as strings and `to_dict` emits their canonical decimal strings; reject `bool`, negative, NaN/Infinity, or malformed values. If any price is configured, require an explicit nonblank `pricing_currency` label.
- `analysis_models.py` is the frozen shared P5 result/error vocabulary reviewed before the two branches start. VAD- and vision-specific records remain within their disjoint subpackages; neither branch edits this shared file.
- Artifact coordinates verified from official tag v6.2.1: ONNX URL `https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/src/silero_vad/data/silero_vad.onnx`, 2,327,524 bytes, checksum above; official MIT LICENSE checksum above.

## Implementation steps

1. Write failing default/roundtrip/unknown-key/type/range/hash tests, separate-credential-name tests, optional Decimal pricing/currency/canonical-hash tests, and frozen shared-model tests.
2. Implement immutable parsing/validation with stdlib TOML and deterministic JSON hashing.
3. Scan future-facing constants only through tests of this module; run invariants/full check; commit owned files.

## Acceptance

Defaults equal the binding values exactly, including no assumed prices; two equivalent TOML documents yield the same hash, every invalid combination fails clearly, and `make check` passes. No model name is introduced outside config/test expectations; neither credential nor price value is read from environment during config loading.

## Failure route

Report key/value, expected diagnostic/hash, base/tests/diff. Do not loosen unknown-key or finite-value validation. Contract disputes go to Opus/Astra; code fix remains GLM.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, config SHA example, and `ready-to-integrate`.
