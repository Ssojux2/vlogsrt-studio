# Task P1-05: prove identity-preserving FCPXML roundtrip

## Authority

Binding spec P1 acceptance; depends on integrated P1-03 and P1-04. This joins the P1 interfaces and is reviewed by Opus before Astra's P1 merge.

## Baseline and ownership

- Fresh `fcp-p1-roundtrip` worktree from exact SHA containing P1-03/P1-04.
- Modify `vlogsrt/project/fcpxml.py`; create `tests/project/test_roundtrip.py`. Synthetic XML is inline or written under pytest `tmp_path`; no fixture/golden file is added or modified.
- Do not change media/time/models/validator contracts or existing fixtures.

## Contract

```python
def write_identity(document: ProjectDocument, destination: Path) -> Path: ...
def validate_roundtrip(source: ProjectDocument, written: Path, *, dtd_path: Path | None = None) -> XmlValidation: ...
```

- Identity output writes `ProjectDocument.raw_xml` byte-for-byte using atomic same-directory replacement; no parse/serialize normalization. The call must not mutate the source document or its `raw_xml`, and a write/replace failure must leave any preexisting destination byte-for-byte unchanged.
- `validate_roundtrip` first uses the existing P1-04 validation boundary, then the existing P1-02 parser/read interfaces. A tool or requested-DTD absence remains `XmlValidationUnavailable`; an invalid written document returns the validator's `XmlValidation(valid=False, ...)` result rather than being promoted to success or a new public exception.
- Reparse output and compare input hash-independent semantic equality for version, exact original time values, resources/UIDs/static attributes, flat item order/gaps/disabled durations and refs. Byte inequality, parse/semantic mismatch, or an otherwise invalid written document returns `XmlValidation(valid=False, bounded actionable diagnostics, accurate dtd_checked)`; runtime correctness must not depend on bare `assert` statements.
- Mismatch diagnostics are deterministic and bounded, name the failed byte/parse/semantic check, and do not dump unbounded XML. Reuse `XmlValidation` and `XmlValidationUnavailable`; do not add a general validation/result hierarchy.
- Parameterize all eight supported frame rates, including 30 vs 29.97, repeated assets, nonzero `asset.start`/`clip.start`/`sequence.tcStart`, `enabled=0`, `srcEnable=video`, and gaps.
- DTD validation is invoked when a real path is supplied; unavailable DTD/tool is separately reported, never synthesized as pass.
- On this development host, run the roundtrip through `/usr/bin/xmllint` with the exact installed DTD `/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources/FCPXMLv1_14.dtd` after reverifying SHA-256 `33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5`. All claimed supported synthetic inputs must use actual DTD spellings/defaults and validate before and after the byte-identical roundtrip.
- This task proves only identity-copy correctness. It does not validate any edited FCPXML serialization or authorize reuse of raw resource IDs as external identity; P6 owns generated-writer correctness and strong-identity rebinding.

## Implementation steps

1. Add failing byte-identity and semantic parameterized tests, mismatch-result tests (bytes, parse/structure, and validator-invalid), source-content non-mutation, plus atomic-write/replace failure preserving the previous destination.
2. Implement the two functions using existing P1 interfaces; do not introduce a general XML writer yet.
3. Run focused tests, all P1 tests, invariants, and `make check`; commit only owned paths.

## Acceptance

Every synthetic document roundtrips byte-identically, leaves its input content untouched, validates against the exact installed DTD before/after, and reparses semantically. Duration/order/ref checks pass for valid inputs; byte/semantic/invalid-written mismatches return bounded `valid=False` evidence with accurate `dtd_checked`, and atomic failure preserves an existing destination. `make check` passes; actual Final Cut import remains `PENDING_HUMAN`.

## Failure route

Report fixture rate/epoch, byte offset or semantic field mismatch, validator state, base/commit/tests. No canonicalization is accepted as an identity fix. Shared contract conflicts escalate to Opus/Astra.

## Report

Report Orca/launch IDs, branch/base/commit, files, exact test totals, DTD availability, pending human gate, and `ready-to-integrate`.
