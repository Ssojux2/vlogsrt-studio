# Task P5-01: pin and validate the Silero v6.2.1 ONNX runtime

## Authority

Binding spec `VAD guard`; depends on P5-00. May run parallel with P5-04 because ownership is disjoint. No VAD inference policy belongs here.

## Baseline and ownership

- Fresh `fcp-p5-vad-runtime` worktree from exact P5-00-integrated SHA.
- Create `vlogsrt/project/vad/__init__.py`, `vlogsrt/project/vad/runtime.py`, `vlogsrt/project/vad/data/silero_vad_v6.2.1.onnx`, `vlogsrt/project/vad/data/SILERO_LICENSE.txt`, `tests/project/test_vad_runtime.py`.
- Modify only the narrow package-data declaration in `pyproject.toml` so the two pinned VAD data files ship in a wheel. Preserve core dependencies, extras, and all unrelated packaging settings. Do not change PCM, guard, vision, or legacy modules.

## Contract

```python
@dataclass(frozen=True, slots=True)
class VadArtifact:
    model_path: Path
    model_sha256: str
    license_path: Path
    license_sha256: str

class VadUnavailable(RuntimeError): ...

def bundled_artifact(cfg: ProjectConfig) -> VadArtifact: ...
def create_cpu_session(artifact: VadArtifact) -> object: ...
```

- Vendor the official tag `v6.2.1` artifact from the exact URL/checksum/size in P5-00 and the official MIT license with checksum `2e63e...520b`; Git LFS pointers or changed bytes fail.
- Locate both files through installed-package resources, never repository cwd, an absolute source checkout path, or runtime download. Validate both hashes before session creation. Missing/mismatch/unavailable NumPy or ONNX Runtime raises `VadUnavailable` and blocks automatic analysis.
- ONNX Runtime session uses only `CPUExecutionProvider`, inter/intra op threads 1, and exposes inputs `input`, `state`, `sr`; output shape/names are checked without executing real audio.
- No torch, torchaudio, automatic download, fallback model, or STT-only downgrade.

## Implementation steps

1. Add failing hash/missing/dependency/provider/input-shape tests with a fake runtime.
2. Fetch the two official files, verify hashes before staging, implement package-resource discovery/validation/session creation, and add exact wheel package-data configuration.
3. Build a wheel, install `.[project]` from that wheel into a fresh isolated environment, change cwd outside the source checkout, prove `vlogsrt.__file__` resolves to the isolated installation, then discover and hash both installed resources.
4. Separately execute the real pinned ONNX model with installed NumPy/ONNX Runtime and `CPUExecutionProvider`; assert the required input/output contract. Keep fake-session coverage but do not count it as real-runtime evidence.
5. Run focused/invariant/full checks and commit owned paths.

## Acceptance

Repository and installed-wheel artifact hashes/size match P5-00 exactly. An outside-checkout process imports the isolated wheel installation and discovers both resources. Corrupt/missing files fail closed. Fake provider-contract tests and a separate real CPU inference both pass with the installed project extra; missing/failed real runtime is `BLOCKED` or pending with exact evidence, never PASS. `make check` passes without torch.

## Failure route

Report source URL, expected/actual hash/size, provider list or missing package, base/tests/diff. Do not download another version or disable verification. Dependency/platform failure is an honest external blocker.

## Report

Report Orca/launch IDs, branch/base/commit, wheel filename, isolated interpreter/import path/outside cwd, installed artifact/license hashes/sizes, fake-provider evidence, real CPU model result, tests, and `ready-to-integrate` or explicit blocked state.
