# Task P1-04: enforce the xmllint-only XML validation boundary

## Authority

Binding spec `Input and output` and `Source contracts and architecture`; depends on P1-02 and P0 subprocess contracts. May run parallel with P1-03.

## Baseline and ownership

- Fresh `fcp-p1-xml-validation` branch/worktree from exact P1-02-integrated SHA.
- Create `vlogsrt/project/xml_validation.py`, `tests/project/test_xml_validation.py`.
- Do not modify parser/media/time modules or checker exceptions; if P0 checker rejects the exact boundary, report rather than widening it here.

## Contract

```python
@dataclass(frozen=True, slots=True)
class XmlValidation:
    valid: bool
    diagnostics: tuple[str, ...]
    dtd_checked: bool

class XmlValidationUnavailable(RuntimeError): ...

def validate_xml(path: Path, *, dtd_path: Path | None = None) -> XmlValidation: ...
```

- Resolve executable only with `shutil.which("xmllint")`; normalize the XML input with `path.resolve()` before constructing argv so a relative name beginning with `-` cannot be consumed as an xmllint option. In both frozen forms below, `str(path)` means this normalized absolute input path.
- The only subprocess invocation is `subprocess.run` with an exact tuple/list argv, `shell=False`, and no executable indirection. Well-formedness argv is exactly `[resolved_xmllint, "--nonet", "--noout", str(path)]`.
- With `dtd_path`, resolve the DTD and convert it with `Path.resolve().as_uri()` so spaces remain one argv element; argv is exactly `[resolved_xmllint, "--nonet", "--noout", "--dtdvalid", dtd_uri, str(path)]` and `dtd_checked=True`. Without it, `dtd_checked=False`.
- Missing tool or requested missing DTD raises `XmlValidationUnavailable`; nonzero xmllint returns `valid=False` with bounded sanitized stderr, not an exception that loses diagnostics.
- Never use shell mode, arbitrary flags, environment/key reads, network schemas, or any executable other than xmllint.
- This development host has `/usr/bin/xmllint` and the installed Final Cut Pro 1.14 DTD at `/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources/FCPXMLv1_14.dtd`, SHA-256 `33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5`. Use that exact read-only file for the real validation check; do not copy or modify it, search broadly for alternatives, or treat its availability as portable runtime configuration.

## Implementation steps

1. Write failing subprocess-mocked tests that compare the complete argv, DTD/no-DTD, `--nonet`, a DTD path containing spaces, missing tool, missing DTD, malformed XML, `shell=False`, and malicious-looking paths treated as one plain argv element. Add isolated temp-tree regressions for nonexistent relative `--version`, a real XML filename beginning with `-`, and an ordinary relative XML input; each argv operand must be its resolved absolute path. Also validate a P1-02 supported synthetic document against the exact installed DTD and prove a schema-invalid spelling fails.
2. Implement the one narrow boundary and immutable result; run P0 invariant tests to prove the exception is still scoped.
3. Run focused/invariant/full checks and commit owned paths only.

## Acceptance

`pytest tests/project/test_xml_validation.py tests/test_invariants.py -q`, invariant checker, and `make check` pass. Tests assert `shell` is never true and the command is exactly xmllint. The exact installed DTD hash is reverified and a real supported-document DTD check exits zero on this host; if the tool/DTD later becomes unavailable, record unavailable/BLOCKED rather than PASS.

## Failure route

Report exact mocked argv/return code/diagnostic and base SHA. Do not call alternate validators or relax the invariant. Tool/DTD absence is an honest external gate; independent offline tasks may continue.

## Report

Report Orca/launch IDs, branch/base/commit, files, focused/full checks, real xmllint path/version, installed DTD path/hash, actual valid/invalid return codes, and merge-ready state.
