# Final Cut project implementation ledger

> **For agentic workers:** Execute only the single linked task brief assigned by the live Orca Dispatch. Product code and tests are written only by fresh `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto` workers; Astra owns phase integration.

**Goal:** Deliver the offline-first Final Cut roughcut and synchronized SOURCE/EDIT SRT package specified in `docs/plans/2026-09-08-final-cut-project.md`.

**Architecture:** The existing subtitle pipeline remains the immutable source engine. A one-way `vlogsrt.project` derived layer consumes its public transcription/preprocess boundaries, models FCPXML and media identity with exact rational clocks, persists content-addressed analysis, and produces deterministic edit/export artifacts without allowing project code to flow back into legacy source modules.

**Tech Stack:** Python 3.11+ runtime (development on Python 3.12.14), stdlib XML/Fraction/wave/hash/JSON, optional NumPy + ONNX Runtime, pytest/ruff/mypy, Final Cut Pro FCPXML 1.14, Silero VAD v6.2.1 ONNX.

**Spec:** `docs/plans/2026-09-08-final-cut-project.md`

## Global constraints

- Integration branch: `feat/final-cut-project`; bootstrap product base: `676460111bad5ef04cbc9d5c776940611fa938a7`; Astra contract authority is recorded through `9727b2be887a31e18b81152189cdd6352fce1e50`, followed by Sol-owned planning commits beginning at `22490cd`.
- Every worker branch starts from the exact phase base SHA approved by Astra, never implicit `origin/main`.
- Maximum two simultaneous implementation workers, only with disjoint owned files/worktrees and satisfied dependencies.
- Managers write plans, reviews, and this ledger only. GLM writes all product code and tests; Astra alone integrates phase branches.
- The user selected normal auto permission mode for this approved project and root recorded it at `274b8e3`; existing deny/offline rules remain binding. Never use bypass mode or automate a response to a blocked prompt.
- Do not read, edit, stage, revert, copy, or merge `.env.example`; do not access `.env`/keys, `golden/**`, or modify `tests/fixtures/api/**`.
- No paid STT/vision calls, no `--online`, no fixture recording, no push, and no deploy.
- No fixture or golden file may be modified or added. Synthetic cases must be inline or generated under pytest `tmp_path`; existing fixture expectations may not be weakened to make failures pass.
- External FCP import/re-export, original 4K relink/playback, and paid/live-model checks stay `PENDING_HUMAN`; `SKIP` and blocked are never recorded as PASS.

## Live orchestration

| Role | Task | Dispatch | State |
|---|---|---|---|
| Astra coordinator | Run `run_73768f7c4dec` | terminal `term_892e5ca1-805d-4d8d-b27f-fe7c90994848` | active |
| Sol manager | `task_8f65a57496f6` | `ctx_bfbd07ec7a4c` | supervising corrected P1-04 XML validation; P2 is complete |
| Opus co-manager | `task_b15d7dad7f7b` | `ctx_f925bd9d4a52` | active through Astra for independent contract/diff review |
| GLM P0-02 writer | `task_f7788389960b` | `ctx_df9c602e28a2` | completed `df489bb`; correction review pending |
| GLM P0-02 correction | `task_7d03430673e1` | `ctx_51f56ba3df5c` | completed `51319b9`; reconciliation still open |
| GLM P0-02b reconciliation | `task_eb3e9e09ca7f` | `ctx_6ca746e225d0` | completed `972a814`; two direct regressions found |
| GLM P0-02c regression fix | `task_73facc46450a` | `ctx_673955f2284e` | `c7af159` integrated by Astra as `45d1393`; full gate passed |
| GLM P0-03 source byte lock | `task_c8c82a82db06` | `ctx_4d19cc8b7736` | `2a1034b` integrated by Astra as `9b7d4e2`; full gate passed |
| GLM P0-04 project skeleton | `task_5cf80c9529fa` + correction `task_a2ba0cb1dbe9` | `ctx_c88e36865f89` + `ctx_9a0e8d667681` | final `0b1743d` integrated by Astra as `cd411c4`; P0 complete |
| GLM P1-01 timebase | `task_3355d2bef6d9` | `ctx_e963c6fd31a4` | final `51b138d` integrated by Astra as `07e4dac`; 4,896 independent probes and full gate passed |
| GLM P1-02 FCPXML ingest | `task_d58951edec1b` | `ctx_614e1594110d` | final `b96684e` integrated by Astra as `ef12f32`; 1,045-test gate passed |
| GLM P1-03 media identity | `task_673b6cda51a8` | `ctx_6fa094276bb7` | final `287b46f` integrated by Astra as `7a107da`; 1,235-test integration gate passed |
| GLM P1-04 XML validation | `task_de39de51bd0a` | `ctx_78e4226f911c` | active from exact `7a107da`, terminal `term_5201b3a0-fc66-44f7-8623-ea2d78e4c9aa` |
| GLM P2-01 PCM extraction | `task_071c14e3282d` | `ctx_dcd31a44ebc6` | final `d301459` integrated by Astra as `5ea9719`; 906-test integration gate passed |
| GLM P2-02 PCM reference | `task_4428b9adca79` | `ctx_f12626850366` | final `b56c640` integrated by Astra as `35af34b`; 1,156-test integration gate passed |
| GLM P2-03 S17 regression | `task_7614fbbdde23` | `ctx_444387418221` | final `7ac575b` integrated by Astra as `3760cf9`; 1,250-test integration gate passed; settling |

