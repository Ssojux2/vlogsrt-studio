# Astra P1-02 ingest review

Date: 2026-09-09 KST. Status: accepted and integrated; the final bounded
structural correction passed Sol, Opus, and Astra review.

- Base: `07e4dac41888ae115157ed4a68d6dfd5fbd5b233`.
- Rejected initial implementation: `eaee32bd338c23f13e13afd1806f47c247232856`.
- Corrected candidate: `05b6e3ed167d368974218afa22c4f8289b12c30a`.
- Owned files: `vlogsrt/project/models.py`, `vlogsrt/project/fcpxml.py`,
  `tests/project/test_fcpxml.py`.

The initial implementation used internal media kind names as XML spellings,
ignored actual split-edit and format/spine coordinate overrides, rejected valid
primary lane zero/static metadata/defaults, and guessed audio presence from an
omitted attribute. Root reproduced the schema error and six false-acceptance
cases, then sent one grouped correction through Sol to the same GLM Dispatch.

On the corrected immutable SHA, Astra generated **32 independent XML documents**:
eight supported frame durations, two timeline epochs, and both explicit audio
declarations. They contain repeated source instances, a gap, a disabled video-only
instance, nonzero asset epochs, optional asset name, default original media kind,
proxy media, lane zero, matching format overrides, and static metadata.

All 32 passed the actual installed Apple 1.14 DTD with the exact argv
`xmllint --nonet --noout --dtdvalid <encoded-local-DTD-file-URI> <temporary-XML>`.
All also passed independent model checks for raw bytes/hash, exact clocks,
resource identity, distinct repeated-instance identity, media kind translation,
and disabled/source-enable preservation. The DTD path/hash are recorded in
`astra-local-fcpxml-schema.md`.

For each document, six separately mutated inputs added `audioStart`,
`audioDuration`, an unresolved clip format, nonzero primary-spine offset, nonzero
primary-spine lane, or omitted audio presence. All **192 mutations** raised
`UnsupportedProject` with the expected attribute/reason and element location.

All inputs were synthetic temporary files. No source media, Final Cut library,
protected fixture, product code, or user environment file was modified by these
probes. This evidence does not claim actual Final Cut import/re-export/playback.

Root's direct `make check` on the candidate exited zero: **985 tests**, Ruff lint
and formatting, mypy across 28 source modules, and all 19 invariant groups.
Sol independently passed its correction probes and installed DTD check. Opus
confirmed the previous findings closed, then identified one residual case: an
asset containing no `media-rep` was accepted as an empty tuple. Astra requested a
bounded same-Dispatch correction requiring at least one representation, covering
empty and metadata-only assets while preserving proxy-only ingest. General DTD
validation remains the separate P1-04 boundary.

## Final acceptance

Final worker SHA: `b96684e041a02825fd0aa0ed43c46fa1363c0308`.
The small correction rejects empty and metadata-only assets while preserving
proxy-only ingest. Astra inspected the complete delta and passed all 139 parser
tests. Opus independently checked the positive/negative cases and approved this
exact SHA (`msg_db927ccc5acb`); the prior sweep was not repeated.

Astra merged it as `ef12f32b997819ac532de0c42af872ca45c59cf4`. Direct integrated
`make check` passed: **1,045 tests**, Ruff lint/format (54 files), mypy (28 source
files), and all 19 invariant groups. Real Final Cut application acceptance remains
pending; this closes only P1-02 offline ingest.
