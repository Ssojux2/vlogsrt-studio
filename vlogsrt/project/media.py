"""소스 미디어 해석 — 원본/프록시 경로, 내용 신원, 준비 게이트(P1-03).

계획("Input and output" ＋ "Source analysis, clocks, identity")의 정본:

- **명시적 미디어 맵이 XML media-rep URI를 덮어쓴다.** 파일 이름·stem으로 짝을
  맞추는 일은 없다. 맵 키는 정확한 XML `resource_id`뿐이고, `r2` 같은 낡은 id는
  **지금 이 문서 안에서만** 뜻이 있는 룩업 키다 — 문서 해시가 스코프인
  `scoped_id`(P1-02)와 달리, 그 자체로 신원이 되거나 문서 사이의 재묶음 키가
  되지 못한다. 재내보내기의 재묶음은 검증된 리소스/내용 신원으로 한다.
- **원본은 언제나 필요하고 통째로 해시된다.** 프록시만 있는 애셋은 준비가
  막히고(프록시가 멀쩡해도), 반대로 프록시가 없으면 이미지도 원본에서 뽑는다.
- **`has_audio`는 정확히 그 리소스에서만 온다.** 확장자·바이트·프로빙, 같은
  내용 해시를 가진 다른 리소스에서 추론하지 않는다(P1-02가 명시적 0/1만
  통과시키므로 삼태가 닿지 않는다 — sol-no-audio-disposition).
- **내용 신원은 문서 바깥이다.** `asset_content_id`는 원본 바이트에서만 나온다:
  같은 바이트면 경로가 달라도 하나고, 다른 바이트면 이름이 같아도 둘이다.
  이후 오디오 정책은 분석 키를 덧붙일 뿐 내용 신원을 바꾸지 않는다.

FCP 데이터베이스는 열지 않는다 — 필요한 것은 XML과 파일 바이트뿐이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from vlogsrt.project.models import AssetResource, ProjectDocument, UnsupportedProject

_CONTENT_ID_DOMAIN = "source-content-v1"
"""내용 신원의 도메인 구분자 — 오디오 정책이 분석 키를 덧붙일 때와 갈라진다."""

_READ_CHUNK_BYTES = 1024 * 1024
"""한 번에 읽는 해시 청크 크기 — 동작 임계값이 아니라 읽기 버퍼다."""

_MAP_ENTRY_KEYS = frozenset({"resource_id", "original", "proxy"})
"""미디어 맵 항목의 전체 키 집합 — 여기 없는 키는 오타이므로 거부한다."""


@dataclass(frozen=True, slots=True)
class MediaOverride:
    """한 리소스에 대한 명시적 경로 지정 — 필드별로 XML media-rep을 덮어쓴다."""

    resource_id: str
    original: Path | None = None
    proxy: Path | None = None


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    """해석이 끝난 소스 미디어 — 이 문서의 이 리소스가 가리키는 실제 파일."""

    resource_scoped_id: str
    asset_content_id: str
    original: Path
    image_source: Path
    original_sha256: str
    has_audio: bool
    used_proxy: bool


class MediaUnresolved(UnsupportedProject):
    """미디어를 해석할 수 없다 — 어디서(location) 왜(reason) 막혔는지 말한다.

    지정 부분집합 밖 입력(`UnsupportedProject`) 계열이다: 문서 형태가 아니라
    가리키는 파일이 준비되지 않았을 때도 같은 자리+이유 규율로 거부한다.
    """


def load_media_map(path: Path) -> tuple[MediaOverride, ...]:
    """미디어 맵 파일(JSON 객체 배열)을 읽는다. 읽는 것 외에 아무것도 안 한다.

    배열이어야 하는 이유는 중복에 있다: 객체를 키로 묶으면 JSON은 뒤의 키가
    앞을 조용히 덮어쓴다. 배열은 중복이 **표현되고**, 그래서 거부할 수 있다.
    """
    where = str(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MediaUnresolved(where, f"미디어 맵 파일이 없다: {where}") from error
    except OSError as error:
        raise MediaUnresolved(where, f"미디어 맵 파일을 읽지 못한다: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MediaUnresolved(where, f"미디어 맵이 JSON이 아니다: {error}") from error
    if not isinstance(payload, list):
        raise MediaUnresolved(where, "미디어 맵은 객체의 배열이어야 한다")
    overrides: list[MediaOverride] = []
    seen: set[str] = set()
    for position, entry in enumerate(payload, start=1):
        overrides.append(_map_entry(entry, position, where, seen))
    return tuple(overrides)


def _map_entry(entry: object, position: int, where: str, seen: set[str]) -> MediaOverride:
    if not isinstance(entry, dict):
        raise MediaUnresolved(where, f"미디어 맵 항목 {position}이 객체가 아니다: {entry!r}")
    unknown = sorted(set(entry) - _MAP_ENTRY_KEYS)
    if unknown:
        raise MediaUnresolved(
            where,
            f"미디어 맵 항목 {position}에 모르는 키가 있다: {unknown}"
            " — 받는 키는 resource_id/original/proxy뿐이다",
        )
    resource_id: object = entry.get("resource_id")
    if not isinstance(resource_id, str) or not resource_id:
        raise MediaUnresolved(
            where,
            f"미디어 맵 항목 {position}의 resource_id가 무효다: {resource_id!r}"
            " — 비지 않은 문자열이어야 한다",
        )
    if resource_id in seen:
        raise MediaUnresolved(where, f"미디어 맵에 중복 resource_id가 있다: {resource_id!r}")
    seen.add(resource_id)
    return MediaOverride(
        resource_id=resource_id,
        original=_map_path(entry.get("original"), "original", position, where),
        proxy=_map_path(entry.get("proxy"), "proxy", position, where),
    )


def _map_path(value: object, field: str, position: int, where: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MediaUnresolved(
            where,
            f"미디어 맵 항목 {position}의 {field}가 무효다: {value!r}"
            " — 경로 문자열이거나 null이어야 한다",
        )
    return Path(value)


def sha256_file(path: Path) -> str:
    """파일을 통째로 sha256한다 — 청크는 읽기 크기일 뿐, 해시 대상은 전체다."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_media(
    document: ProjectDocument, overrides: Sequence[MediaOverride] = ()
) -> tuple[ResolvedMedia, ...]:
    """문서의 모든 애셋 리소스를 미디어로 해석한다 — 문서 순서 그대로 돌려준다.

    해석 단위는 리소스다: 같은 애셋을 클립으로 몇 번 쓰든 미디어는 하나고,
    전사·해시도 한 번만 일어난다. `has_audio`는 리소스의 선언을 그대로 옮기고,
    프록시 유무는 이미지 원본(`image_source`)의 선택에만 쓰인다.
    """
    override_map = _index_overrides(document, overrides)
    return tuple(
        _resolve_resource(resource, position, override_map.get(resource.xml_id))
        for position, resource in enumerate(document.resources, start=1)
    )


