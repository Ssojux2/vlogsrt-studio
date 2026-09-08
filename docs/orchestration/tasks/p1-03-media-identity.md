# Task P1-03: resolve original/proxy media with strong identity

## Authority

Binding spec `Input and output` and `Source analysis, clocks, identity`; depends on P1-02. May run parallel with P1-04 because files are disjoint.

## Baseline and ownership

- Fresh branch/worktree `fcp-p1-media` from the exact P1-02-integrated SHA.
- Create `vlogsrt/project/media.py`, `tests/project/test_media.py`.
- Do not modify P1 models/parser, XML validation, fixtures/API data, FCP databases, or protected paths.

## Contract

```python
@dataclass(frozen=True, slots=True)
class MediaOverride:
    resource_id: str
    original: Path | None = None
    proxy: Path | None = None

@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    resource_scoped_id: str
    asset_content_id: str
    original: Path
    image_source: Path
    original_sha256: str
    has_audio: bool
    used_proxy: bool

def load_media_map(path: Path) -> tuple[MediaOverride, ...]: ...
def sha256_file(path: Path) -> str: ...
def resolve_media(document: ProjectDocument, overrides: Sequence[MediaOverride] = ()) -> tuple[ResolvedMedia, ...]: ...
```

- `load_media_map` accepts one strict JSON shape: a top-level array of objects with required nonempty string `resource_id` and at least one of optional `original`/`proxy`. No other top-level type, unknown object key, duplicate `resource_id`, empty string, JSON `null`, or non-string field is accepted; raise `ValueError` naming the map path and entry/key location. `original` and `proxy`, when present, are nonempty filesystem path strings: relative paths resolve from `path.parent`, never cwd, while absolute paths remain unchanged. URI strings are rejected, and neither environment-variable nor tilde expansion occurs. This loader validates structure and path addressing only—document membership and media usability remain `resolve_media` checks.

```json
[
  {"resource_id": "r2", "original": "media/camera A.mov", "proxy": "proxy/camera-a.mp4"},
  {"resource_id": "r7", "original": "../external/camera-b.mov"}
]
```

- Map overrides by exact XML `resource_id`; reject duplicate/unknown keys. A raw ID such as `r2` is only a lookup key within the specific current input document identified by its hash; it is never a durable identity, cross-document rebind key, or exported media identity. Never match by filename/stem.
- Overrides take precedence over XML media-rep URIs. URI decoding must be explicit and reject non-file/unresolvable schemes.
- Original is always required and hashed in full. A missing or unusable proxy falls back to original for images and sets `used_proxy=False`; a missing or unusable original fails readiness even when a proxy exists. Proxy/original frame-correspondence validation belongs to P5-04, not this task.
- `has_audio` is copied from the exact matched `AssetResource`; never infer it from file extension, bytes, stream probing, or another resource with the same content hash. P1-02 only constructs that resource after explicit XML `hasAudio="1"`/`"0"`; omission is rejected as valid-but-outside-v1 readiness, so no tri-state is reachable here and explicit `0` alone authorizes declared-no-audio.
- Repeated clip instances resolve to one `ResolvedMedia`; same filename with different content remains distinct.
- `asset_content_id = sha256("source-content-v1\0" + original_sha256).hexdigest()`; later audio policy augments analysis keys without changing content identity.

## Implementation steps

1. Write failing tests for the strict map schema and example, relative-path resolution from the map parent rather than cwd, unchanged absolute paths, rejection of URI/empty/null/non-string/unknown fields and entries with no media path, no environment/tilde expansion, override precedence, percent-encoded XML file URIs, same-name/different-content, repeated instances, missing/unusable proxy fallback, missing/unusable original failure, unknown/duplicate map entries, reuse of the same raw `r2` ID in two different documents without cross-document binding, and equal content bytes referenced by resources with different `has_audio` declarations.
2. Implement deterministic full hashing and resolution without opening any FCP database.
3. Run focused tests/invariants/`make check`; commit owned files only.

## Acceptance

All focused cases pass offline; identical bytes at different paths share `asset_content_id` without coalescing resource-level `has_audio`, different bytes with the same basename do not, and unresolved originals block. Full check passes with no protected paths changed.

## Failure route

Report resource XML/scoped ID, sanitized path form, expected/actual resolution, base SHA and tests. Do not add basename fallback. Ambiguous mapping policy escalates Opus/Astra; fixes remain GLM.

## Report

Report Orca and launch IDs, branch/base/commit, files, failing-first/focused/full tests, fallback counts, and `ready-to-integrate`.