Initial gate message IDs: root `msg_d8fc69fe164a`; Opus `msg_0f70e95b09f7`.

## Current evidence and gate

- Before the Sol planning series, root checkout HEAD was `9727b2be887a31e18b81152189cdd6352fce1e50` on `feat/final-cut-project`; Astra approved `676460111bad5ef04cbc9d5c776940611fa938a7` as the initial P0 product-code base.
- Python preflight is complete: `python3.12` and `.venv/bin/python` report `3.12.14`. A uv-created venv need not contain the `pip` module; package health is checked with `uv pip check --python .venv/bin/python`.
- Baseline `make check` at the initial source baseline passed on 2026-09-08: ruff lint/format, mypy (23 files), pytest (`389 passed`), and all 16 invariant groups.
- P0-02 completed on branch `Ssojux2/fcp-p0-contract` at `df489bb0a8c077ed6c74c9709dfa929432129d59`, exact base `676460111bad5ef04cbc9d5c776940611fa938a7`. Run `run_b6d40d052690`, Task `task_f7788389960b`, Dispatch `ctx_df9c602e28a2`, terminal `term_ea415fa1-9b4f-402d-922f-4d6dbe039b22`; focused 80 and full 453 tests passed, 18 invariant groups passed, and the worktree was clean.
- Pre-merge audit found five contract gaps in `df489bb`: optional imports are scoped to all project files rather than exact `project/vad/**`; the CLI exception skips every project import rather than only `project.commands`; `glm-*` is absent from the hardcoded-model check; FCPXML project model freezing is absent; xmllint accepts arbitrary constant argv rather than the exact two allowed shapes. Astra additionally reproduced executable override, opaque keyword, false provenance, extra positional, and valid alias handling defects and recorded the binding correction at `9727b2b`.
- Opus's first correction worker completed commit `51319b99635f86a68543d02783674d4bf32eb06b` from `df489bb`: Run `run_b6d40d052690`, Task `task_7d03430673e1`, Dispatch `ctx_51f56ba3df5c`, terminal `term_7a94892e-6f38-4421-8273-51b889ce2619`; 461 tests and `make check` passed for its narrower original defect set.
- Sol reproduced the still-open ratified reconciliation against `51319b9` with isolated source strings: `cli.py -> project.other`, NumPy under `project/vision`, a `glm-5.3-flash` literal, and `subprocess.Popen([exe, "--evil", path])` all returned no checker failure; project model dataclasses are still not inspected. Astra was escalated with messages `msg_c75ad9ff9e82` and `msg_dc58b3e29beb`; P0-02 remains unmergeable until a GLM correction closes these cases.
- P0-02b launched from the verified clean required base `51319b99635f86a68543d02783674d4bf32eb06b` using fresh `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`: child Run `run_8f41d4a91c5d`, Task `task_eb3e9e09ca7f`, Dispatch `ctx_6ca746e225d0`, terminal `term_0aa7004f-d352-48fa-aba5-5c31d848add3`. The dispatched brief embeds all five Astra reconciliation requirements and confines ownership to the checker, invariant tests, and contract doc.
- P0-02b committed `972a81412756b89fd330da622f22f14a067065e5`, with 149 focused and 522 full tests plus 19 invariant groups passing. Astra then reproduced two remaining static cases before review: ONNX Runtime outside VAD was not scanned and a `subprocess.run` function alias could bypass keyword/argv checks. Fresh P0-02c child Task `task_73facc46450a`, Dispatch `ctx_673955f2284e`, terminal `term_3aaadfca-ba88-4152-a686-747ec12deeb1` was launched to correct only those cases from base `972a814`.
- P0-02c committed `c7af1593e659f290323bdcde12c365e6740dabaf` with exactly `scripts/check_invariants.py` and `tests/test_invariants.py` changed. Its worktree initially contained an uncommitted owned test-only diff left by the settled P0-02b attempt after Astra reported the two regressions; the fresh worker disclosed it, an auto-mode `git checkout --` reset was denied, and the worker preserved/incorporated it into the mandated red-first matrix rather than bypassing permission. Sol then observed a clean committed tree, `git diff --check`, 176 focused tests, and 19 invariant groups passing; Astra independently passed 20 direct AST/temp-tree probes and full `make check` (549 tests, 19 invariant groups). Opus hit a service limit rather than completing review, so Astra used the authorized final-review fallback and integrated P0-02 as `45d1393f7e2c2e200868f9c5972ceb20d3409d95`; integration repeated the same full green gate.
- P0-03 committed `2a1034beaaea9cd15edd847e51f2d43444980dc0` from the explicitly authorized independent base `58bb25347319301391064d6ba9b6aad33a58f51c`, adding only `tests/test_source_immutability.py`. It locks exact inline UTF-8 SRT bytes for `demo`, `loopy`, `loopyletter`, and `loopylong` with ordinary `Config()` defaults, default repair/verification behavior, env discovery disabled, precise one-byte mutation diagnostics, and no expectation regeneration. Sol passed the four focused cases and structural probes; GLM reported 393 related/full tests and `make check`. The worker disclosed one unnecessary existence-only ancestor `.env` probe (`test -f`) performed before Sol stopped further probing; it printed no path/content and did not target `.env.example`. Astra used the service-limit fallback, integrated it as `9b7d4e2295729589805574e3f0ab9a947ce97af5`, and passed full `make check` with 553 tests and 19 invariant groups.
- P0-04's initial `acb85e4` and bounded correction `c17001a` added the five owned project-config/package paths, then exact one-path portability correction `0b1743d3b75d46946231cd3b0537ce09c7ff0cd1` replaced a private Python-3.12-only `__dataclass_params__.slots` assertion with public `__slots__`/instance behavior. Sol and Astra both passed actual CPython 3.11.16 probes plus direct checks. Astra integrated the final chain as `cd411c4376ccf548151dbd42a9e11c78bbfb4067`; the P0 phase join passed Ruff lint/format, mypy on 26 files, 579 tests, and 19 invariant groups.
- P1-01's initial `3fd514783497a60c9e862fc023101fb60b7b892f` expanded the frozen parser signature and misstated the point-interval edge. The same GLM Dispatch closed both findings in `51b138deee7bfc6a95704c0fec45b78310a44381`: signed epochs parse through the exact one-argument API, and half-open outward snapping documents/tests grid-aligned versus off-grid point behavior. Sol passed focused 269 tests plus 19 invariants; Astra passed full `make check` with 848 tests and 4,896 independent rate/anchor/order/inverse probes, then integrated it as `07e4dac41888ae115157ed4a68d6dfd5fbd5b233`.
- P1-02 launched from exact `07e4dac41888ae115157ed4a68d6dfd5fbd5b233` in the fresh `fcp-p1-ingest` worktree using `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`: Task `task_d58951edec1b`, Dispatch `ctx_614e1594110d`, terminal `term_233ac174-346d-4c89-9edb-3f1ff112afd3`. It owns only the frozen FCPXML models, parser, and focused test file.
- P1-02's final bounded correction `b96684e041a02825fd0aa0ed43c46fa1363c0308` (parent `05b6e3e`) rejects zero-`media-rep` and metadata-only assets at the asset path while retaining proxy-only and explicit silent-asset support. Sol verified a clean owned-path-only diff, `git diff --check`, and four focused positive/negative cases; Opus accepted the exact diff in `msg_db927ccc5acb`. Astra integrated it as `ef12f32b997819ac532de0c42af872ca45c59cf4` and passed Ruff, mypy on 28 files, 1,045 tests, and all 19 invariant groups.
- P1-03's final `287b46f2674cc1c18618ac89db57dd3414c8e44f` closes the strict media-map schema/path rules, absolute local XML file-URI rule, original regular-file precheck, bounded proxy readability fallback, resource-scoped `has_audio`, and content-hash identity in exactly `vlogsrt/project/media.py` and `tests/project/test_media.py`. Sol passed a fresh 1,124-test gate, Opus passed in `msg_7588c992877a`, and Astra integrated it as `7a107da516ce2665b883a618221a43e9b873bcba`; the integrated gate passed 1,235 tests, Ruff on 58 files, mypy on 30 files, and all 19 invariant groups.
- P1-04 launched from exact `7a107da516ce2665b883a618221a43e9b873bcba` in fresh branch `Ssojux2/fcp-p1-xml-validation` using custom `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`: Task `task_de39de51bd0a`, Dispatch `ctx_78e4226f911c`, terminal `term_5201b3a0-fc66-44f7-8623-ea2d78e4c9aa`. It owns only new `vlogsrt/project/xml_validation.py` and `tests/project/test_xml_validation.py` and uses the exact installed read-only FCPXML 1.14 DTD.
- P2-01's initial `9ce4e5b` omitted the global ffprobe stream index and accepted ambiguous selection failures; `9b9fb25` fixed the first review, and final `d3014597df5144093bb3a1ca57da54f8c0755b53` split the remaining no-audio, out-of-range, missing-index, and mapped-stream diagnostic paths. Sol passed 74 focused tests, 19 invariants, and full `make check` with 637 tests; Astra's integration gate passed 906 tests and mypy on 27 files, then integrated the branch as `5ea9719cb2fb3ece20ad228fa78481ae60bbdb3b`.
- P2-02 launched from exact `5ea9719cb2fb3ece20ad228fa78481ae60bbdb3b` in the fresh `fcp-p2-pcm-reference` worktree using `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`: Task `task_4428b9adca79`, Dispatch `ctx_f12626850366`, terminal `term_16b167fe-4bd5-4e05-980b-068da23374f8`. It owns only `vlogsrt/project/pcm.py` and `tests/project/test_pcm.py`; the brief fixes total audio-only `PcmReference` semantics and forbids fabricated references or P3 construction.
- P2-02's immutable-generation sequence culminated at `b56c64069058b550ffbbb643589b4c2efbeddff2`: exclusive generation PCM+metadata, one atomic logical pointer, recomputed content-key verification, no generation deletion, and best-effort post-commit temp cleanup preserve every issued reference across metadata/pointer failure, abrupt exit, and successful replacement. Sol and Opus (`msg_3939175c24ae`) passed the exact SHA; Astra integrated it as `35af34b4cdab2e0d96af2b8f6a5530c764e78d12` with 1,156 tests, Ruff on 56 files, mypy on 29 files, and all 19 invariant groups passing.
- P2-03 launched from exact `35af34b4cdab2e0d96af2b8f6a5530c764e78d12` in fresh branch `Ssojux2/fcp-p2-s17` using custom `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`: Task `task_7614fbbdde23`, Dispatch `ctx_444387418221`, terminal `term_fbd93677-fe9f-4bd1-b858-cdcc9e3d213c`. Ownership is only `vlogsrt/project/pcm.py` and `tests/project/test_pcm.py`; publication and legacy preprocess are explicitly closed, while actual 77-minute media evidence stays `PENDING_HUMAN`.
- P2-03's final `7ac575b3da0518c3ebd1b795acc63261f9c52541` retains exact snapped PCM coordinates with deterministic `range-NNNN.wav` leaves that stay inside the requested directory for traversal, absolute, and ordinary Korean/space-containing stems. Its evidence explicitly limits the regression to a reproduced synthetic legacy mismatch: the historical 77-minute cause, magnitude, and final-versus-intermediate behavior remain unverified and `PENDING_HUMAN`; range prevalidation prevents writes for invalid input but does not claim whole-batch atomicity. Sol passed all 126 PCM tests and a fresh full gate, root and Opus independently accepted the exact SHA, and Astra integrated it as `3760cf9148abfb03db2166f3665c904403ce702b` with Ruff on 58 files, mypy on 30 files, 1,250 tests, and all 19 invariant groups passing.
- Known user-owned dirty path is intentionally excluded from every inspection/commit operation. Its contents have not been read.

