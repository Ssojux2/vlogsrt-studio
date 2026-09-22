# Task P1-02: parse the supported FCPXML 1.14 subset

## Authority

Binding spec `Input and output` plus `Source analysis, clocks, identity`; depends on P1-01. Because P1-03/P1-05 consume the frozen model names, their cross-task compatibility required manager review before dispatch. Opus was service-limited on 2026-09-09, so Astra read P1-01/P1-02/P1-03/P1-04/P1-05 together and explicitly approved the exact names/signatures below under the user-authorized final-review fallback; record that disposition and do not wait for a duplicate pre-dispatch review.

## Baseline and ownership

- Fresh `fcp-p1-ingest` child worktree from Astra's exact approved P1-01 integration SHA `07e4dac41888ae115157ed4a68d6dfd5fbd5b233`; abort on mismatch.
- Modify the P0-created FCPXML-only `vlogsrt/project/models.py`; create `vlogsrt/project/fcpxml.py`, `tests/project/test_fcpxml.py`.
- No media resolution, subprocess/DTD validation, writing, source transcription, or CLI work.

## Contract

```python
@dataclass(frozen=True, slots=True)
class MediaRep:
    kind: Literal["original", "proxy"]
    src: str

@dataclass(frozen=True, slots=True)
class AssetResource:
    xml_id: str
    scoped_id: str
    uid: str
    name: str
    format_id: str
    frame_duration: Fraction
    start: Fraction
    duration: Fraction
    has_audio: bool
    media_reps: tuple[MediaRep, ...]

@dataclass(frozen=True, slots=True)
class ClipInstance:
    instance_id: str
    element_path: str
    resource_scoped_id: str
    offset: Fraction
    start: Fraction
    duration: Fraction
    enabled: bool
    src_enable: Literal["all", "video"]

@dataclass(frozen=True, slots=True)
class GapInstance:
    instance_id: str
    element_path: str
    offset: Fraction
    duration: Fraction

TimelineItem = ClipInstance | GapInstance

@dataclass(frozen=True, slots=True)
class ProjectDocument:
    input_path: Path
    info_path: Path
    input_sha256: str
    raw_xml: bytes
    version: str
    sequence_tc_start: Fraction
    frame_duration: Fraction
    resources: tuple[AssetResource, ...]
    storyline: tuple[TimelineItem, ...]

class UnsupportedProject(RuntimeError):
    location: str
    reason: str

def resolve_info_path(path: Path) -> Path: ...
def parse_project(path: Path) -> ProjectDocument: ...
```

- Accept `.fcpxml` and `.fcpxmld/Info.fcpxml`, version exactly 1.14, one project/sequence/spine with only direct `asset-clip` and `gap` children.
- Resolve each asset/clip format ref and require normal-speed source/project CFR equality at one of the P1-01 supported rates; reject a missing format, unsupported rate, or 30-versus-29.97 mismatch rather than resampling. Sequence duration, clip offset/start/duration, and source bounds must be frame-aligned on that exact grid; direct storyline items must be in document order, non-overlapping, and within sequence duration.
- Preserve `raw_xml`, resources, UID/static attributes, and original time strings by never normalizing the raw tree during ingest.
- Reject unresolved refs, connected clips/audio, compound/sync/multicam, retime, transitions, audio routing overrides, and time-varying effects with deterministic element path plus reason. A discovered version other than `1.14` is named in the rejection.
- Accept `enabled="0"` and `srcEnable` values `video`/`all`; derive audibility later, without dropping clips. `srcEnable="audio"` is valid FCPXML syntax but outside this supported subset and must be rejected with location/reason. `srcEnable="none"` is invalid for an `asset-clip` and is rejected as invalid, not documented as supported.
- Require an explicit asset `hasAudio="1"` or `hasAudio="0"`. Omission is valid FCPXML whose audio presence should be derived from media, but this narrow v1 does not add presence probing: reject omission as supported-subset/readiness `UnsupportedProject`, never as malformed and never by defaulting true/false. Explicit `0` alone authorizes the later `declared_no_audio` disposition. `hasVideo` adds no model field; if present accept only `0`/`1`, while omission remains uninterpreted.
- Translate real XML `media-rep kind="original-media"`/`"proxy-media"` into internal `MediaRep.kind == "original"`/`"proxy"`; omitted XML `kind` defaults to `original-media`. Do not teach fixtures the internal spellings as XML.
- Asset `name` is optional and maps to the deterministic empty string. Asset-clip `offset` and `start` each default exactly to `0s`. Direct asset-clips accept absent `lane` or `lane="0"`; nonzero lane is connected/unsupported. A gap `lane` attribute is not valid in the schema and is rejected. Absent or exact-zero primary-spine `offset`/`lane` is accepted; nonzero is an unsupported coordinate/anchoring override.
- Reject actual split-edit attributes `audioStart` and `audioDuration` with location/reason. Optional `format` refs on the primary spine or an asset-clip, when present, must resolve and equal the already-required supported sequence/source frame grid; never ignore an unresolved or mismatched override.
- Preserve schema-valid static metadata in `raw_xml` and permit the actual DTD's metadata/marker content where valid under asset, asset-clip, and gap. This does not broaden support to effects, connected content, or dynamic timing constructs.
- `element_path` is a stable 1-based positional path such as `/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]/spine[1]/asset-clip[3]`; repeated identical sibling elements therefore receive distinct paths and IDs.
- `vlogsrt/project/models.py` is explicitly FCPXML-only. Every dataclass in it is frozen; non-FCPXML phase models must not accumulate there.
- `scoped_id = sha256(input_sha256 + ":resource:" + xml_id).hexdigest()` and `instance_id = sha256(input_sha256 + ":instance:" + element_path).hexdigest()`.

## Implementation steps

1. Write inline/synthetic failing XML tests for the flat supported shape, bundles, gaps, repeated/disabled instances, positional uniqueness of identical siblings, nonzero epochs, unresolved refs, frame-misaligned ranges, out-of-order/overlap/source-bound violations, supported-rate mismatch, every rejected construct including `audio` and `none` `srcEnable` values, true XML media-rep spellings/default, every timing/format/lane override rule, optional name/timing defaults, explicit/omitted audio declaration, and schema-valid static metadata.
2. Add frozen models and a stdlib XML parser with deterministic locations; do not use subprocess or `xmllint` here.
3. Assert parsed models cannot be mutated and repeated instances share a resource ID but not an instance ID.
4. Run focused/full checks and commit only owned paths.

## Acceptance

`pytest tests/project/test_fcpxml.py -q`, invariant checker, and a direct/pipefail-protected `make check` pass. Rejection tests assert location and reason. The input file is never rewritten, all supported rate fixtures parse through P1-01 functions, and the claimed supported synthetic fixture validates with `/usr/bin/xmllint --nonet --noout --dtdvalid` against the installed 1.14 DTD (no validator is implemented in this parser task).

## Failure route

Report minimal XML, location/reason mismatch, exact base/commit and commands. Do not silently flatten or broaden the supported subset. Escalate ambiguous FCP semantics to Opus/Astra before a GLM fix.

## Report

After committing, report Orca IDs/launch receipt, branch/base/immutable commit, files, failing-first and full evidence, unsupported cases, and `ready-to-integrate` as a review-ready checkpoint. Ask Sol and wait; after Sol passes, continue waiting in the same Dispatch for Astra's exact-SHA acceptance or bounded findings before `worker_done`.
