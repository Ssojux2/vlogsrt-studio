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
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlparse

from vlogsrt.project.models import AssetResource, ProjectDocument, UnsupportedProject

_CONTENT_ID_DOMAIN = "source-content-v1"
"""내용 신원의 도메인 구분자 — 오디오 정책이 분석 키를 덧붙일 때와 갈라진다."""

_READ_CHUNK_BYTES = 1024 * 1024
"""한 번에 읽는 해시 청크 크기 — 동작 임계값이 아니라 읽기 버퍼다."""

_MAP_ENTRY_KEYS = frozenset({"resource_id", "original", "proxy"})
"""미디어 맵 항목의 전체 키 집합 — 여기 없는 키는 오타이므로 거부한다."""

__all__ = [
    "MediaOverride",
    "ResolvedMedia",
    "load_media_map",
    "resolve_media",
    "sha256_file",
]
"""동결된 공개 면 — 이 모듈은 새 예외 형을 내보내지 않는다.

맵 스키마 위반은 `ValueError`, 문서·파일 준비 거부는 models의
`UnsupportedProject`를 그대로 쓴다(위치+이유 규율 유지).
"""


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


def load_media_map(path: Path) -> tuple[MediaOverride, ...]:
    """미디어 맵 파일(JSON 객체 배열)을 읽는다. 읽는 것 외에 아무것도 안 한다.

    배열이어야 하는 이유는 중복에 있다: 객체를 키로 묶으면 JSON은 뒤의 키가
    앞을 조용히 덮어쓴다. 배열은 중복이 **표현되고**, 그래서 거부할 수 있다.
    스키마 위반은 `ValueError`로 맵 경로와 항목·키를 댄다. 경로 값은 파일
    경로다: 절대 경로는 그대로, 상대 경로는 **맵 파일의 부모**가 기준이다 —
    읽는 시점에 절대로 붙여 두므로 cwd와 무관하다. `~`·`$…`는 펴지지 않고
    글자 그대로 남는다. 스킴이 파싱되는 값(file:·http:·data: …)은 이 층의
    입력이 아니므로 거부한다 — URI 해석은 XML media-rep의 몫이다.
    """
    where = str(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"{where}: 미디어 맵 파일이 없다") from error
    except OSError as error:
        raise ValueError(f"{where}: 미디어 맵 파일을 읽지 못한다 ({error})") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{where}: 미디어 맵이 JSON이 아니다 ({error})") from error
    if not isinstance(payload, list):
        raise ValueError(f"{where}: 미디어 맵은 객체의 배열이어야 한다")
    # 상대 경로의 기준을 **읽는 지금** 절대로 붙여 둔다 — 그래야 cwd가 바뀐 뒤에도
    # 오버라이드가 맵 파일을 따라간다.
    base = path.resolve().parent
    overrides: list[MediaOverride] = []
    seen: set[str] = set()
    for position, entry in enumerate(payload, start=1):
        overrides.append(_map_entry(entry, position, where, seen, base))
    return tuple(overrides)


def _map_entry(
    entry: object, position: int, where: str, seen: set[str], base: Path
) -> MediaOverride:
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: 미디어 맵 항목 {position}이 객체가 아니다: {entry!r}")
    unknown = sorted(set(entry) - _MAP_ENTRY_KEYS)
    if unknown:
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}에 모르는 키가 있다: {unknown}"
            " — 받는 키는 resource_id/original/proxy뿐이다"
        )
    resource_id: object = entry.get("resource_id")
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}의 resource_id가 무효다: {resource_id!r}"
            " — 비지 않은 문자열이어야 한다"
        )
    if resource_id in seen:
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}의 resource_id가 중복이다: {resource_id!r}"
        )
    seen.add(resource_id)
    original = _map_path(entry, "original", position, where, base)
    proxy = _map_path(entry, "proxy", position, where, base)
    if original is None and proxy is None:
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}({resource_id!r})에 경로가 하나도 없다"
            " — original/proxy 중 적어도 하나는 있어야 한다"
        )
    return MediaOverride(resource_id=resource_id, original=original, proxy=proxy)