## Phase DAG and parallel groups

Arrows mean hard dependencies. A bracketed pair is the only permitted two-writer parallel wave; all other tasks are serial. Every phase ends at an Astra integration checkpoint, whose merge SHA becomes the next phase base.

```text
P0-01 environment evidence -> P0-02 source/derived contract guard -> P0-04 project package/config skeleton
P0-01 environment evidence -> P0-03 legacy SOURCE SRT byte lock (Astra dependency exception)
                           -> [P0-02/P0-04 lineage + P0-03] -> Astra P0 merge
                              |-> P1-01 rational time -> P1-02 FCPXML ingest
                              |                         -> [P1-03 media identity, P1-04 XML validation]
                              |                         -> P1-05 roundtrip -> Astra P1 merge
                              `-> P2-01 measured PCM extraction -> P2-02 PCM reference/slicing
                                                        -> P2-03 S17 regression -> Astra P2 merge

P1 merge + P2 merge -> P3-01 typed transcription outcome -> P3-02 per-asset adapter
                    -> P3-03 source trace/SOURCE SRT -> Astra P3 merge

P3 merge -> P4-01 logical retained slices -> P4-02 boundary request closure
         -> P4-03 retained-audio transcription -> P4-04 instance projection/locks
         -> Astra P4 merge

