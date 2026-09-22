# Astra: precise P5 task requirements

This records the requirements from message msg_636ababe65a6 so a truncated notification cannot lose the task contract. Sol incorporates them before P5 dispatch; they do not block the active P0 task.

## P5-01: packaged VAD artifact

- The existing pyproject.toml has no package-data declaration. Merely placing ONNX and license files under vlogsrt/project/data does not demonstrate that an installed distribution contains them.
- Add narrowly scoped ownership of the package-data declaration in pyproject.toml to P5-01, or assign a separate serial packaging task. Preserve core dependencies, existing extras, and other packaging settings. This does not overlap the P5 vision worker's files.
- Include the exact pinned model and license in the built wheel. Locate resources in the installed package, without relying on the repository cwd, an absolute development path, or an automatic runtime download.
- Acceptance must build a wheel, install it into an isolated environment, and invoke artifact discovery and hash validation from outside the source checkout. Verify imported vlogsrt belongs to that installation, then verify model/license bytes against the pinned hashes. A successful editable-install test alone is insufficient.
- Keep fake-session tests for missing/corrupt artifacts and provider contracts. Separately exercise the real pinned model with the installed project extra and CPU provider. A fake session cannot establish real ONNX execution compatibility. Missing runtime or a failed real check remains explicitly pending/blocked, never PASS.
- Preserve existing failure behavior: unavailable/mismatched artifact or dependency blocks automatic analysis; no alternate model or silent STT-only downgrade.

## P5-04: original-to-proxy frame correspondence

- FCP media-rep association or the explicit media map supplies the intended original/proxy pair. Filename similarity alone supplies no association.
- Frame requests identify a position on the original source's exact frame grid. Do not pass the same numeric seek seconds to both files without establishing their video time references.
- Define original and proxy video presentation origins, CFR frame duration, usable frame count/range, and the mapping from an original frame index to a proxy frame index. Obtain media probing/extraction through the existing preprocess.py ffprobe/ffmpeg boundary. Keep original audio/STT/VAD identity unchanged.
- For the supported same-rate, frame-preserving proxy case, verify rate and frame-range compatibility and account for nonzero/different video PTS origins when selecting the corresponding frame. Pin the required metadata/signatures in the task brief before implementation; do not let parallel workers invent incompatible fields.
- These checks establish supported timing correspondence, not a cryptographic proof that every proxy image depicts the same content as the original. Do not claim a content-equality test that was not performed.
- If timing correspondence is unavailable, incompatible, or the requested proxy frame is out of range, use the original image and record the reason in review metadata. Do not guess a proxy offset or silently use the wrong frame. Failure to read the original remains an explicit analysis failure.
- Use deterministic synthetic original/proxy fixtures with known frame identities. Test different/nonzero PTS origins, first/last frames, rate or frame-count mismatch, out-of-range requests, and fallback. Mocked seek-argv tests alone do not prove the selected image corresponds to the requested source frame.

## Current launch preference

The user selected auto mode in the first GLM terminal. Root verified its banner and recorded that preference in workflow.json and README.md at commit 274b8e3. Subsequent fresh GLM workers in this same approved project use:

`claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`

Existing deny rules and offline settings remain unchanged. This is the normal auto classifier mode, not bypassPermissions. It does not authorize automated responses to a blocked permission prompt. The active worker does not need a restart.
