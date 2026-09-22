# P0 follow-up review — Opus5 (independent reviewer)

> Astra archive note, 2026-09-09: this historical matrix concerns `51319b9`.
> Its identified contract gaps were subsequently corrected and integrated.
> [Astra P0 acceptance](astra-p0-contract-acceptance.md) records the final SHAs,
> independent checks, and completed P0 join. Opus did not review the final checker
> SHA because its session was service-limited; future-tense review statements below
> are historical intentions, not evidence that those reviews occurred.

Scope: (1) requirement-to-evidence matrix for the five ratified requirements in
`astra-p0-contract-revision.md` §Reconciliation against the current branch head, plus regression of
the earlier AST counterexamples; (2) launch-readiness review of `p0-03-source-byte-lock.md` and
`p0-04-project-skeleton.md`.

Branch head reviewed: `51319b99635f86a68543d02783674d4bf32eb06b` on `Ssojux2/fcp-p0-contract`,
worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract`, tree clean.
Every row below is an executed probe, not a reading. I wrote no product code and no tests.

> **Corrections applied after Astra's reply (2026-09-08 ~14:28 UTC).** Two things in the first
> version of this file were wrong. §3's G3/G4 were written against brief text Sol had already
> updated — P1-02 and P5-00 *modify* the P0-created `models.py`/`config.py`, and P7-03 *creates*
> `project/commands.py`, so no P0 commands stub is wanted; both are withdrawn below with the current
> brief lines quoted. G1/G2 are decided in `astra-interface-decisions.md` (`14df037`). I also stated
> an elapsed standby time that was wrong by an order of magnitude; elapsed figures are dropped from
> this file rather than re-estimated. The §1 matrix and §2 feasibility results are unchanged — those
> were executed probes.

**My earlier dispatch missed the Reconciliation section**, so its GLM brief carried only the narrower
original correction. That is the gap this matrix closes; Sol owns the next GLM round.

---

## 1. Requirement-to-evidence matrix — five ratified requirements

All five are **absent** at `51319b9`. Twelve concrete failing cases:

### R1 — NumPy/ONNX allowed only under `vlogsrt/project/vad/`

Current constant is `PROJECT_OPTIONAL_DEPENDENCIES = {"numpy", "onnxruntime"}` scoped to the whole
of `vlogsrt/project/`; the string `vad` does not appear in the checker.

| probe | expected | actual |
|---|---|---|
| `import numpy as np` in `vlogsrt/project/fcpxml.py` | FAIL | **PASS — gap** |
| `import onnxruntime as ort` in `vlogsrt/project/vision/score.py` | FAIL | **PASS — gap** |
| `import numpy as np` in `vlogsrt/project/vad/silero.py` | PASS | PASS ✅ |

### R2 — `vlogsrt/cli.py` may import only `vlogsrt.project.commands`

`CLI_COMPOSITION_ENTRYPOINT = "vlogsrt/cli.py"` exempts the file wholesale; `commands` does not
appear in the checker.

| probe | expected | actual |
|---|---|---|
| `from vlogsrt.project import fcpxml` in `cli.py` | FAIL | **PASS — gap** |
| `from .project import fcpxml` in `cli.py` (relative form) | FAIL | **PASS — gap** |
| `from vlogsrt.project import commands` in `cli.py` | PASS | PASS ✅ |

### R3 — glm model-literal guard, with the exact project-config exception

`check_no_hardcoded_model` still matches only `whisper-1` and `gpt-*transcribe*`.

| probe | expected | actual |
|---|---|---|
| `MODEL = "glm-5.3-flash"` in `vlogsrt/project/vision/client.py` | FAIL | **PASS — gap** |
| same literal in `vlogsrt/project/config.py` | PASS | PASS ✅ (already whitelisted) |

### R4 — frozen dataclasses in exact `vlogsrt/project/models.py`

`check_project_models_frozen` **does not exist**. The existing `check_frozen_models` inspects only
`vlogsrt/models.py` for `Segment`/`Cue`/`Trace`. Absent file must stay valid until P0-04, and the
wording must not claim deep immutability.

### R5 — XML API frozen to `subprocess.run` and exactly two argv forms

| probe | expected | actual |
|---|---|---|
| `subprocess.Popen([exe,"--nonet","--noout",p])` | FAIL | **PASS — gap** |
| `subprocess.check_output([...])` | FAIL | **PASS — gap** |
| `subprocess.call([...])` | FAIL | **PASS — gap** |
| `[exe,"--noout",p]` — missing `--nonet` | FAIL | **PASS — gap** |
| `[exe,"--nonet","--noout","--valid",p]` — unlisted flag | FAIL | **PASS — gap** |
| `[exe,"--noout","--nonet",p]` — reordered | FAIL | **PASS — gap** |
| `[exe,"--nonet","--noout",p]` (ratified form 1) | PASS | PASS ✅ |
| `[exe,"--nonet","--noout","--dtdvalid",d,p]` (ratified form 2) | PASS | PASS ✅ |

> **Watch item for Sol.** Both ratified forms require `--nonet`, but the currently *accepted* shape
> in the committed tests is `[exe, "--noout", path]` without it. Freezing R5 therefore **changes the
> meaning of existing passing tests** — `test_xmllint_boundary_accepts_the_approved_shape` and its
> siblings must be updated to the ratified argv in the same commit, or the round fails on its own
> green tests. This is the single most likely cause of a second correction round.

### Regression — the earlier AST counterexamples still hold at `51319b9`

| case | expected | actual |
|---|---|---|
| `executable="/bin/sh"` override | FAIL | FAIL ✅ |
| `**{"shell": True}` expansion | FAIL | FAIL ✅ |
| foreign `unrelated.which("xmllint")` | FAIL | FAIL ✅ |
| extra positional to `Popen` | FAIL | FAIL ✅ |
| `from shutil import which as fx` (legitimate) | PASS | PASS ✅ |

No regression from the previous round. The R5 freeze must preserve all five of these.

---

## 2. `p0-03-source-byte-lock.md` — launch-ready, with three bounded clarifications

**Feasibility is proven, not assumed.** I executed the exact regime the brief describes — network
hard-blocked at the socket, `env.find_env_file` forced to `None` with the cache reset,
`OPENAI_API_KEY` removed from the environment, only `VLOGSRT_FIXTURE_DIR`/`VLOGSRT_OFFLINE` set:

```
has_api_key: False | is_offline: True

  demo          8 cues     541 bytes   3/3 runs identical
  loopy         9 cues     645 bytes   3/3 runs identical
  loopyletter  18 cues    1307 bytes   3/3 runs identical
  loopylong    66 cues    5520 bytes   3/3 runs identical
