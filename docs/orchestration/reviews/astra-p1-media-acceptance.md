# Astra P1-03 media review

Date: 2026-09-09 KST. Status: accepted and integrated; all bounded
map-path and readiness corrections below are closed.

Base: `ef12f32b997819ac532de0c42af872ca45c59cf4`.
Initial candidate: `76186aa7ee8d6340ce7fc999172781a0e2df2a74`.
Schema follow-up: `4a0ae2317d94263a42fcc4077131ae5845b291f4`.
Readiness follow-up: `a33515afd3fe70fe96161697d69bf77b000afe4a`.
Owned paths: `vlogsrt/project/media.py`, `tests/project/test_media.py`.

Initial review found stale map schema/exception rules, cwd-dependent relative
paths, and missing regular-file/readability checks. Sol grouped the corrections
through the same GLM Dispatch. The final map format is an array of objects with
resource_id and at least one non-null original/proxy path, strict keys/types,
absolute filesystem paths preserved, relative paths based on the map file,
and no environment/tilde expansion or URI-valued map paths.

Astra independently generated temporary XML and three same-basename originals
whose data exceeded 1 MiB: two identical contents and one differing at the tail.
The probe passed complete SHA/content identity, distinct resource-scoped IDs,
mixed explicit has_audio values preserved across identical bytes, repeated-clip
resolution once per resource, readable proxy preference, missing overridden
proxy fallback, mocked unreadable-proxy fallback, and FIFO original rejection
before attempting any read. No real source media or protected files were used.

Opus independently passed those owned behaviors and additional URI/override
rejections at a33515a (75 focused tests, 1,120 full tests, 19 invariant groups),
reported in `msg_26d3790b626d`. No new blockers were found.

The remaining two already-routed corrections are recorded in
`msg_45f37ffe202c`: resolve the parent of a relative map filename at load time,
so changing cwd later cannot rebind returned paths; reject URI schemes such as
file:/x and data: rather than testing only for the substring ://.
The core review is closed; only this final delta needs subsequent inspection.

## Final acceptance

Final worker SHA: `287b46f2674cc1c18618ac89db57dd3414c8e44f`. Astra inspected
the final delta and independently proved a relative map filename remains bound
after cwd changes, absolute overrides remain unchanged, and four URI scheme
forms reject with map/key context. The first harness comparison confused macOS
/var with its resolved /private/var alias; correcting the expected path passed
without any product change.

Direct worker `make check` passed **1,124 tests**, Ruff lint/format, mypy, and
19 invariant groups. Opus exact-SHA approval is `msg_7588c992877a`; Sol final
approval is `msg_78ada9a27b22`. Previously passed core checks were not reopened.

Astra integrated the exact worker SHA as
`7a107da516ce2665b883a618221a43e9b873bcba`. Direct integrated `make check`
passed **1,235 tests**, Ruff lint/format (58 files), mypy (30 source files), and
all 19 invariant groups. P1-03 offline resolution is complete. Proxy frame
correspondence remains P5-04's work; actual Final Cut relink/playback remains
pending external acceptance.
