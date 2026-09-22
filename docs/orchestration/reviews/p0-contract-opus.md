# P0-02 diff review — Opus5 (co-manager)

> Astra archive note, 2026-09-09: this is a historical review of `df489bb` and
> `51319b9`, not approval of the final implementation. Its merge recommendations
> were superseded by later findings and fixes. The authoritative final result is
> [Astra P0 acceptance](astra-p0-contract-acceptance.md): final checker `c7af159`,
> P0 phase join `cd411c4`. Opus hit its service limit before final review, so final
> approval was performed by Astra. Claims below apply only to their stated rounds.

> **Round 1 below is superseded.** Astra held this merge and found real xmllint-boundary
> bypasses I missed; the corrected verdict, the reproduction, and the verification of the fix
> are in **Revision round** at the end of this file. Read that section for the current state.
> In particular, the round-1 claim that the xmllint boundary was "better than specified" was wrong.

Verdict (round 1, superseded): ready for Astra merge. Three follow-ups, none blocking.

| | |
|---|---|
| Branch / worktree | `Ssojux2/fcp-p0-contract` · `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract` (Orca child) |
| Base | `676460111bad5ef04cbc9d5c776940611fa938a7` (child HEAD verified equal before work) |
| Commit | `df489bb0a8c077ed6c74c9709dfa929432129d59` |
| Worker | claude-glm `glm-5.3[1m]` effort `max` — banner verified: *"Claude Code v2.1.263 / glm-5.3[1m] with max effort · API Usage Billing"* |
| Orca | Run `run_b6d40d052690` · Task `task_f7788389960b` · Dispatch `ctx_df9c602e28a2` · Terminal `term_ea415fa1-9b4f-402d-922f-4d6dbe039b22` (state `succeeded`, stage `settled`) |

Everything below I ran myself in the child worktree. I did not take the worker's report on faith,
and I wrote no source or test code.

---

## 1. Independent verification

```
.venv/bin/python -c "import sys,vlogsrt;print(...)"
  → 3.12.14
  → /Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract/vlogsrt/__init__.py   ← own checkout ✅

.venv/bin/python scripts/check_invariants.py   → 18 groups, all [OK], exit 0
.venv/bin/python -m pytest                     → 453 passed   (389 baseline + 64 new)
make check PY=.venv/bin/python                 → PASS
git diff --check HEAD^                         → exit 0
git status --porcelain                         → clean
git show --stat HEAD                           → exactly the four owned paths
```

Commit contents: `docs/PROJECT_CONTRACTS.md` (+89), `pyproject.toml` (+5/−2),
`scripts/check_invariants.py` (+326/−24), `tests/test_invariants.py` (+448). Nothing else.
No `.env*`, no `golden/`, no `tests/fixtures/api/`, no `vlogsrt/project/**`, no legacy runtime module.
`.env.example` is untouched and absent from the child, as Astra's disposition requires.

## 2. The tests are not vacuous — two mutation experiments

Anti-vacuity is the thing worth checking on a checker task, so I checked it rather than reading it.

**Experiment A — swap in the pre-fix checker (`HEAD^`), keep the new tests.**
Multiple new tests fail, including `test_project_sources_stay_below_the_project_package`,
`test_ui_belongs_to_the_legacy_side` and `test_the_real_tree_passes_the_new_contract_checks`.

**Experiment B — targeted mutation.** I reverted exactly one line of the new checker, putting the
openai boundary back to `path.name == "transcribe.py"`:

```
FAILED tests/test_invariants.py::test_transcribe_keeps_the_openai_monopoly_by_exact_path
```

One named test, precisely the rename-bypass regression Astra asked for. That is real coverage, not a
constant assertion. Working tree restored clean after both experiments.

## 3. Contract coverage against what was asked