def _map_path(entry: dict, key: str, position: int, where: str, base: Path) -> Path | None:
    """키가 **있을 때만** 경로다 — 명시적 null과 키 부재는 다르다(동결 계약)."""
    if key not in entry:
        return None
    value: object = entry[key]
    if value is None:
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}의 {key}가 null이다"
            " — 키를 아예 빼거나 비지 않은 경로 문자열을 써라"
        )
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}의 {key}가 무효다: {value!r}"
            " — 비지 않은 경로 문자열이어야 한다"
        )
    scheme = urlparse(value).scheme
    if scheme:
        # 스킴이 파싱되면 URI다 — file:·http:·data: 그 무엇이든 경로가 아니다.
        raise ValueError(
            f"{where}: 미디어 맵 항목 {position}의 {key}가 URI다: {value!r}"
            f" (스킴 {scheme!r}) — 여기는 파일 경로지 URI가 아니다"
        )
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base / candidate


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
            raise UnsupportedProject(
                str(document.input_path),
                f"오버라이드에 중복 resource_id가 있다: {override.resource_id!r}"
                " — 어느 쪽이 진짜인지 고르지 않는다",
            )
        indexed[override.resource_id] = override
    known_ids = {resource.xml_id for resource in document.resources}
    unknown = sorted(set(indexed) - known_ids)
    if unknown:
        raise UnsupportedProject(
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
    # 일반 파일 선검사 — FIFO·장치를 열면 읽기가 막힐 수 있으므로 열기 전에 거부한다.
    if not original.is_file():
        raise UnsupportedProject(
            where, f"원본 미디어가 일반 파일이 아니다: {original} — FIFO·장치는 받지 않는다"
        )
    try:
        original_sha256 = sha256_file(original)
    except OSError as error:
        raise UnsupportedProject(
            where, f"원본 미디어를 읽을 수 없다: {original} ({error})"
        ) from error
    image_source = original
    used_proxy = False
    if proxy is not None and proxy.is_file() and _read_probe(proxy):
        # 프록시는 실제로 읽을 수 있을 때만 이미지 원본이 된다 — 아니면 원본에서 뽑는다.
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


def _read_probe(path: Path) -> bool:
    """1바이트 읽기 탐침 — 프록시가 열려도 읽지 못하면 못 쓰는 프록시다."""
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _select_original(resource: AssetResource, override: MediaOverride | None, where: str) -> Path:
    """원본 경로 — 오버라이드가 우선하고, 없으면 XML의 유일한 original-media다."""
    if override is not None and override.original is not None:
        return override.original
    candidates = [rep for rep in resource.media_reps if rep.kind == "original"]
    if not candidates:
        raise UnsupportedProject(
            where,
            "원본(original-media) media-rep이 없다 — 원본은 프록시가 있어도"
            " 언제나 필요하다(프록시는 이미지에만 쓰인다)",
        )
    if len(candidates) > 1:
        raise UnsupportedProject(
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
        raise UnsupportedProject(
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
        raise UnsupportedProject(where, f"media-rep src가 file URI가 아니다(스킴이 없다): {src}")
    if scheme != "file":
        raise UnsupportedProject(where, f"media-rep src가 file 스킴이 아니다: {scheme!r} — {src}")
    if parsed.hostname not in (None, "localhost"):
        raise UnsupportedProject(
            where, f"로컬 경로로 못 푸는 file URI 호스트다: {parsed.hostname!r} — {src}"
        )
    if parsed.params or parsed.query or parsed.fragment:
        raise UnsupportedProject(where, f"file URI에 params/query/fragment가 붙어 있다: {src}")
    try:
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise UnsupportedProject(
            where, f"퍼센트 인코딩이 UTF-8로 해석되지 않는다: {src} ({error})"
        ) from error
    if not decoded:
        raise UnsupportedProject(where, f"file URI에 경로가 없다: {src}")
    path = Path(_drive_path(decoded))
    if not path.is_absolute():
        # XML의 자리는 문서 자신뿐이다 — 상대 경로를 cwd로 풀면 문서 밖 상태가 새어 든다.
        raise UnsupportedProject(where, f"file URI가 상대 경로다: {src} — 절대 경로여야 한다")
    return path


def _drive_path(decoded: str) -> str:
    """URI 경로 칸 → 이 운영체제의 경로 표기. POSIX는 그대로다.

    Windows에서 `Path("/C:/a")`는 드라이브 없는 루트 상대(`\\C:\\a`)라 절대 경로가 아니다 —
    `as_uri()`가 지은 `file:///C:/…`가 모두 「상대 경로」로 거부됐다(W-84a). 이 모듈은 표준
    라이브러리 중 정한 것만 쓰므로 운영체제는 `Path`의 형으로 가린다.
    """
    letter, colon, rest = decoded[1:2], decoded[2:3], decoded[3:4]
    drive = decoded[:1] == "/" and letter.isascii() and letter.isalpha() and colon == ":"
    if drive and rest in ("", "/") and isinstance(Path(), PureWindowsPath):
        return decoded[1:]
    return decoded


def _asset_content_id(original_sha256: str) -> str:
    """내용 신원 — 원본 해시 하나에서만 나온다(도메인 구분자 ＋ NUL ＋ 해시)."""
    return hashlib.sha256((_CONTENT_ID_DOMAIN + "\0" + original_sha256).encode()).hexdigest()