def _index_overrides(
    document: ProjectDocument, overrides: Sequence[MediaOverride]
) -> dict[str, MediaOverride]:
    """오버라이드를 resource_id로 묶는다 — 중복·모르는 키는 그 자리에서 거부한다."""
    indexed: dict[str, MediaOverride] = {}
    for override in overrides:
        if override.resource_id in indexed:
            raise MediaUnresolved(
                str(document.input_path),
                f"오버라이드에 중복 resource_id가 있다: {override.resource_id!r}"
                " — 어느 쪽이 진짜인지 고르지 않는다",
            )
        indexed[override.resource_id] = override
    known_ids = {resource.xml_id for resource in document.resources}
    unknown = sorted(set(indexed) - known_ids)
    if unknown:
        raise MediaUnresolved(
            str(document.input_path),
            f"문서에 없는 resource_id다: {unknown[0]!r}"
            f" — 이 문서의 애셋 id는 {sorted(known_ids)}뿐이다",
        )
    return indexed


def _resolve_resource(
    resource: AssetResource, position: int, override: MediaOverride | None
) -> ResolvedMedia:
    where = f"/fcpxml[1]/resources[1]/asset[{position}]"
    original = _select_original(resource, override, where)
    proxy = _select_proxy(resource, override, where)
    try:
        original_sha256 = sha256_file(original)
    except OSError as error:
        raise MediaUnresolved(where, f"원본 미디어를 읽을 수 없다: {original} ({error})") from error
    image_source = original
    used_proxy = False
    if proxy is not None and proxy.is_file():
        # 프록시는 실제로 쓸 수 있을 때만 이미지 원본이 된다 — 없으면 원본에서 뽑는다.
        image_source = proxy
        used_proxy = True
    return ResolvedMedia(
        resource_scoped_id=resource.scoped_id,
        asset_content_id=_asset_content_id(original_sha256),
        original=original,
        image_source=image_source,
        original_sha256=original_sha256,
        has_audio=resource.has_audio,
        used_proxy=used_proxy,
    )


