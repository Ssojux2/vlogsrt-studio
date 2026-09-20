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
| Sol manager | `task_88db49bbe723` | `ctx_7d23138a62b5` | recovered child Run `run_8f41d4a91c5d`; P4/P5-04 candidates reviewed, awaiting Astra dispositions |
| Opus co-manager | `task_1816b1e90bd0` | `ctx_4309d1be0a61` | active through Astra for independent contract/diff review |
| GLM P0-02 writer | `task_f7788389960b` | `ctx_df9c602e28a2` | completed `df489bb`; correction review pending |
| GLM P0-02 correction | `task_7d03430673e1` | `ctx_51f56ba3df5c` | completed `51319b9`; reconciliation still open |
| GLM P0-02b reconciliation | `task_eb3e9e09ca7f` | `ctx_6ca746e225d0` | completed `972a814`; two direct regressions found |
| GLM P0-02c regression fix | `task_73facc46450a` | `ctx_673955f2284e` | `c7af159` integrated by Astra as `45d1393`; full gate passed |
| GLM P0-03 source byte lock | `task_c8c82a82db06` | `ctx_4d19cc8b7736` | `2a1034b` integrated by Astra as `9b7d4e2`; full gate passed |
| GLM P0-04 project skeleton | `task_5cf80c9529fa` + correction `task_a2ba0cb1dbe9` | `ctx_c88e36865f89` + `ctx_9a0e8d667681` | final `0b1743d` integrated by Astra as `cd411c4`; P0 complete |
| GLM P1-01 timebase | `task_3355d2bef6d9` | `ctx_e963c6fd31a4` | final `51b138d` integrated by Astra as `07e4dac`; 4,896 independent probes and full gate passed |
| GLM P1-02 FCPXML ingest | `task_d58951edec1b` | `ctx_614e1594110d` | final `b96684e` integrated by Astra as `ef12f32`; 1,045-test gate passed |
| GLM P1-03 media identity | `task_673b6cda51a8` | `ctx_6fa094276bb7` | final `287b46f` integrated by Astra as `7a107da`; 1,235-test integration gate passed |
| GLM P1-04 XML validation | `task_de39de51bd0a` | `ctx_78e4226f911c` | final `182b786` integrated by Astra as `1fe07e5`; 1,274-test gate passed; worker settled |
| GLM P1-05 identity roundtrip | `task_0a4361c516ef` | `ctx_a29134bebdcf` | final `5f8885a` integrated by Astra as `d5a60e0`; 1,304-test gate passed; worker settled |
| GLM P3-01 transcription outcome | `task_c6df79a6d95d` | `ctx_99e3555a24d1` | actual `df2d3fda` integrated as `46ad02b`; 1,382-test gate passed; worker settled |
| GLM P3-02 source adapter | `task_070d3f42b435` | `ctx_706f2d3e4738` | final `5723ed5` integrated as `0a9854e`; 1,569-test gate passed; worker settled and terminal closed |
| GLM P3-03 SOURCE SRT | `task_f546e48b3c6a` | `ctx_2cae80fca959` | final `d1c3205` integrated as `6d5155d`; 1,681-test combined gate passed; worker reported succeeded and terminal closed |
| GLM P4-01 logical slices | `task_276718b7ae60` | `ctx_b598b33a6ec0` | candidate `4782720` passed Sol/root/Opus review; worker settled and terminal closed; integration disposition pending |
| GLM P5-00 project config | `task_09aa9485a647` | `ctx_b2aea13daff9` | corrected `a3c31c7` integrated as `e3dccba`; 1,479-test gate passed; worker settled |
| GLM P5-01 VAD runtime | `task_02c22fa2dc8f` | `ctx_98c9705f5edd` | final `0a0c711` integrated as `2440d6e`; 1,616-test gate passed; worker settled and terminal closed |
| GLM P5-02 VAD inference | `task_ff5c764d5d18` | `ctx_113216ab95db` | final `a578018` integrated as `31bac0b`; 1,645-test gate passed; completed process manually stopped/closed without forged worker success |
| GLM P5-04 vision sampling | `task_204b4c5bbeb7` | `ctx_6e0ceb2ee0f1` | candidate `950229e` completed and worker closed; Sol reproduced CFR-proof and odd-dimension scaling blockers, so integration is held |
| GLM P2-01 PCM extraction | `task_071c14e3282d` | `ctx_dcd31a44ebc6` | final `d301459` integrated by Astra as `5ea9719`; 906-test integration gate passed |
| GLM P2-02 PCM reference | `task_4428b9adca79` | `ctx_f12626850366` | final `b56c640` integrated by Astra as `35af34b`; 1,156-test integration gate passed |
| GLM P2-03 S17 regression | `task_7614fbbdde23` | `ctx_444387418221` | final `7ac575b` integrated by Astra as `3760cf9`; 1,250-test integration gate passed; worker settled |

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
- P1-04's final `182b786fb5ee7e6219dbdcdea28566ae1e1b8eae` normalizes XML operands to absolute paths before either frozen xmllint argv, preserves scoped missing-tool/spawn failures, and includes isolated regressions for nonexistent `--version`, a real dash-prefixed file, and ordinary relative input. Sol passed 200 focused tests, Opus passed in `msg_817975b45994`, and Astra integrated it as `1fe07e563c89958d2c771594d01645222d9df4aa`; the integrated gate passed 1,274 tests, Ruff on 60 files, mypy on 31 files, and all 19 invariant groups.
- P1-05 final `5f8885ad7c3b82d02b97b2dc350465a38708b0dd` preserves identity bytes and bounded roundtrip semantics in exactly its two owned paths. Sol and Opus passed the exact candidate; Astra integrated it as `d5a60e03a8f41603d9a2be9ebcd7373b3f156d04`, and the integrated gate passed Ruff on 61 files, mypy on 31 files, 1,304 tests, and all 19 invariant groups. Worker `task_0a4361c516ef` / `ctx_a29134bebdcf` reported completion in `msg_7e39fdf5e537`; release request `39f1d239-0e08-4ff9-9041-e0d83f63b0a3` returned retained because it is an external terminal. P1 and P2 offline scopes are complete; external Final Cut and historical 77-minute evidence remain `PENDING_HUMAN`.
- By Astra's narrower dependency exception, P3-01 launched alongside P1-05 from the same exact `1fe07e563c89958d2c771594d01645222d9df4aa` because its four legacy source files are disjoint and it consumes none of the P1-05 writer surface: Task `task_c6df79a6d95d`, Dispatch `ctx_99e3555a24d1`, terminal `term_e8ccdf92-6fac-4ab8-b963-070de645c6fd`, branch `Ssojux2/fcp-p3-transcription-outcome`. Root reproduced three blockers in first candidate `4b8f6681082b65878874da46ab75536385d8c04a`; the same Dispatch fixed them at actual exact `df2d3fda28efdfe95524c36d735464e11af24e16`. Sol verified the clean exact four-path diff and independently passed its 1,352-test gate; Opus passed the exact SHA in `msg_65c6fe3cda2f`. Astra integrated it as `46ad02b32fd2f201dabf7ad1466ae28aa8518fa1`, and the root gate passed Ruff on 61 files, mypy on 31 files, 1,382 tests, and all 19 invariant groups. Acceptance record `81fe02e0fb373665c9a3fd5ae36d2c964f7edd7a` became the next exact base. Worker completion `msg_03acbf3002d6` was released with retained/external-terminal receipt `94a7fa7e-ff04-48ba-a093-2341b86c2239`.
- P3-02 launched from exact `81fe02e0fb373665c9a3fd5ae36d2c964f7edd7a` in fresh branch `Ssojux2/fcp-p3-source-adapter` using custom GLM max/auto: Task `task_070d3f42b435`, Dispatch `ctx_706f2d3e4738`, terminal `term_0f77ae29-e6c6-4ef8-bcb1-bb123b6c13d0`. Ownership is only new `vlogsrt/project/speech.py`, new `tests/project/test_speech.py`, and the narrow repair seam in `vlogsrt/repair.py`/`tests/test_repair.py`. Its launch envelope fixes job-kind capture: `FIXTURE`/`RESCUE` require strict segments, while `ARBITER`/`VERIFY` accept a dict payload with explicit string `text` or explicit `segments` list of string-text objects, including an empty list, never a top-level list or timestamp synthesis.
- Astra authorized P5-00 to use the newly free second slot before the P3 merge because its four paths consume only the integrated P0 project config and define independent P5 vocabulary. Fresh custom GLM max/auto Task `task_09aa9485a647`, Dispatch `ctx_b2aea13daff9`, terminal `term_4ccec400-fbac-41b3-830a-d228412ad515` is active in `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-config` on `Ssojux2/fcp-p5-config` from exact `d5a60e03a8f41603d9a2be9ebcd7373b3f156d04`. Ownership is only `vlogsrt/project/config.py`, `vlogsrt.project.toml`, new `vlogsrt/project/analysis_models.py`, and new `tests/project/test_project_config.py`; the embedded brief includes the optional Decimal pricing/currency contract. P5-01/P5-04 still wait for P5-00 integration, and P5-03 still waits for integrated P4-04 projection.
- Root's WIP review of P5-00 first candidate `1f3974a7b276989c033048f2c6c3bdaa971da80a` passed its 1,397-test gate and found exactly two canonical-hash blockers: ambient Decimal context could round `Decimal.normalize()`, and signed float zero produced a distinct hash for the two accepted probability fields. The same Dispatch received the bounded correction in `msg_7fd967cfa7c9`; Opus reported all other surfaces clean in `msg_996277b26f68`, and credential-name regex expansion remains out of scope.
- P5-00 corrected candidate `a3c31c710896666931571417b8127b55d9f4f6f7` makes exact fixed-point Decimal serialization independent of ambient precision, strips only fractional trailing zeroes, folds signed Decimal zero, and normalizes float signed zero for `vad_positive` and `vision_apply_confidence`. Sol independently confirmed the clean four-file scope and passed Ruff on 62 files, mypy on 32 files, 1,401 tests, and all 19 invariant groups; Opus exact-SHA PASS is `msg_6d12d73ae82c`, and Sol sent Astra merge-ready evidence in `msg_d8e0338d4791`.
- Astra integrated P5-00 as `e3dccba31719b0b052f3be18061226d32a4ebbc3`; the integration gate passed Ruff on 62 files, mypy on 32 files, 1,479 tests, and all 19 invariant groups, and acceptance record `e23590475dfeea918bac23b05e24282aaf79191e` became the next exact base. Worker completion is `msg_e3aa806ffbf3`; release request `77f4c6aa-8179-442d-982b-0578e97f69d7` retained the external terminal. Fresh P5-01 GLM max/auto Task `task_02c22fa2dc8f`, Dispatch `ctx_98c9705f5edd`, terminal `term_77467e15-ccda-4ce8-91fe-f8c0516a4beb` is active in `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-vad-runtime` on `Ssojux2/fcp-p5-vad-runtime` from that exact clean acceptance SHA.
- Root+Opus found that P3-02's original durable `<namespace>/<stem>.SOURCE.srt` path collides across valid changed-config/policy analyses. The same active Dispatch received `msg_03f5a9e8f98e`: durable source-local SRTs are keyed as `<asset_content_id>/<analysis_key>/<stem>.SOURCE.srt`, while fixture namespace and scratch locations stay unchanged; same-key publication is exclusive, byte-identical reuse is idempotent, conflicting bytes preserve the existing artifact and produce an error trace.
- Root's next synthetic P3-02 probes reproduced an over-limit quiet cut (1.2 seconds under a one-second limit), whitespace-only successful segments misclassified as literal empty, and a PCM key incorrectly reused as the SOURCE analysis key across changed transcription config. The same active GLM Dispatch received all three bounded corrections in `msg_9def4fbe28e8`: constrain cuts to seconds and actual WAV-byte caps while reserving remaining samples, treat only explicit outcome-empty as successful empty, and derive a domain-separated SOURCE key from the PCM key plus canonical source config/model and adapter schema while retaining the PCM key as provenance.
- Root also proved that first candidate `28f00a235a0770b25169850adfdeec2040a7dd7c` accepted `has_audio` declarations contradicting their real/unavailable variants and returned duplicate output objects for repeated identical inputs. Corrections routed in `msg_214d993fd161` and `msg_0b14a074fbfb` added whole-batch pre-call declaration/variant validation and first-order output normalized once per SOURCE key or unavailable resource; later rounds also closed sub-sample chunk limits and completed-stage error evidence. Final candidate `5723ed5d6a5cf1b657509a500f5883ca3f0c3671` passed Sol, Opus, and root review, was integrated as `0a9854e8bbf5cd8842fb2af386b422fde7358780`, and passed the 1,569-test/Ruff64/mypy33/19-invariant gate; acceptance `304ad68926eea35d3aaad7e548566ae337265e75` is the P3-03 base.
- P3-03 final `d1c3205f99895a756b912146fee715fc73f07498` passed Sol, Opus, and root review, and Astra integrated it as `6d5155d5e828907dd6c6aba2320e4d4ce216529e`. Its lifecycle completion is `msg_5a250e4f4258`; exact external-terminal release retained the process, so root-authorized idle proof preceded closing `term_48b7be5b-eec6-4660-81bf-e795af85c194`. Opus withdrew the repeated summary-ID concern after confirming per-placement identity/flags preserve P4-04 semantics.
- P5-01's immutable correction chain ended at `0a0c7119a5a3b536b45983310c466839d1f42e24`: official Silero v6.2.1 model/license bytes are installed package resources, public artifact/session boundaries fail closed through contextual `VadUnavailable`, and CPU-only one-thread metadata plus real inference were verified from an isolated installed wheel. Sol, Opus, and root accepted the exact SHA; Astra integrated it as `2440d6e760f43e5cc5fdb8f252dcfb25b3fd79c6`, and the gate passed 1,616 tests, Ruff on 67 files, mypy on 35 files, and all 19 invariant groups. Acceptance `9c5bccf906ae0f1c57f80497eb6590db5c067a1c` is the P5-02 base.
- P5-02 final `a578018130835126a0d7686a4be0769b9f5765f4` passed Sol, Opus, and root review, and Astra integrated it as `31bac0b518fac6393458b0afbe54fd1402622fff`; the P5 integration gate passed 1,645 tests, Ruff on 69 files, mypy on 36 files, and all 19 invariant groups. The completed worker's long polling process was manually stopped and exact terminal `term_f78422fd-e4ce-429c-99d5-b7ea31a92a50` closed under the user's cleanup instruction; it emitted no `worker_done`, so lifecycle state remains failed/manual-stop and is not represented as worker success.
- Combined P3/P5 acceptance `5bfcc463a844d1caae91c0cac450754f47c4740d` records both integrated candidates, the combined 1,681-test/Ruff71/mypy37/19-invariant gate, and Opus final PASS `msg_e7422ef19aa4`. Fresh P4-01 Task `task_276718b7ae60`, Dispatch `ctx_b598b33a6ec0`, terminal `term_01bc8c90-da88-45f8-9c13-e09b52420834` is active in `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p4-logical-slices` on `Ssojux2/fcp-p4-logical-slices` from that exact SHA using GLM max/auto. Root ratified the common file/XML/decoded-frame clock bridge in `msg_0caddd2056cd`; the binding spec plus P5-03/P5-04/P6-01 briefs carry the exact formulas, original-mapping error rule, and request dedup contract. Root's CFR clarifications `msg_ed8782640edc` and `msg_d947c7f78cf2` further fix P5-04 proof to exact integer PTS/time-base validation with a declared-tick-derived quantization bound, distinguishing ordinary quantized CFR from a nonuniform frame jump.
- P4-01 candidate `478272097b09546f1334416cee3e621fdd37787d` has exact parent `5bfcc463a844d1caae91c0cac450754f47c4740d` and only `vlogsrt/project/boundary.py` plus `tests/project/test_logical_slices.py`. GLM reported 33 focused/1,714 full tests, Sol independently passed the focused file, root passed its exact-candidate gate, and fresh Opus review `msg_49be392869fa` returned PASS with only low residuals: opaque `KeyError` for a hand-reconstructed dangling clip resource, unpinned coalescing of different authored states that both derive `audible=False`, missing direct shuffled-media regression, and no NTSC multi-blade regression. The worker Dispatch was released and the exact idle terminal closed; Astra integration disposition remains pending.
- P5-04 candidate `950229ec8f83a9dc65488c743c9d86b22f05553e` has exact parent `1cc81eb5ed3b85000a5e19de4b0807dfecfb5e4a`, five owned paths, and worker evidence of 42 new tests plus a 1,741-test full gate. Sol held integration in `msg_475d39fa0293`: the timestamp proof accepts exact-25-fps PTS `[0,40,81,120]` at `time_base=1/1000` despite the nonuniform 40/41/39 tick steps, the even-dimension scale turns an actual 65x65 input into 66x65 despite never-upscale/aspect requirements, and the probe omits accepted-brief validation of `avg_frame_rate` and declared count/range metadata. The completed worker was released, proved idle, and exact-closed; a fresh bounded GLM correction requires Astra authorization and an exact base.
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
                              |                         -> [P1-05 roundtrip, P3-01 outcome]
                              |                            (Astra dependency exception at exact `1fe07e5`)
                              |                         -> P1-05/P3-01 join -> P3-02 per-asset adapter
                              `-> P2-01 measured PCM extraction -> P2-02 PCM reference/slicing
                                                        -> P2-03 S17 regression -> Astra P2 merge

P3-02 -> P3-03 source trace/SOURCE SRT -> Astra P3 merge

P1-05 merge -> [P3-01 correction, P5-00 project config]
                 (Astra independent-config exception for P5-00 at exact `d5a60e0`)
P3 merge -> P4-01 logical retained slices
P4-01 -> P4-02 boundary request closure -> P4-03 retained-audio transcription
      -> P4-04 instance projection/locks -> Astra P4 merge

P5-00 -> [P5-01 VAD artifact/runtime, P5-04 vision sampling]
P5-01 -> P5-02 VAD inference
P5-02 + P4 merge -> P5-03 guard union
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

By explicit Astra dependency audit, P0-03 may run from `58bb25347319301391064d6ba9b6aad33a58f51c` while P0-02c is active: it owns only `tests/test_source_immutability.py` and consumes unchanged legacy source behavior, no checker API or project package. P0-04 still waits for corrected P0-02 integration; Astra joins all three reviewed results at the P0 checkpoint. P1 and P2 may execute concurrently after P0 integration, because they start from the same approved P0 SHA and own disjoint files. After P1-04 and P2 were integrated at exact `1fe07e5`, Astra made a narrower exception for disjoint P1-05 and P3-01: P3-01 consumes unchanged legacy Config/Chunk/Segment/build_spine and no P1-05 symbol. P3-02 still waits for corrected P3-01 integration, so this exception cannot claim a P3 phase completion early. After P1-05 integrated as exact `d5a60e0`, Astra made a second narrow exception for P5-00: its four config/vocabulary paths consume no unfinished P3 surface, so it may run beside the P3-01 correction from that explicit base. P4-01 still waits for the P3 merge; P5-01 and P5-04 wait for P5-00 integration. While the serial P4 chain advances, at most one ready disjoint P5 task may occupy the other writer slot; P5-03 additionally depends on the integrated P4-04 projection and cannot start earlier. After P4 completes, both writer slots may be used by ready disjoint VAD/vision tasks, still respecting each branch's hard dependencies. P7-04 and P7-05 may run together only from the same integrated P7-03 SHA; they own disjoint reproject/commands versus UI paths and join before P7-06.

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
| [P1-04](tasks/p1-04-xml-validation.md) | xmllint-only boundary plus structural validation | P1-03 integration `7a107da` | XML validation/tests | complete; integrated `1fe07e5`, 1,274-test gate passed |
| [P1-05](tasks/p1-05-roundtrip.md) | identity-preserving roundtrip | P1-03,P1-04 | project FCPXML writer/tests | complete; integrated `d5a60e0`, 1,304-test gate passed |
| [P2-01](tasks/p2-01-pcm-extraction.md) | measured 16 kHz PCM extraction metadata | P0 merge `cd411c4` | preprocess additions/tests | complete; integrated `5ea9719`, 906-test gate passed |
| [P2-02](tasks/p2-02-pcm-reference.md) | persistent hashed PCM and exact sample slicing | P2-01 merge `5ea9719` | project PCM/tests | complete; integrated `35af34b`, 1,156-test gate passed |
| [P2-03](tasks/p2-03-s17-regression.md) | last/intermediate/repair drift regression | P2-02 integration `35af34b` | project PCM helper/tests only | complete; integrated `3760cf9`, 1,250-test gate passed; historical 77-minute acceptance remains `PENDING_HUMAN` |
| [P3-01](tasks/p3-01-transcription-outcome.md) | explicit success/empty/error contract | exact `1fe07e5` by Astra dependency exception; P1-05 join complete | transcribe additions/tests | complete; integrated `46ad02b`, 1,382-test gate passed |
| [P3-02](tasks/p3-02-source-adapter.md) | full legacy repair/refine engine once per unique source | P3-01 acceptance `81fe02e` | project speech plus narrow repair-cutter seam/tests | complete; integrated `0a9854e`, 1,569-test gate passed |
| [P3-03](tasks/p3-03-source-srt.md) | immutable source-local artifacts and pre-roughcut input-timeline SOURCE SRT | P3-02 acceptance `304ad689` | project captions/tests | complete; `d1c3205` integrated as `6d5155d`, combined 1,681-test gate passed |
| [P4-01](tasks/p4-01-logical-slices.md) | coalesced retained logical slices | P3 merge | project boundary models/planner tests | complete; integrated `f5be5bc` |
| [P4-02](tasks/p4-02-boundary-closure.md) | fixed-point boundary requests | P4-01 | project boundary planner/tests | complete; candidate `092689b` integrated as `8742ebf` |
| [P4-03](tasks/p4-03-boundary-transcribe.md) | retained-audio-only BoundaryTranscript | P4-02 | project boundary runner/tests | complete; candidate `c7b6c4f` integrated as `f6af955`, 1,898-test gate passed |
| [P4-04](tasks/p4-04-boundary-projection.md) / [P4-04b](tasks/p4-04b-timeline-anchor.md) | instance-scoped replacement and locks | P4-03 | project caption projection/tests | complete; `0ef58b2`+`f3475f6` integrated as `cb6b039`, 2,058-test gate passed; **P4 phase closed** |
| [P5-00](tasks/p5-00-project-config.md) | frozen project-only tunables/shared analysis vocabulary | exact `d5a60e0` by Astra independent-config exception | project config/analysis models/tests | complete; integrated `e3dccba`, 1,479-test gate passed |
| [P5-01](tasks/p5-01-vad-runtime.md) | pinned Silero model/license/runtime | P5-00 acceptance `e2359047` | `project/vad/` runtime/artifact/tests | complete; integrated `2440d6e`, 1,616-test gate passed |
| [P5-02](tasks/p5-02-vad-inference.md) | channel-wise continuous-state inference | P5-01 acceptance `9c5bccf` | `project/vad/inference.py`/tests | complete product result; `a578018` integrated as `31bac0b`, lifecycle manually stopped |
| [P5-03](tasks/p5-03-guard-union.md) / [P5-03b](tasks/p5-03b-guard-identity-rails.md) | conservative source/instance speech guard | P5-02 and integrated P4-04 projection | `project/vad/guard.py`/tests | complete; `f2a798f`+`5387c6c` integrated as `4e185f3`, 2,094-test gate passed; **P5 phase closed** |
| [P5-04](tasks/p5-04-vision-sampling.md) | deterministic proxy/original frame samples | P5-00 | `project/vision/sampling.py`, preprocess seam/tests | complete; corrected candidate `ec4e2b2` integrated as `6a9a98f` |
| [P5-05](tasks/p5-05-vision-policy.md) / [P5-05b](tasks/p5-05b-vision-policy-rails.md) | strict saved-response decision parser | P5-04 | transcribe boundary + `project/vision/policy.py`/tests | complete; chain `2a25009`->`f3a4911`->`693aa5b`->`05a7455` integrated as `14b60bf`, 2,022-test gate passed |
| [P5-06](tasks/p5-06-upstream-identity-rails.md) | request identity on decisions; constructor-level config rails | Fable P6 pre-phase review | `project/config.py`, `project/vision/policy.py`/tests | complete; `3b33c53` integrated as `eeb8e7a`, 2,152-test gate passed |
| [P6-01](tasks/p6-01-block-partition.md) / [P6-01b](tasks/p6-01b-sample-clamp-and-rails.md) | eligible 6–10 s blocks and candidates | P4,P5 merges + P5-06 | project edit partition/tests | complete; `be2c570`+`d9cfd5c` integrated as `8a1135f`, 2,298-test gate, 20th invariant added |
| [P6-02](tasks/p6-02-edit-plan.md) / [P6-02b](tasks/p6-02b-validator-and-runs.md) | immutable deterministic EditPlan | P6-01 | project edit plan/tests | complete; `bde0368`+`0422987` integrated as `ad80db5`, 2,368-test gate; synthetic roughcut shortens 20s→13s |
| [P6-03](tasks/p6-03-edit-captions.md) / [P6-03b](tasks/p6-03b-removal-check-order.md) | EDIT SRT projection | P6-02 | project captions/tests | complete; `a022f96`+`d52c809` integrated as `97e1063`, 2,392-test gate |
| [P6-04](tasks/p6-04-package-export.md) | atomic linked FCPXML/SRT/review package | P6-03 | export/tests | drafted |
| D34 | vision judge moved to Google `gemini-3.8-flash`; hardcoded-model guard widened to `(?:glm\|gemini)-` | — | project config, invariant checker, contracts, tests | complete; **`b1fbc45`**, 2,396-test gate, 20/20 invariants, mutation-proven |

### 2026-09-13 replan — cut-first ordering and the Final Cut plugin

The product order is inverted (**cut → caption**) and the delivery form is a **Final Cut
Workflow Extension**. Binding revision: [`docs/plans/2026-09-13-cut-first-and-plugin.md`](../plans/2026-09-13-cut-first-and-plugin.md).
Direction review: [`astra-replan-cut-first.md`](reviews/astra-replan-cut-first.md) — **UNSOUND**
on the first draft (6 blocking), three defects reproduced with synthetic calls; all twelve
findings folded in, and the twelve reversed Opus claims are listed in that plan's §7.

**Execution order: X-01 (human) · P6-04b → R-00 → rest of R → P7 (rewritten) → rest of X.**

| Task | Outcome | Depends on | Owned files | State |
|---|---|---|---|---|
| **R-00** | rule 4/9 amendment and the derived-edit audit contract (**D35**) | — | `CLAUDE.md`, `DECISIONS.md`, plans | complete; **`19e80d8`** — legacy rules kept verbatim, derived-layer clause added narrowly; 20/20 invariants |
| **R-01** | availability locks only, derived from media + PCM | R-00 | new `project/availability.py` | planned |
| **R-02** | `SpeechEvidence` guard; no empty default, evidence in revision | R-00 | `vad/guard.py`, `project/config.py`, `edit.py`, export | planned |
| **R-03** | per-retained-range independent engine run; requested/completed/failed range types | R-02 | `project/speech.py`, `project/boundary.py` | planned |
| **R-04** | `caption_timeline` — captions from whatever timeline is given | R-03 | new `project/recaption.py` | planned |
| **R-05** | candidate-block verification transcription (safety level A) | R-02 | `edit.py` or new module | planned |
| **R-06** | `analysis_complete`/`review_required`/approval/`caption_complete`, bound to the cut revision | R-02 | `edit.py`, export, workspace | planned |
| **R-07** | cut-only package; caption-only artifact without an `EditPlan` | P6-04b, R-06 | `project/export.py` | planned |
| **R-08** | input adapter wrapping a single browser clip as a project | — | `project/fcpxml.py` or new module | planned |
| **R-09** | remove Gradio entirely; reverse D11/D14 (**D36**) | — | delete `ui/**`, `pyproject.toml`, invariant checker, contracts §1·§6 | planned, no dependencies |
| [P7-01](tasks/p7-01-workspace.md) | content-keyed revisioned workspace | R phase | project workspace/tests | **rewrite required** — partial request keys, completed/failed state, cut↔caption revision relation |
| [P7-02](tasks/p7-02-runner.md) | budgets, cancellation, resumable analysis | P7-01 | project runner/tests | **rewrite required** — brief demands full-source cost and bans network in `captions`; both conflict with the new order |
| [P7-03](tasks/p7-03-cli.md) | `project inspect\|cut\|approve\|caption` | P7-02 | CLI/project command tests | **rewrite required** — no single `run` command; it would bypass the human gate |
| [P7-04](tasks/p7-04-reproject.md) | supported user-recut caption reprojection | P7-03 | reproject/tests | **shrink only after** identity rebind / stale / cache / failure disposition actually move into R-04 |
| [P7-05](tasks/p7-05-ui.md) | lightweight project UI flow | — | — | **dropped** (R-09) |
| [P7-06](tasks/p7-06-acceptance.md) | offline acceptance matrix and honest pending gates | P7-04 | offline acceptance tests/docs | rewrite — drop UI dependency, add the new state machine |
| **X-01** | drag-in payload, Apple-events return, real `FCPXTimeline` capability | — | spike only | **PENDING_HUMAN** — needs Xcode + installed FCP; it decides the I/O contract, so it leads |
| **X-02**…**X-06** | app + extension shell, engine bundling, helper/entitlements, result return, signing | X-01, engine | new macOS targets | planned |

## Review and integration protocol

1. Opus records shared-contract/phase findings under `docs/orchestration/reviews/`; Sol resolves ambiguity in task briefs before dispatch.
2. Each GLM commits, then reports its immutable review-ready SHA with launch receipt, exact base, branch, changed files, failing-first evidence, scoped tests, and `make check`. Once the bounded deliverable and evidence are complete, it sends `worker_done` exactly once and exits; finished coding workers never enter prolonged review polling loops.
3. Sol inspects scope and evidence. Opus reviews nontrivial shared contracts and phase diffs when available; Astra gives the final exact-SHA disposition. Post-completion corrections use a fresh bounded GLM Dispatch from the exact candidate or accepted base, while managers never edit product code. Settled workers are released immediately; an external terminal is closed only after exact completion/idle proof and explicit cleanup authority.
4. After two repetitions of the same failure, request Terra high (root cause) and Luna high (minimal repro/test evidence), read-only. If synthesis fails, escalate to Opus, then Astra; code still returns to GLM.
5. Sol sends `merge_ready` with exact branch/commit/base/test/review data. Astra integrates, verifies, records the merge SHA, and only then releases the next phase.

## 2026-09-10 coordination handover to Opus 5

The user simplified the manager layer on 2026-09-10: Opus 5 coordinates directly in place of the
Astra/Sol pair. Opus decomposes the remaining plan into task briefs, dispatches each one to a fresh
`claude-glm glm-5.3[1m] --effort max --permission-mode auto` worker, verifies the candidate itself,
escalates to Fable 5.1 only when verification finds a defect, and performs the merge into
`feat/final-cut-project`. Run `run_a091d4869a98`, coordinator terminal
`term_7c509adc-43bc-40e3-ab6c-0e374d516231`. The earlier Astra/Sol runs stay stopped.

P4-03 was verified independently at exact `c7b6c4f` in its own worktree (Ruff lint/format, mypy 40
source files, 1,898 tests, 19/19 invariant groups) and reviewed against its brief before merging.
The review confirmed that the cache key carries no path/stem material, that linkage closes only on
`(source_id, pcm_analysis_key)` with duplicate rejection plus a separate `pcm_sample0_file_time`
origin proof, that `_require_retainable` bounds every request before any call, that
`_clamped_repair_range` clamps nested repair cuts inside the retained range and dies rather than
padding, that the local -> decoded -> file clock conversion happens exactly once per layer, and that
`_finite`/`_bad_count` reject NaN, infinity, and `bool` in numeric positions. Integrated as
`f6af955`; the post-merge `make check` passed with the same 1,898-test/19-group result.

P5-05 remains unintegrated. Root reproduced an acceptance bypass on exact `693aa5b`: `ProjectConfig`
validates its numeric ranges only inside `load_project_config`, so a programmatically constructed
config skips validation, and `max(float("nan"), 0.8)` is NaN while `0.5 < NaN` is false — a
confidence-0.5 candidate is returned as `accepted`. `min(float("nan"), 16)` defeats the frame cap the
same way. Task P5-05b (`docs/orchestration/tasks/p5-05b-vision-policy-rails.md`, Orca
`task_35a3d06e8b32` / `ctx_b488232db344`) requires a fail-closed `VisionPolicyError` reached from
both `build_payload` and `parse_decision`, positioned outside the `try` that manufactures
`_error_decision`, so a caller bug can never be laundered into a "the model misbehaved" record.

## P5-05b outcome, 2026-09-10

GLM closed the rail bypass in `05a7455` by making `_require_config(cfg)` the first gate of both
`build_payload` and `parse_decision`, positioned outside the `try` that manufactures
`_error_decision`, so a caller's bad config raises `VisionPolicyError` instead of being laundered
into a "the model misbehaved" `keep_all` record. The check excludes `bool`, requires
`math.isfinite`, and requires `[0.0, 1.0]` for `vision_apply_confidence`; `vision_max_images` must
be a non-`bool` `int` >= 1. The rail directions are unchanged: config may only tighten.

Opus verified the candidate independently rather than trusting the report. The worker's claim of
"15/15 invariant groups" was a reporting miscount — the actual run prints all 19 groups `[OK]`.
Ruff, mypy (41 source files), and 1,928 tests pass at exact `05a7455`. Opus then re-ran the exact
bypass on the fixed tree: NaN, +/-inf, -0.5, and `True` for `vision_apply_confidence`, and NaN, 0,
and `True` for `vision_max_images`, all raise from both entry points. A control run confirmed
`accepted` is still reachable at confidence 0.93 under the default 0.8 floor, that a configured
0.95 floor pushes 0.93 down to `keep_all`, and that a configured 0.10 cannot lower the floor below
0.8 — so the closures above are real refusals, not a uniformly broken parser. Integrated as
`14b60bf`; post-merge `make check` passed with 2,022 tests and 19/19 invariant groups.

P4-04 (`task_2607661f58fe` / `ctx_fc4e96c3bf70`) runs in parallel from base `9747988` in
`fcp-p4-boundary-projection`, owning `vlogsrt/project/captions.py`, `vlogsrt/project/boundary.py`,
and a new `tests/project/test_boundary_projection.py`.

## P4-04 outcome and the Fable adjudication, 2026-09-10

The first candidate `0ef58b2` passed every gate (1,931 tests, 19/19 groups) but review found a
contract defect: `project_boundaries` reconstructed the per-slice timeline shift `k` by
intersecting, over the slice's clamped cues, the bounds each cue implies. It did that because
`BoundaryPlan` carries only `clamped_cues`/`requests`/locks/unavailable and drops the
`LogicalSlice` records that hold the exact anchor. The task contract caused this, not the worker.

Opus raised three grounds and Fable 5.1 high adjudicated each independently. Two were refuted,
and that matters more than the confirmation: the premise that `k` is constant within a slice is
exactly true (`_continues` forces equal source/timeline end-start pairs across coalesced blades,
retime is rejected at ingest); ground 1 (derives a time) is CONFIRMED as a contract defect but
not a literal rule-1 breach, since whisper times are untouched and what is reconstructed is an
XML editorial offset; grounds 2 (can fail on valid input) and 3 (can silently mis-pick) are
REFUTED for plan-produced input by an algebraic argument — non-narrowing implies exactly one cue
crossing a slice edge, where both hypotheses equal `k` or fall out of range — corroborated by a
fuzz of 6,000 frame-aligned worlds: 7,191 requests, 1,209 in the non-narrowing branch, zero
raises and zero wrong shifts. A hand-built request narrower than its cue raises rather than
mis-picks. Full write-up: `reviews/fable-p4-04-timeline-shift.md`.

The correction was therefore dispatched as a hardening, with the worker told plainly that its
code had been adjudicated correct so it would not hunt a nonexistent bug. `f3475f6` adds
`BoundaryRequest.timeline_start`, set in `_sealed_request` to
`slice.timeline_start + (start - slice.source_start)` — the binding formula verbatim — computes
the shift locally in `_boundary_project_cues`, deletes `_slice_timeline_shift` and its
bookkeeping, and adds `_require_anchor_matches_cue` as a check that closes on contradiction
rather than a fallback that computes.

Identity preservation was verified rather than trusted. `_request_id` and `_boundary_cache_key`
are byte-identical before and after (function-body diff), and the worker's pinned digest
`b9bd7014...` was re-run by Opus inside a throwaway worktree checked out at the pre-change
commit `0ef58b2`, where it passes — so the pin is genuine and not circular. Independent gate at
`f3475f6`: Ruff, mypy 40 files, 19/19 invariant groups, 1,934 tests. Integrated as `cb6b039`
with a 2,058-test post-merge `make check`.

One asymmetry Fable flagged for P6, not a P4-04 defect: a source cue straddling an internal
blade boundary becomes two `ProjectCue`s (one per instance, same `cue_id`), while a boundary cue
spanning several blades is a single `ProjectCue` carrying all `request.instance_ids`.

## 2026-09-16 PROTO-6 로컬 스모크

PROTO-6 브리프 §2의 실측이다. 테스트가 아니라 **이 머신에서 실제로 한 번** 돌렸다 —
실제 ffmpeg·ffprobe(/opt/homebrew/bin)·xmllint(/usr/bin)·Silero ONNX(번들 아티팩트,
venv의 numpy 2.5.3·onnxruntime 1.30.0). 유료 호출 0회(무플래그 = 오프라인 기본).
스크래치 디렉터리는 `$TMPDIR/proto6-smoke.Ruy9eu`(커밋 안 함).

**재료.** 합성 미디어:

```
ffmpeg -f lavfi -i "testsrc2=duration=10:size=320x240:rate=30" \
       -f lavfi -i "sine=frequency=440:duration=10" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 64k -shortest clip.mp4
```

10.000초·30fps·AAC. 손으로 쓴 최소 FCPXML(`version="1.14"`, format r1 frameDuration
1/30s, asset r2 hasAudio="1" + `media-rep kind="original-media"`, asset-clip 하나
offset/start 0s duration 10s, sequence duration 10s)은 **파서가 그대로 받았다** — 거부
문구를 기록할 일은 없었다. 프로젝트 설정은 `schema_version = 1` 한 줄.

**명령.**

```
.venv/bin/python -m vlogsrt.cli project run $SMOKE/smoke.fcpxml \
    -o $SMOKE/out --project-config $SMOKE/smoke.project.toml
```

**1차 실행 — 종료 코드 0.** stdout 전문:

```
최종 판정: 차단 없음
리비전: …/out/package/revisions/1ae323f30a305aff908620a0426c6fe7093d4f2ebf93696709eb4fd7aa7a056f
판정 0개 · 제거 구간 0건
DTD 대조 안 함
```

stderr 전문(기본 로그 레벨): `오디오 준비 와료: 리소스 1개 — PCM 1, 부재 0` / `분석
완료: 인스턴스 가드 1개 — 소스 가드 1, 경계 재전사 0, 부재 인스턴스 0, VAD 실패 0` /
`출판 완료: 블록 0개 — 판정 0개 (오류 0), 제거 0건, 리비전 1ae323f30a30`.

산출: `OUT/artifacts/`(PCM `reference.pcm` 160000프레임=10초@16kHz + 세대 기록 + 전사
조각 `initial-0000.wav`), `OUT/package/current` → 리비전 다섯 파일
(`VLOG_AI_EDIT.fcpxml` 781B · `VLOG_AI_EDIT.srt` **0B** · `VLOG_AI_SOURCE.srt` **0B** ·
`project_trace.json` · `review.json`). review.json: `final_accepted: true`,
`final_blockers: []`. 내보낸 FCPXML은 전체 보존 사본이다(300프레임 그대로, 새
event/project 이름 `VLOG_AI_EDIT`, 원본 media-rep 재묶음) — 단독 `xmllint --noout`도
통과.

**픽스처 없는 클립의 실제 동작 — 사람이 반드시 알아야 할 것.** 예외도 종료 2도 막힌
패키지(1)도 아니다. 오프라인 전사기가 `픽스처가 없습니다: …/clip.whisper.json`으로
죽고, 그 예외는 소스 **error 기록**(in-memory `SourceTrace.status="error"`)으로 바뀐다.
`boundary._resolve_slices`가 error 소스의 인스턴스 전부를 **잠근다**(boundary.py:649) —
계획의 `retained` 이유가 `kept_locked`인 것이 그 흔적이다. 잠긴 클립은 컷 후보가 못
되므로 블록 0 · 판정 0 · 제거 0이고, `_final_blockers`는 아무 코드도 보지 못해
`final_accepted: true`로 **종료 0**에 자막 0장인 패키지가 나온다. 즉 실패 닫힘(보존)은
지켜지지만 **실패의 이유가 어디에도 남지 않는다**: stderr의 요약 3줄에 없고,
`VLOGSRT_LOG_LEVEL=DEBUG`에서도 없고(별도 OUT으로 재실측), review.json·
project_trace.json·artifacts 어디에도 error 문구가 없다. 제품 코드는 고치지 않았다
(범위 밖) — RUNBOOK의 "오프라인의 실제 모습" 절에 이 사실을 적었다.

**실제로 돌았던 것:** ffprobe 오디오 probe(전역 1번)·ffmpeg PCM 추출(로그 "PCM 추출
완료 … 원점 0초")·Silero ONNX VAD("VAD 실패 0" — 세션 생성 실패였으면 VadUnavailable
하드 실패로 exit 2였을 것)·xmllint(게시 관문 안). 프레임 표본의 ffmpeg는 블록이 0이라
한 번도 안 돌았다.

**재실행(§3 실측).** 같은 명령을 같은 OUT에 그대로 두 번째: **종료 코드 2**, stderr
한 줄 `ERR 리비전 디렉터리가 이미 있다: 1ae323f3… — 같은 리비전의 재게시는 배타 생성에서
결정적으로 실패한다`. 첫 리비전·`current` 심링크는 그대로다. PROTO-5 테스트 9의 새
단언(종료 2 + 이 문구)과 일치한다.