| Requirement | Status |
|---|---|
| Rename bypass closed at all three sites (`transcribe.py`, `preprocess.py`, `config.py` line ~34/265/303) | ✅ exact relative paths via `_rel()`; `MODEL_LITERAL_PATHS` correctly admits both `vlogsrt/config.py` and `vlogsrt/project/config.py` |
| Legacy may not import `vlogsrt.project`, absolute or relative | ✅ `_project_importfrom` covers `import vlogsrt.project`, `from vlogsrt.project import x`, `from vlogsrt import project`, `from .project import x`, `from . import project`, and `from .. import project` |
| Narrow CLI composition exception | ✅ `CLI_COMPOSITION_ENTRYPOINT = "vlogsrt/cli.py"`, exact path, with a test that no other legacy module or `ui/**` inherits it |
| `ui/**` on the legacy side | ✅ `_legacy_sources()` = `vlogsrt/*.py` (top level only) + `ui/**`, with a test |
| numpy/onnxruntime scoped below `vlogsrt/project/` | ✅ and `test_other_banned_names_still_fail_everywhere_in_the_package` proves the rest of `BANNED_IMPORTS` was not loosened |
| AST, not regex; aliases and multi-imports | ✅ `_import_roots` handles `import numpy as np`, `from numpy import ndarray`, `import numpy.typing`; `_subprocess_bindings` handles `import subprocess as sp` and `from subprocess import run, Popen` |
| xmllint-only boundary: fixed executable, fixed argv, no shell | ✅ **better than specified** — `_xmllint_names` tracks `shutil.which("xmllint")` provenance and *taints* any name reassigned from anything else; argv must be a list/tuple literal; `JoinedStr`/`BinOp`/`Starred` elements rejected; `shell` must be absent or literal `False`; `from subprocess import *` rejected outright |
| Missing `xml_validation.py` neither crashes nor silently passes | ✅ `test_missing_xml_boundary_neither_crashes_nor_blinds_the_check` — the global subprocess ban stays live while the file is absent |
| 16 existing groups still active and not weakened | ✅ 18 groups reported, the original 16 unchanged in behaviour |
| Core deps, requires-python, dev/ui extras untouched | ✅ only the `project` extra and two mypy overrides added |
| Scope discipline | ✅ no `vlogsrt/project/**` created, no byte-snapshot or ProjectConfig work pulled in |

`XML_VALIDATION_MODULE` is `vlogsrt/project/xml_validation.py`, which matches the path Sol's
`p1-04-xml-validation.md` will create. The worker cross-read that brief before hard-coding the
constant — good instinct, and it removes a coordination risk I had flagged.

## 4. Follow-ups (none block the merge)

**FU-1 — `glm-` model literals are still not caught.** `check_no_hardcoded_model` matches only
`whisper-1` and `gpt-*transcribe*`. My dispatch asked for `glm-[\w.-]*` to be added; it was not, and
Sol's brief did not require it, so I am not sending this back. It is harmless today because
`vlogsrt/project/` does not exist — but P5-05 introduces `glm-5.3-flash`, and a literal in
`vlogsrt/project/vision/*.py` would pass unnoticed. Fold the pattern into whichever P5 task first
touches the checker.

**FU-2 — `os.system` / `os.popen` remain uncovered.** The subprocess boundary keys on the
`subprocess` module. `os.system("xmllint …")` would evade every check. Pre-existing gap, widened in
relevance now that a second subprocess boundary exists. Cheap to add later; not this task's scope.

**FU-3 — a stale group label.** `check_ffmpeg_boundary()` now delegates to
`check_subprocess_boundaries()`, which the brief explicitly permits, but the console still prints
`[OK] ffmpeg 경계` for a check that also enforces the xmllint contract. Rename the label, or register
`check_subprocess_boundaries` directly, next time that file is opened.

**Nit, no action needed.** The commit also changes one unrelated comment string in `DELETED_MODULES`
(`되돌리기` → `되살리기`). Outside the requested scope, zero behavioural effect.

## 5. Note on how this ran

The worker stalled twice on interactive permission prompts. Orca returns `agent_prompt_blocked` when
one agent tries to answer another agent's prompt, so I could not clear them; per Astra's Decision D I
did not attempt any workaround. The user cleared them and then switched the terminal to auto mode
themselves. The child's committed `.claude/settings.json` deny rules and `VLOGSRT_OFFLINE=1` stayed in
force throughout, so protected paths and paid calls were never reachable.

For future tasks this is worth designing away, as Astra also observed: the brief lived only in the
root checkout, so every read of it raised a prompt. Committing or embedding the brief inside the child
worktree removes that class of stall entirely.

## 6. Recommendation

Merge `df489bb` into `feat/final-cut-project`. Branch is `ready-to-integrate`, not merged; I performed
no merge, push, or rebase. FU-1 is the one worth scheduling before P5.

---

# Revision round — disposition of Astra's `astra-p0-contract-revision.md`

Verdict: **the correction is complete and verified. `51319b9` is ready for Astra's merge.**