def _select_original(resource: AssetResource, override: MediaOverride | None, where: str) -> Path:
    """원본 경로 — 오버라이드가 우선하고, 없으면 XML의 유일한 original-media다."""
    if override is not None and override.original is not None:
        return override.original
    candidates = [rep for rep in resource.media_reps if rep.kind == "original"]
    if not candidates:
        raise MediaUnresolved(
            where,
            "원본(original-media) media-rep이 없다 — 원본은 프록시가 있어도"
            " 언제나 필요하다(프록시는 이미지에만 쓰인다)",
        )
    if len(candidates) > 1:
        raise MediaUnresolved(
            where,
            f"original-media media-rep이 {len(candidates)}개다"
            " — 어느 쪽이 원본인지 못 정하므로 첫째를 고르지 않는다",
        )
    return _file_uri_to_path(candidates[0].src, where)


def _select_proxy(
    resource: AssetResource, override: MediaOverride | None, where: str
) -> Path | None:
    """프록시 경로 — 오버라이드가 우선하고, XML에 없으면 없는 것이다(None)."""
    if override is not None and override.proxy is not None:
        return override.proxy
    candidates = [rep for rep in resource.media_reps if rep.kind == "proxy"]
    if len(candidates) > 1:
        raise MediaUnresolved(
            where,
            f"proxy-media media-rep이 {len(candidates)}개다"
            " — 어느 쪽이 프록시인지 못 정하므로 첫째를 고르지 않는다",
        )
    if not candidates:
        return None
    return _file_uri_to_path(candidates[0].src, where)


def _file_uri_to_path(src: str, where: str) -> Path:
    """file URI를 로컬 경로로 명시적으로 푼다 — 못 푸는 형태는 전부 거부한다."""
    parsed = urlparse(src)
    scheme = parsed.scheme.lower()
    if not scheme:
        raise MediaUnresolved(where, f"media-rep src가 file URI가 아니다(스킴이 없다): {src}")
    if scheme != "file":
        raise MediaUnresolved(where, f"media-rep src가 file 스킴이 아니다: {scheme!r} — {src}")
    if parsed.hostname not in (None, "localhost"):
        raise MediaUnresolved(
            where, f"로컬 경로로 못 푸는 file URI 호스트다: {parsed.hostname!r} — {src}"
        )
    if parsed.params or parsed.query or parsed.fragment:
        raise MediaUnresolved(where, f"file URI에 params/query/fragment가 붙어 있다: {src}")
    try:
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise MediaUnresolved(
            where, f"퍼센트 인코딩이 UTF-8로 해석되지 않는다: {src} ({error})"
        ) from error
    if not decoded:
        raise MediaUnresolved(where, f"file URI에 경로가 없다: {src}")
    return Path(decoded)


def _asset_content_id(original_sha256: str) -> str:
    """내용 신원 — 원본 해시 하나에서만 나온다(도메인 구분자 ＋ NUL ＋ 해시)."""
    return hashlib.sha256((_CONTENT_ID_DOMAIN + "\0" + original_sha256).encode()).hexdigest()
