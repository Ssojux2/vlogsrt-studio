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

- Identity output writes `ProjectDocument.raw_xml` byte-for-byte using atomic same-directory replacement; no parse/serialize normalization.
- Reparse output and assert input hash-independent semantic equality for version, exact original time strings, resources/UIDs/static attributes, flat item order/gaps/disabled durations and refs.
- Parameterize all eight supported frame rates, including 30 vs 29.97, repeated assets, nonzero `asset.start`/`clip.start`/`sequence.tcStart`, `enabled=0`, `srcEnable=video`, and gaps.
- DTD validation is invoked when a real path is supplied; unavailable DTD/tool is separately reported, never synthesized as pass.
- This task proves only identity-copy correctness. It does not validate any edited FCPXML serialization or authorize reuse of raw resource IDs as external identity; P6 owns generated-writer correctness and strong-identity rebinding.

## Implementation steps

1. Add failing byte-identity and semantic parameterized tests, plus atomic-write failure preserving the previous destination.
2. Implement the two functions using existing P1 interfaces; do not introduce a general XML writer yet.
3. Run focused tests, all P1 tests, invariants, and `make check`; commit only owned paths.

## Acceptance

Every fixture roundtrips byte-identically and reparses semantically. Duration/order/ref checks pass for valid inputs and reject zero-frame identity mismatch. `make check` passes; actual Final Cut import remains `PENDING_HUMAN`.

## Failure route

Report fixture rate/epoch, byte offset or semantic field mismatch, validator state, base/commit/tests. No canonicalization is accepted as an identity fix. Shared contract conflicts escalate to Opus/Astra.

## Report

Report Orca/launch IDs, branch/base/commit, files, exact test totals, DTD availability, pending human gate, and `ready-to-integrate`.
