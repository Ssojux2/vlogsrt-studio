# P1 XML validation acceptance

Accepted GLM candidate `182b786fb5ee7e6219dbdcdea28566ae1e1b8eae`, integrated as `1fe07e563c89958d2c771594d01645222d9df4aa` on 2026-09-08 UTC.

The validator uses only the two approved xmllint argv forms, `shell=False`, and executable discovery through `shutil.which`. Input operands are resolved absolute before invocation; requested DTD paths use `resolve().as_uri()`. Nonzero tool results report invalid XML with bounded diagnostics, while missing tools/DTDs and spawn failures raise `XmlValidationUnavailable`.

Root reproduced a false success on initial `897164b`: nonexistent `Path('--version')` was interpreted as an option. The correction passed independent actual-tool probes in a temporary cwd: missing option-like input rejects, and `--version`, `-`, Korean/spaces, and ordinary relative filenames are judged by their valid or malformed XML content. Root passed the product gate at `e7d4005` (1,257 tests) and all 24 final XML tests after reviewing the final test-only delta. Installed Final Cut DTD validation and environment-failure behavior were verified by Sol and Opus; final approvals are `msg_1bd8b51061c6` and `msg_817975b45994`.

Integrated `make check` passed **1,274 tests**, Ruff (60 files), mypy (31 modules), and all **19 invariant groups**. P1 identity roundtrip remains the next task; actual Final Cut import/re-export is still `PENDING_HUMAN`.