P3 merge -> P5-00 project config -> [P5-01 VAD artifact/runtime, P5-04 vision sampling]
P5-01 -> P5-02 VAD inference -> P5-03 guard union
P5-04 -> P5-05 saved-response vision policy
P5-03 + P5-05 -> Astra P5 merge

P4 merge + P5 merge -> P6-01 eligible block partition -> P6-02 deterministic EditPlan
                    -> P6-03 EDIT cue projection -> P6-04 atomic FCPXML/package export
                    -> Astra P6 merge

P6 merge -> P7-01 workspace revisions -> P7-02 budget/cancel/resume runner
         -> P7-03 CLI commands -> [P7-04 captions reproject, P7-05 project UI]
         -> P7-06 offline acceptance ledger -> Astra P7 integration
         -> PENDING_HUMAN live Final Cut/media/model acceptance
```

By explicit Astra dependency audit, P0-03 may run from `58bb25347319301391064d6ba9b6aad33a58f51c` while P0-02c is active: it owns only `tests/test_source_immutability.py` and consumes unchanged legacy source behavior, no checker API or project package. P0-04 still waits for corrected P0-02 integration; Astra joins all three reviewed results at the P0 checkpoint. P1 and P2 may execute concurrently after P0 integration, because they start from the same approved P0 SHA and own disjoint files; Astra must integrate both before P3. In P5, VAD and vision may execute concurrently only at the explicit first wave above; their follow-ups remain serial within each branch. P7-04 and P7-05 may run together only from the same integrated P7-03 SHA; they own disjoint reproject/commands versus UI paths and join before P7-06.

## Task ledger

| Task | Outcome | Depends on | Owned implementation files | State |
|---|---|---|---|---|
| [P0-01](tasks/p0-01-environment.md) | reproducible Python 3.12 venv and baseline evidence | bootstrap commit | none (ignored `.venv` only) | complete; evidence-only |
| [P0-02](tasks/p0-02-contract-guard.md) / [P0-02b](tasks/p0-02b-contract-reconciliation.md) / [P0-02c](tasks/p0-02c-contract-regressions.md) | source/derived dependency and subprocess contract | P0-01 | contract doc, pyproject, invariant script/tests | complete; integrated `45d1393`, full gate passed |
| [P0-03](tasks/p0-03-source-byte-lock.md) | exact synthetic legacy SOURCE SRT regression | exact `58bb253` by Astra exception; joins corrected P0-02/P0-04 at phase gate | `tests/test_source_immutability.py` only | complete; integrated `9b7d4e2`, 553-test gate passed |
| [P0-04](tasks/p0-04-project-skeleton.md) | isolated project package/config/FCPXML-model namespace skeleton | corrected P0-02 merge `45d1393` | project package/config/models + config test only | complete; P0 integrated `cd411c4`, 579-test gate passed |
| [P1-01](tasks/p1-01-rational-time.md) | exact FCP rational/frame math | P0 merge `cd411c4` | project time module/tests | complete; integrated `07e4dac`, full gate plus 4,896 probes passed |
| [P1-02](tasks/p1-02-fcpxml-ingest.md) | FCPXML/bundle parser and rejection surface | P1-01 merge `07e4dac` | project FCPXML models/parser/tests | complete; integrated `ef12f32`, 1,045-test gate passed |
| [P1-03](tasks/p1-03-media-identity.md) | original/proxy resolution and strong IDs | P1-02 merge `ef12f32` | project media/tests | complete; integrated `7a107da`, 1,235-test gate passed |
| [P1-04](tasks/p1-04-xml-validation.md) | xmllint-only boundary plus structural validation | P1-03 integration `7a107da` | XML validation/tests | active: `task_de39de51bd0a` / `ctx_78e4226f911c` |
| [P1-05](tasks/p1-05-roundtrip.md) | identity-preserving roundtrip | P1-03,P1-04 | project FCPXML writer/tests | drafted |
| [P2-01](tasks/p2-01-pcm-extraction.md) | measured 16 kHz PCM extraction metadata | P0 merge `cd411c4` | preprocess additions/tests | complete; integrated `5ea9719`, 906-test gate passed |
| [P2-02](tasks/p2-02-pcm-reference.md) | persistent hashed PCM and exact sample slicing | P2-01 merge `5ea9719` | project PCM/tests | complete; integrated `35af34b`, 1,156-test gate passed |
| [P2-03](tasks/p2-03-s17-regression.md) | last/intermediate/repair drift regression | P2-02 integration `35af34b` | project PCM helper/tests only | complete; integrated `3760cf9`, 1,250-test gate passed; historical 77-minute acceptance remains `PENDING_HUMAN` |
| [P3-01](tasks/p3-01-transcription-outcome.md) | explicit success/empty/error contract | P1,P2 merges | transcribe additions/tests | drafted |
| [P3-02](tasks/p3-02-source-adapter.md) | full legacy repair/refine engine once per unique source | P3-01 | project speech plus narrow repair-cutter seam/tests | drafted |
| [P3-03](tasks/p3-03-source-srt.md) | immutable source-local artifacts and pre-roughcut input-timeline SOURCE SRT | P3-02 | project captions/tests | drafted |
| [P4-01](tasks/p4-01-logical-slices.md) | coalesced retained logical slices | P3 merge | project boundary models/planner tests | drafted |
| [P4-02](tasks/p4-02-boundary-closure.md) | fixed-point boundary requests | P4-01 | project boundary planner/tests | drafted |
| [P4-03](tasks/p4-03-boundary-transcribe.md) | retained-audio-only BoundaryTranscript | P4-02 | project boundary runner/tests | drafted |
| [P4-04](tasks/p4-04-boundary-projection.md) | instance-scoped replacement and locks | P4-03 | project caption projection/tests | drafted |
| [P5-00](tasks/p5-00-project-config.md) | frozen project-only tunables/shared analysis vocabulary | P3 merge | project config/analysis models/tests | drafted |
| [P5-01](tasks/p5-01-vad-runtime.md) | pinned Silero model/license/runtime | P5-00 | `project/vad/` runtime/artifact/tests | drafted |
| [P5-02](tasks/p5-02-vad-inference.md) | channel-wise continuous-state inference | P5-01 | `project/vad/inference.py`/tests | drafted |
| [P5-03](tasks/p5-03-guard-union.md) | conservative source/instance speech guard | P5-02 | `project/vad/guard.py`/tests | drafted |
| [P5-04](tasks/p5-04-vision-sampling.md) | deterministic proxy/original frame samples | P5-00 | `project/vision/sampling.py`, preprocess seam/tests | drafted |
| [P5-05](tasks/p5-05-vision-policy.md) | strict saved-response decision parser | P5-04 | transcribe boundary + `project/vision/policy.py`/tests | drafted |
| [P6-01](tasks/p6-01-block-partition.md) | eligible 6–10 s blocks and candidates | P4,P5 merges | project edit partition/tests | drafted |
| [P6-02](tasks/p6-02-edit-plan.md) | immutable deterministic EditPlan | P6-01 | project edit plan/tests | drafted |
| [P6-03](tasks/p6-03-edit-captions.md) | EDIT SRT projection | P6-02 | project captions/tests | drafted |
| [P6-04](tasks/p6-04-package-export.md) | atomic linked FCPXML/SRT/review package | P6-03 | export/tests | drafted |
| [P7-01](tasks/p7-01-workspace.md) | content-keyed revisioned workspace | P6 merge | project workspace/tests | drafted |
| [P7-02](tasks/p7-02-runner.md) | budgets, cancellation, resumable analysis | P7-01 | project runner/tests | drafted |
| [P7-03](tasks/p7-03-cli.md) | project inspect/analyze/plan/export/captions/run | P7-02 | CLI/project command tests | drafted |
| [P7-04](tasks/p7-04-reproject.md) | supported user-recut caption reprojection | P7-03 | reproject/tests | drafted |
| [P7-05](tasks/p7-05-ui.md) | lightweight project UI flow | P7-03 | UI service/app/tests | drafted |
| [P7-06](tasks/p7-06-acceptance.md) | offline acceptance matrix and honest pending gates | P7-04,P7-05 | offline acceptance tests/docs | drafted |

## Review and integration protocol

1. Opus records shared-contract/phase findings under `docs/orchestration/reviews/`; Sol resolves ambiguity in task briefs before dispatch.
2. Each GLM commits, then reports an immutable review-ready SHA with launch receipt, exact base, branch, changed files, failing-first evidence, scoped tests, and `make check`; it waits on an Orca ask. A Sol pass routes the exact SHA to Astra but does not yet authorize `worker_done`.
3. Sol inspects scope and evidence. Opus reviews nontrivial shared contracts and phase diffs when available; Astra gives the final exact-SHA disposition. Corrections are issued to the same still-active GLM Dispatch, and only Astra acceptance releases it to `worker_done`; managers never edit product code.
4. After two repetitions of the same failure, request Terra high (root cause) and Luna high (minimal repro/test evidence), read-only. If synthesis fails, escalate to Opus, then Astra; code still returns to GLM.
5. Sol sends `merge_ready` with exact branch/commit/base/test/review data. Astra integrates, verifies, records the merge SHA, and only then releases the next phase.
