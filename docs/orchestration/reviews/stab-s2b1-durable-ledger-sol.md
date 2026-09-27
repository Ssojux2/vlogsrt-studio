# STAB-S2B-1 durable-ledger foundation — Sol review

Updated 2026-09-28. **Accepted offline as a foundation only** at `99e0202a6e7cab38ee16a081ebbf9dc30fa23ed6`. This does not complete S2B or yet enforce budgets on the application's real paid requests.

## Scope and source identity

The committed product scope is exactly these two paths:

- `vlogsrt/project/budget_ledger.py` — SHA-256 `cdad1b66942a0ffdc744940ce974ea3b8deb7993583389ebaefce683f38ab8e4`
- `tests/project/test_budget_ledger.py` — SHA-256 `c803ec4287a0573b17d62fc824d1e607e0e4c72717c9f5ac4c14e264839c78d5`

Both hashes matched after Sol acceptance, commit, and the stop hook. The immutable High Agency estimate was `files<=2 / modules<=1`; the stop hook measured **2 files / 2 modules**, a major module-metric drift. Git confirmed the explicitly owned product and test paths did not change. Sol covered both files and the affected `workflow_protocol` and `workflow_analysis` modules. The explicit two-path ownership was unchanged; do not report that the module metric matched. Root's earlier estimate undercounted how the hook counts the product and test modules.

## RED-to-green evidence and corrections

The first baseline was an expected **collection RED**, not a behavioral failure: the new module did not exist yet. The exact command was:

```sh
/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m pytest tests/project/test_budget_ledger.py -q --basetemp=/tmp/stab-s2b1-sol-red
```

It exited 2 while collecting because `vlogsrt.project.budget_ledger` was absent at test import line 27 (`/tmp/stab-s2b1-sol-red.log`). This established the missing API boundary only.

The first implemented version had two targeted runtime failures and four mypy errors. `test_exact_arithmetic_survives_low_ambient_decimal_context` showed that the 18-digit limit rejected the 25-digit fixed-point serialization of an accepted `1E+24`; the round-trip rejected its own value. The mixed concurrency assertion expected 12 although the implementation reconciled 16; the oracle failed before its separate snapshot assertions, so v1 does not establish actual-usage or remaining-bound values. The final v2 test verifies 16 reconciliations, 0 rejected reservations, actual usage 8 and remaining bound 4. Mypy reported three comparisons between exponent literals `n/N/F` and integers and one `str` passed where a reservation-state `Literal` was required. Logs: `/tmp/stab-s2b1-sol-target-v1.log` and `/tmp/stab-s2b1-sol-mypy-v1.log`.

The accepted version uses a documented value-based numeric domain of 30 significant digits, minimum exponent `-42`, adjusted exponent at most `24`, and trapped arithmetic precision `80`. This accommodates existing transcription-usage amounts with repeating fractional audio seconds (which can require 28 digits); charges are not rounded to fit. The ambient-context arithmetic regression and real synthetic transcription-usage compatibility coverage pass. The concurrency oracle now accounts for measured usage and released unused bound without weakening the assertion. Type narrowing and the literal state annotation resolve the four mypy errors. Automatic parent-directory creation was removed: the caller must supply a durable parent directory.

## Final offline gates

Each final command used the clean base environment `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8`; the pytest commands also set `VLOGSRT_OFFLINE=1` as shown. Commands 3–6 use the same base prefix without the offline variable:

1. `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m pytest tests/project/test_budget_ledger.py -o addopts= -q --basetemp=/tmp/stab-s2b1-sol-target-v2` — **46 passed**, 0.19s (`/tmp/stab-s2b1-sol-target-v2.log`).
2. `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m pytest tests/project/test_workflow_protocol.py tests/project/test_workflow_analysis.py -o addopts= -q --basetemp=/tmp/stab-s2b1-sol-affected` — **22 passed**, 0.06s (`/tmp/stab-s2b1-sol-affected.log`).
3. `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m mypy vlogsrt/project/budget_ledger.py` — exit 0, one file (`/tmp/stab-s2b1-sol-mypy-v2.log`).
4. `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/ruff check vlogsrt/project/budget_ledger.py tests/project/test_budget_ledger.py` — exit 0.
5. `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/ruff format --check vlogsrt/project/budget_ledger.py tests/project/test_budget_ledger.py` — exit 0.
6. `/usr/bin/env -i HOME=/tmp/stab-s2b1-sol-home TMPDIR=/tmp/stab-s2b1-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/python scripts/check_invariants.py` — all 21 invariants passed, exit 0 (`/tmp/stab-s2b1-sol-invariants.log`).

The targeted count is parameterized cases, not the number of test functions. Sol preserved each original exit before collecting log tails. No aggregate `make check`, Swift, or native build ran in this isolated unit; those gates remain for integrated S2B.

## Durability and integration limits

The ledger is an offline persistence primitive. Its cross-instance safety assumes cooperating processes on the supported local POSIX filesystem honor its file-lock protocol; this is not evidence for noncooperating writers, cross-host locking, or power-loss recovery. The caller must create and provide the durable parent directory. Existing pytest conftest guards were active and cooperative, not an OS sandbox or retrospective proof of historical no-access. Atomic writes and lock tests do not prove those broader guarantees.

No real provider, paid request, media, Final Cut Pro, Keychain, or native app path was exercised. The primitive is not yet called at the current production request boundaries, so it does not yet prevent or account for real paid calls. S2B remains incomplete.

## Next unit

Start a fresh bounded S2B-2 source-only task at the generic per-actual-request observer boundary in `transcribe.py`; carry primitive request identity/model/chunk/response data so legacy modules never import project modules. Then refine project ledger adaptation and bounded estimates against this accepted API; cache/retry and Python/Swift cost UI remain later integration work. Give the first functional source boundary to Sol before the extended test matrix, and include the stop hook's actual module metric in the next immutable preflight.
