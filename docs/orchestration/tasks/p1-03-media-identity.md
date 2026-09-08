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
    content_id: str
    original: Path
    image_source: Path
    original_sha256: str
    used_proxy: bool

def load_media_map(path: Path) -> tuple[MediaOverride, ...]: ...
def sha256_file(path: Path) -> str: ...
def resolve_media(document: ProjectDocument, overrides: Sequence[MediaOverride] = ()) -> tuple[ResolvedMedia, ...]: ...
```

- Map overrides by exact XML `resource_id`; reject duplicate/unknown keys. A raw ID such as `r2` is only a lookup key within the specific current input document identified by its hash; it is never a durable identity, cross-document rebind key, or exported media identity. Never match by filename/stem.
- Overrides take precedence over XML media-rep URIs. URI decoding must be explicit and reject non-file/unresolvable schemes.
- Original is always required and hashed in full. Missing proxy falls back to original for images; missing original fails readiness even when proxy exists.
- Repeated clip instances resolve to one `ResolvedMedia`; same filename with different content remains distinct.
- `content_id = sha256("source-content-v1\0" + original_sha256).hexdigest()`; later audio policy augments analysis keys without changing content identity.

## Implementation steps

1. Write failing tests for override precedence, percent-encoded file URIs, same-name/different-content, repeated instances, missing proxy fallback, missing original failure, unknown/duplicate map entries, and reuse of the same raw `r2` ID in two different documents without cross-document binding.
2. Implement deterministic full hashing and resolution without opening any FCP database.
3. Run focused tests/invariants/`make check`; commit owned files only.

## Acceptance

All focused cases pass offline; identical bytes at different paths share `content_id`, different bytes with the same basename do not, and unresolved originals block. Full check passes with no protected paths changed.

## Failure route

Report resource XML/scoped ID, sanitized path form, expected/actual resolution, base SHA and tests. Do not add basename fallback. Ambiguous mapping policy escalates Opus/Astra; fixes remain GLM.

## Report

Report Orca and launch IDs, branch/base/commit, files, failing-first/focused/full tests, fallback counts, and `ready-to-integrate`.
