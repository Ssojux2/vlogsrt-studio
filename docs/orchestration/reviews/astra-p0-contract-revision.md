# Astra P0-02 review — revision required

Reviewed commit: `df489bb0a8c077ed6c74c9709dfa929432129d59`.
Decision: do not merge yet. Opus reports 453 tests and 18 invariant groups passing;
Astra independently reproduced the following checker defects by parsing source strings
and calling `_xmllint_boundary_failures`. No subprocess in these examples was executed.

## Required bounded correction

With `import shutil, subprocess` and `exe = shutil.which("xmllint")`, both calls
below incorrectly return no failures:

```python
subprocess.run([exe, "--nonet", "--noout", path], executable="/bin/sh")
subprocess.run([exe, "--noout", path], **{"shell": True})
```

Also, `exe = unrelated.which("xmllint")` is accepted without a shutil import,
whereas the legitimate `from shutil import which as find_executable` alias is rejected.

GLM must add regression tests first, then repair the checker within the existing
P0-02 ownership: `scripts/check_invariants.py`, `tests/test_invariants.py`, and
`docs/PROJECT_CONTRACTS.md`. No runtime-module changes are needed.

- Reject an `executable` override and opaque keyword expansion in subprocess calls.
- Keep argv as the sole positional argument; reject extra positional arguments that
  could supply Popen's shell or executable parameters implicitly.
- Resolve shutil module and which-function imports, including aliases, rather than
  accepting any attribute named `which`. Reject unsupported/shadowed executable
  provenance conservatively; document the supported static shape instead of claiming
  a general Python security sandbox.
- Preserve approved ordinary and aliased subprocess/shutil patterns, shell omitted
  or literal False, and dynamic path arguments in the literal argv list.
- Correct the contract's timing wording: per-asset cached source SRT is immutable
  asset-local time; packaged SOURCE SRT projects those cues onto the input project's
  elapsed timeline before automatic cuts, including repeated instances. EDIT SRT
  projects onto the edited timeline. No source artifact is rewritten.
- Narrow the final machine-enforcement claim: these AST checks enforce the listed
  syntactic boundaries; source immutability/projection requires behavioral tests.

Opus must independently reproduce failures on the old commit, verify the repaired
tests plus full `make check`, and append the review disposition to
`docs/orchestration/reviews/p0-contract-opus.md`. Astra remains sole merger.
Do not expand this task into a general static-analysis engine or unrelated hardening.
## Reconciliation with Sol's current brief

Sol reported additional differences from his current P0-02 brief. C9 itself said
project-scoped optional imports and a documented CLI composition entrypoint; it did
not already mandate a VAD-only subtree or the specific `commands` module. To remove
ambiguity, Astra now ratifies the following narrower implementation contracts from
Sol's brief for this correction; do not describe all of them as defects against the
original worker dispatch.

- NumPy and ONNX Runtime imports are allowed only under `vlogsrt/project/vad/`.
  The proposed VAD and vision ownership already matches this boundary.
- Exact `vlogsrt/cli.py` may import only `vlogsrt.project.commands` from the project
  package, including equivalent relative forms; other project imports still fail.
- Add the brief's glm model-literal guard now, with exact project config exception.
- Check dataclasses in exact `vlogsrt/project/models.py` require `frozen=True`;
  the absent file remains valid until P0-04. Avoid claiming deep immutability.
- Freeze the XML subprocess API to `subprocess.run` (including resolved aliases)
  and these two argv forms, with normal dynamic path expressions in the final
  slots: `[exe, "--nonet", "--noout", path]` or
  `[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, path]`.
  Other methods/flags fail. P1-04 separately behavior-tests DTD URI conversion,
  path escaping, errors, and actual XML validation; do not implement P1 here.

Opus should embed this entire amended document in the fresh GLM task. Source timing
wording and the original reproduced guard defects remain required. This closes the
shared contract once, before downstream parallel coding.

## Execution

Continue in `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract`, based on the
reviewed commit. One fresh GLM dispatch/session, model `glm-5.3[1m]`, effort `max`,
user-selected permission mode `auto`, existing deny rules and offline constraints.
No competing editor in that worktree. Opus owns supervision/review, GLM owns code/tests.
Do not merge, push, read secrets, alter protected fixtures, or claim live FCP/media
acceptance. Report exact new commit and verification results.