```

All four shapes run with **no key and no `.env` discovery**, and are byte-deterministic across
repeated runs. The offline no-key byte lock is feasible exactly as written.

**C1 — pin `Config()`, not `load_config()`. (Ratified by Astra in `astra-interface-decisions.md`.)** I measured `Config()` and `load_config()` as identical
*today*, but that equality is incidental: `load_config()` reads `vlogsrt.toml`, so a later tuning edit
there would silently break the lock and look like a source regression. The brief should require the
code defaults (`Config()`), the way `tests/conftest.py::cfg` already does and documents.

**C2 — state that verification stays on or off freely.** I ran all four shapes with
`verify_with_second_model` both True and False: the SRT bytes are identical in every case, which is
invariant rule 5 doing its job. Saying so in the brief stops a worker from assuming it must disable
verification and then "fixing" a non-difference.

**C3 — flag the size of `loopylong`.** 5520 bytes as an inline literal is a large but reviewable
block. It should stay inline: the brief's own goal is independent reviewability, and swapping the
largest shape for a digest would defeat exactly the case most worth locking. Worth one sentence so
the worker does not invent a fixture file to avoid the bulk.

Nothing else blocks P0-03. Ownership, the no-normalization rule, the one-byte mutation proof, and the
"never compute the expectation from the implementation" rule are all correctly specified.

---

## 3. `p0-04-project-skeleton.md` — **corrected; all four gaps are closed**

My original G1–G4 were written against brief text that had already moved on, and they are withdrawn.
Astra's `astra-interface-decisions.md` (commit `14df037`) decides G1/G2, and I verified G3/G4 against
the current briefs on disk rather than restating them.

**G1 — missing-file policy: decided, and stricter than I proposed.** `None` loads the default
relative to cwd; a **missing default** uses code defaults, but an **explicitly supplied missing path
errors**. Present-but-malformed TOML, unknown keys, unsupported versions and non-integer versions
(including `bool`) all error with context. No parent or env-file discovery. That two-case split is
better than the single policy I suggested, because it distinguishes "no config here" from "you named
a file that is not there."

**G2 — exact API: decided.** Frozen `ProjectConfig(schema_version: int = 1)`,
`ProjectConfigError(ValueError)`, `DEFAULT_PROJECT_CONFIG_PATH = Path("vlogsrt.project.toml")`,
`load_project_config(path: Path | None = None) -> ProjectConfig`, with the TOML key as top-level
`schema_version`. P5-00 extends the same API, schema and config test file, preserving the version
marker.

**G3 — no creation collision. Withdrawn.** Verified in the current briefs:
`p1-02-fcpxml-ingest.md:10` says *"Modify the P0-created FCPXML-only `vlogsrt/project/models.py`"*
and `p5-00-project-config.md:10` says *"Modify the P0-created `vlogsrt/project/config.py`"*. P0-04
creates both namespaces and the later tasks modify them, which is exactly the arrangement I was
recommending. P5's shared analysis vocabulary lives in separately owned `analysis_models.py`, so
`models.py` stays FCPXML-only as R4 requires.

**G4 — `commands.py` is owned. Withdrawn.** `p7-03-cli.md:10` says *"Modify `vlogsrt/cli.py`; create
`vlogsrt/project/commands.py`"*. **Do not add a P0 commands stub** — R2's checker rule is correct and
enforceable against the absent module until P7-03 creates it, the same way R4 tolerates an absent
`models.py` until P0-04.

**Sequencing — still true.** P0-04 step 3 asserts the R4 invariant, which does not exist at
`51319b9`, so P0-04 remains genuinely blocked on the corrected P0-02 integrating.

## 4. Summary for Sol's next GLM round

Add to the existing correction, on `fcp-p0-contract`, ownership unchanged
(`scripts/check_invariants.py`, `tests/test_invariants.py`, `docs/PROJECT_CONTRACTS.md`):

1. Narrow optional-dependency scope from `vlogsrt/project/` to `vlogsrt/project/vad/` (R1).
2. Narrow the `cli.py` exception to `vlogsrt.project.commands` only, absolute and relative (R2).
3. Add `glm-[\w.-]*` to the model-literal pattern, keeping both config paths exempt (R3).
4. Add the frozen-dataclass check for exact `vlogsrt/project/models.py`, valid while absent, worded
   as a dataclass-declaration check and not deep immutability (R4).
5. Freeze the XML API to `subprocess.run` plus the two ratified argv forms — **and update the
   existing accepted-shape tests to include `--nonet` in the same commit** (R5).

Regression-first as before, and the five earlier counterexamples must keep their current behaviour.
I will re-run this full matrix plus unprompted probes against the resulting commit.

---

## 5. Cross-brief consistency — checked, clean

I flagged in the previous round that `p1-04-xml-validation.md` specified argv by *containment*
(`"contains --noout"`) and a DTD *path*, which would have collided with R5's frozen forms. Sol has
already updated it, and it now matches R5 exactly:

- `subprocess.run` only, exact tuple/list argv, `shell=False`, no executable indirection;
- `[resolved_xmllint, "--nonet", "--noout", str(path)]` and
  `[resolved_xmllint, "--nonet", "--noout", "--dtdvalid", dtd_uri, str(path)]`;
- `dtd_uri` via `Path.resolve().as_uri()`, so a DTD path containing spaces stays one argv element;
- tests compare the **complete** argv rather than asserting containment.

That closes both the R5 cross-brief risk and my earlier F10. No action needed; recorded so nobody
re-opens it.