| | |
|---|---|
| Reviewed commit | `51319b99635f86a68543d02783674d4bf32eb06b` (parent `df489bb`) |
| Branch / worktree | `Ssojux2/fcp-p0-contract` · `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract` |
| Worker | fresh claude-glm `glm-5.3[1m]` effort `max`, `--permission-mode auto` (the user's recorded choice; a distinct CLI mode from `bypassPermissions`, so the committed deny rules and `VLOGSRT_OFFLINE=1` stayed in force) |
| Orca | Run `run_b6d40d052690` · Task `task_7d03430673e1` · Dispatch `ctx_51f56ba3df5c` · Terminal `term_7a94892e-6f38-4421-8273-51b889ce2619` |

## 1. Astra was right and my earlier verdict was wrong

I reproduced every counterexample on `df489bb` before launching anything, by parsing source strings
and calling `_xmllint_boundary_failures` directly. No subprocess was executed.

| case | on `df489bb` |
|---|---|
| `subprocess.run([exe, "--nonet", "--noout", path], executable="/bin/sh")` | **no failures — bypass** |
| `subprocess.run([exe, "--noout", path], **{"shell": True})` | **no failures — bypass** |
| `exe = unrelated.which("xmllint")` then `subprocess.run([exe, …])` | **no failures — bypass** |
| `subprocess.Popen([exe, "--noout", path], -1, "/bin/sh")` | **no failures — bypass** |
| `from shutil import which as find_executable` (legitimate) | **wrongly rejected** |

My first-round review called this boundary "better than specified." That was wrong, and the reason is
a defect in my own method, not just in the code: my mutation probe covered only the `openai`
basename path and never touched the argv contract, so a check I had not probed got credit from
checks I had. This round I probed the argv contract directly, and I have added unprompted
counterexamples as a standing habit for checker reviews.

## 2. Independent verification of `51319b9`

**Counterexample battery — 15 cases, 0 mismatches.** The five Astra named, plus ten probes the worker
was never told about, to test whether it fixed the *class* or only the named cases:

| probe (not in the worker's brief) | expected | result |
|---|---|---|
| `subprocess.run(args=[exe, …], shell=True)` — argv as keyword | reject | reject |
| `subprocess.run([exe, …], **kw)` — opaque Name, not a dict literal | reject | reject |
| `exe = shutil.which("xmllint")` then `exe = "/bin/sh"` — reassignment | reject | reject |
| `from shutil import *; exe = which("xmllint")` | reject | reject |
| `foo.shutil.which("xmllint")` — nested attribute | reject | reject |
| `shutil.which("xsltproc")` — wrong executable | reject | reject |
| `import shutil as sh; sh.which("xmllint")` — module alias | accept | accept |
| `shell=False` literal | accept | accept |
| `import subprocess as sp; sp.run(...)` | accept | accept |
| `from subprocess import run; run(...)` | accept | accept |

It fixed the class, not the five cases.

**Failing-first, against the reviewed commit.** Restoring `df489bb`'s checker under the new tests
fails exactly seven, covering all four bypasses, the false positive, and two provenance cases the
worker added on its own initiative:

```
[executable-override]  [dict-expansion-shell]  [foreign-which]  [popen-positional-executable]
test_xmllint_boundary_accepts_the_aliased_which_import
test_xmllint_boundary_rejects_a_shadowed_which_binding
test_xmllint_boundary_rejects_shutil_star_imports
```

**Mutation probes on the argv contract — the gap from last round.** Each guard is load-bearing and
individually covered:

| mutation | failing test |
|---|---|
| drop the `executable=` keyword rejection | `…rejects_the_reviewed_bypasses[executable-override]` |
| allow extra positional args (`len(site.args) > 1` → `> 99`) | `…[popen-positional-executable]` |
| drop the `**kwargs` rejection | `…[dict-expansion-shell]` |
| trust any attribute named `which` | 20 tests fail |

**Full acceptance, run by me in the child worktree:**

```
vlogsrt.__file__ → /Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract/vlogsrt/__init__.py  (3.12.14)
scripts/check_invariants.py  → 18 groups, all [OK], exit 0
pytest                       → 461 passed   (453 + 8 new)
make check PY=.venv/bin/python → PASS
git diff --check HEAD^       → exit 0 ;  git status --porcelain → clean
git show --stat HEAD         → exactly the three owned paths
```

## 3. The two wording corrections

Both land as specified. §3 now separates immutable asset-local cached source SRT from the packaged
SOURCE SRT projected onto the input project's elapsed timeline before automatic cuts — per instance
for repeated assets — and EDIT SRT onto the edited timeline, with no source artifact rewritten.
The enforcement section now claims only the syntactic boundaries (§1, §4, §5, §6) and states plainly
that source immutability (§2) and time projection (§3) require behavioral tests, and that the checker
is a guardrail against accidental drift rather than a Python security sandbox.

## 4. Scope

Held. Three owned files, no runtime-module change, no general static-analysis engine, no `os.system`
work, and the `glm-` model-literal follow-up (FU-1 above) correctly left for a later task before P5.
No competing editor: the previous session's terminal was closed before this one was created.

## 5. Recommendation

Merge `51319b9`. No merge, push, or rebase was performed by me or by the worker.
