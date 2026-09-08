"""P1-03 미디어 신원 해석의 계약 — 원본/프록시, 오버라이드, 내용 신원.

계획("Input and output" ＋ "Source analysis, clocks, identity")의 정본: 명시적
미디어 맵이 XML media-rep URI를 덮어쓰고, 원본은 언제나 필요하며 통째로 해시된다.
프록시가 없으면 이미지 원본으로 물러나고, 원본을 못 찾으면 프록시가 있어도 준비가
막힌다. `hasAudio`는 **정확히 그 리소스**에서만 온다 — 확장자·바이트·같은 내용을
가진 다른 리소스에서 추론하지 않는다. 낡은 `r2` 같은 원시 id는 룩업 키일 뿐,
문서를 가리지 않는 신원이 되지 못한다.

모든 문서는 인라인 합성이고 미디어 파일은 tmp 합성 바이트다 — 픽스처·골든은
만지지 않는다(실행 규율).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import threading
from dataclasses import fields
from pathlib import Path
from urllib.parse import quote

import pytest

from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import (
    MediaOverride,
    ResolvedMedia,
    load_media_map,
    resolve_media,
    sha256_file,
)
from vlogsrt.project.models import ProjectDocument, UnsupportedProject

# ─── 합성 문서·미디어 빌더 ────────────────────────────────────────────────

FORMAT_30 = (
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>'
)

ORIGINAL = "original-media"
PROXY = "proxy-media"

ASSET_1 = "/fcpxml[1]/resources[1]/asset[1]"


def rep(kind: str, src: str) -> str:
    return f'<media-rep kind="{kind}" src="{src}"/>'


def asset(xml_id: str, media_reps: str, *, has_audio: str = "1") -> str:
    return (
        f'<asset id="{xml_id}" name="{xml_id}.mp4" start="0s" duration="600s"'
        f' hasAudio="{has_audio}" format="r1">\n      {media_reps}\n    </asset>'
    )


def clips(*refs: str, name: str = "클립") -> str:
    parts = []
    for index, ref in enumerate(refs):
        parts.append(
            f'<asset-clip ref="{ref}" name="{name}{index}" offset="{index * 5}s"'
            ' start="0s" duration="5s"/>'
        )
    return "\n            ".join(parts)


def parse_media_project(
    tmp_path: Path, assets: list[str], spine_items: str, *, stem: str = "project"
) -> ProjectDocument:
    resources = "\n    ".join([FORMAT_30, *assets])
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        '<fcpxml version="1.14">\n'
        f"  <resources>\n    {resources}\n  </resources>\n"
        '  <library>\n    <event name="이벤트">\n      <project name="프로젝트">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        f"          <spine>\n            {spine_items}\n          </spine>\n"
        "        </sequence>\n      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )
    path = tmp_path / f"{stem}.fcpxml"
    path.write_text(body, encoding="utf-8")
    return parse_project(path)


def write_media(directory: Path, name: str, payload: bytes) -> Path:
    path = directory / name
    path.write_bytes(payload)
    return path


def media_dir(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def file_uri(path: Path) -> str:
    """실제 FCPXML처럼 퍼센트 인코딩된 file URI — 한국어·공백이 %XX가 된다."""
    return "file://" + quote(str(path))


# ─── 오버라이드 우선순위 ─────────────────────────────────────────────────


def test_override_paths_take_precedence_over_xml_uris(tmp_path: Path) -> None:
    """명시적 미디어 맵은 XML media-rep original/proxy를 둘 다 덮어쓴다."""
    xml_original = write_media(tmp_path, "xml_original.mp4", b"xml-original-bytes")
    xml_proxy = write_media(tmp_path, "xml_proxy.mov", b"xml-proxy-bytes")
    map_original = write_media(tmp_path, "map_original.mp4", b"map-original-bytes")
    map_proxy = write_media(tmp_path, "map_proxy.mov", b"map-proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(xml_original)) + rep(PROXY, file_uri(xml_proxy)))],
        clips("r2"),
    )
    resolved = resolve_media(
        document, (MediaOverride("r2", original=map_original, proxy=map_proxy),)
    )
    assert len(resolved) == 1
    assert resolved[0].original == map_original
    assert resolved[0].image_source == map_proxy
    assert resolved[0].used_proxy is True
    assert resolved[0].original_sha256 == hashlib.sha256(b"map-original-bytes").hexdigest()


def test_override_proxy_alone_keeps_xml_original(tmp_path: Path) -> None:
    """오버라이드는 필드별로 우선한다 — proxy만 바꿔도 original은 XML 그대로다."""
    xml_original = write_media(tmp_path, "original.mp4", b"original-bytes")
    xml_proxy = write_media(tmp_path, "proxy.mov", b"xml-proxy-bytes")
    map_proxy = write_media(tmp_path, "map_proxy.mov", b"map-proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(xml_original)) + rep(PROXY, file_uri(xml_proxy)))],
        clips("r2"),
    )
    resolved = resolve_media(document, (MediaOverride("r2", proxy=map_proxy),))
    assert resolved[0].original == xml_original
    assert resolved[0].image_source == map_proxy
    assert resolved[0].used_proxy is True


def test_override_with_no_paths_is_an_accepted_no_op(tmp_path: Path) -> None:
    """필드 없는 오버라이드는 아무것도 바꾸지 않는다 — 키 존재 선언일 뿐이다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2")
    )
    without = resolve_media(document)
    with_empty = resolve_media(document, (MediaOverride("r2"),))
    assert with_empty == without


# ─── file URI 해석 ────────────────────────────────────────────────────────


def test_percent_encoded_file_uris_decode_to_exact_paths(tmp_path: Path) -> None:
    """한국어·공백 파일명은 %XX로 오며, 명시적 UTF-8 퍼센트 복원만이 경로가 된다."""
    directory = media_dir(tmp_path, "미디어")
    original = write_media(directory, "한국어 블로그.mp4", b"korean-bytes")
    proxy = write_media(directory, "한국어 블로그 프록시.mov", b"korean-proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)) + rep(PROXY, file_uri(proxy)))],
        clips("r2"),
    )
    resolved = resolve_media(document)
    assert resolved[0].original == original
    assert resolved[0].image_source == proxy


def test_localhost_file_uri_resolves_like_empty_host(tmp_path: Path) -> None:
    """`file://localhost/…`도 로컬이다 — 그 밖의 호스트는 원격이므로 거부한다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    src = "file://localhost" + quote(str(original))
    document = parse_media_project(tmp_path, [asset("r2", rep(ORIGINAL, src))], clips("r2"))
    resolved = resolve_media(document)
    assert resolved[0].original == original


@pytest.mark.parametrize(
    ("src", "token"),
    [
        ("http://example.com/media/a.mp4", "http"),
        ("ftp://example.com/media/a.mp4", "ftp"),
        ("/media/a.mp4", "스킴"),
        ("media/a.mp4", "스킴"),
        ("file://server/media/a.mp4", "server"),
        ("file:///media/a.mp4?part=1", "query"),
        ("file:///media/a.mp4#장면", "fragment"),
        ("file://localhost", "경로"),
        ("file:relative.mov", "상대"),
    ],
)
def test_non_file_or_unresolvable_uris_are_rejected(tmp_path: Path, src: str, token: str) -> None:
    """file 스킴이 아니거나 로컬 경로로 못 푸는 URI는 자리와 이유를 대고 거부한다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    assets = [asset("r2", rep(ORIGINAL, src)), asset("r3", rep(ORIGINAL, file_uri(original)))]
    document = parse_media_project(tmp_path, assets, clips("r2", "r3"))
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1, str(excinfo.value)
    assert token in excinfo.value.reason, str(excinfo.value)


def test_malformed_percent_encoding_is_rejected(tmp_path: Path) -> None:
    """`%FF%FE`는 UTF-8이 아니다 — 조용한 치환이 아니라 명시적 거부다."""
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, "file:///media/%FF%FE.mp4"))], clips("r2")
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert "UTF-8" in excinfo.value.reason


def test_malformed_proxy_uri_is_rejected_even_with_valid_original(tmp_path: Path) -> None:
    """프록시 URI도 같은 해석 규칙을 지난다 — 못 푸는 URI는 '없는 프록시'가 아니다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)) + rep(PROXY, "http://cdn/proxy.mov"))],
        clips("r2"),
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert "http" in excinfo.value.reason


@pytest.mark.parametrize("kind", [ORIGINAL, PROXY])
def test_multiple_same_kind_reps_are_ambiguous(tmp_path: Path, kind: str) -> None:
    """같은 종류의 media-rep이 둘이면 어느 쪽인지 못 정한다 — 첫째를 고르지 않는다."""
    valid = write_media(tmp_path, "valid.mp4", b"valid")
    original = write_media(tmp_path, "a.mp4", b"a")
    other = write_media(tmp_path, "b.mp4", b"b")
    # 흔들리는 종류와 반대쪽은 멀쩡하게 둔다 — 중복 그 자체만이 거부 사유다.
    other_kind = PROXY if kind == ORIGINAL else ORIGINAL
    reps = (
        rep(other_kind, file_uri(valid))
        + rep(kind, file_uri(original))
        + rep(kind, file_uri(other))
    )
    document = parse_media_project(tmp_path, [asset("r2", reps)], clips("r2"))
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert kind in excinfo.value.reason


def test_override_bypasses_ambiguous_xml_reps(tmp_path: Path) -> None:
    """오버라이드가 덮어쓰는 종류의 XML 중복은 무관하다 — 그 URI를 안 쓴다."""
    original = write_media(tmp_path, "a.mp4", b"a")
    other = write_media(tmp_path, "b.mp4", b"b")
    map_original = write_media(tmp_path, "map.mp4", b"map")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)) + rep(ORIGINAL, file_uri(other)))],
        clips("r2"),
    )
    resolved = resolve_media(document, (MediaOverride("r2", original=map_original),))
    assert resolved[0].original == map_original


# ─── 원본·프록시 준비 ─────────────────────────────────────────────────────


def test_original_from_xml_is_hashed_in_full(tmp_path: Path) -> None:
    """원본은 통째로 해시된다 — 부분 읽기·메타데이터 대용이 없다."""
    original = write_media(tmp_path, "original.mp4", b"every-single-byte-counts")
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)) + rep(PROXY, file_uri(proxy)))],
        clips("r2"),
    )
    resolved = resolve_media(document)
    assert resolved[0].original == original
    assert resolved[0].original_sha256 == hashlib.sha256(b"every-single-byte-counts").hexdigest()
    assert resolved[0].image_source == proxy
    assert resolved[0].used_proxy is True


def test_missing_proxy_rep_falls_back_to_original_for_images(tmp_path: Path) -> None:
    """프록시 표현이 없으면 이미지도 원본에서 뽑는다(계획 "Input and output")."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2")
    )
    resolved = resolve_media(document)
    assert resolved[0].image_source == original
    assert resolved[0].used_proxy is False


def test_device_as_original_is_rejected_before_opening(tmp_path: Path) -> None:
    """/dev/null은 일반 파일이 아니다 — 열어 보기 전에 거부한다(블록 없음)."""
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, "file:///dev/null") + rep(PROXY, file_uri(proxy)))],
        clips("r2"),
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert "/dev/null" in excinfo.value.reason


def test_fifo_as_original_is_rejected_without_blocking(tmp_path: Path) -> None:
    """FIFO 원본은 열면 막힌다 — is_file 선검사로 열기 전에 거부한다."""
    fifo = tmp_path / "pipe.mp4"
    os.mkfifo(str(fifo))
    # 선검사가 없는 빨간 상태에서 해시 열기가 막히지 않도록, 곧 닫는 쓴이를 둔다.
    writer = threading.Thread(target=lambda: os.close(os.open(str(fifo), os.O_WRONLY)), daemon=True)
    writer.start()
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(fifo)))], clips("r2")
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert "파일" in excinfo.value.reason


def test_unreadable_proxy_probes_once_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프록시가 일반 파일이어도 열어 읽지 못하면 못 쓰는 프록시다 — 원본으로 물러난다."""
    import vlogsrt.project.media as media_module

    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)) + rep(PROXY, file_uri(proxy)))],
        clips("r2"),
    )
    monkeypatch.setattr(media_module, "_read_probe", lambda path: False)
    resolved = resolve_media(document)
    assert resolved[0].image_source == original
    assert resolved[0].used_proxy is False


def test_absent_proxy_file_falls_back_to_original(tmp_path: Path) -> None:
    """프록시가 선언됐지만 파일이 없으면 없는 프록시다 — 원본으로 물러난다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    missing_proxy = tmp_path / "no_such_proxy.mov"
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)) + rep(PROXY, file_uri(missing_proxy)))],
        clips("r2"),
    )
    resolved = resolve_media(document)
    assert resolved[0].image_source == original
    assert resolved[0].used_proxy is False


def test_missing_original_fails_even_when_proxy_exists(tmp_path: Path) -> None:
    """원본 없이 프록시만 있는 애셋은 준비가 막힌다 — 프록시가 멀쩡해도 마찬가지."""
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(PROXY, file_uri(proxy)))], clips("r2")
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert "original" in excinfo.value.reason


def test_absent_original_file_blocks_readiness(tmp_path: Path) -> None:
    """원본 경로가 있어도 파일이 없으면 막힌다 — 프록시가 있어도 못 넘어간다."""
    missing = tmp_path / "no_such_original.mp4"
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(missing)) + rep(PROXY, file_uri(proxy)))],
        clips("r2"),
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document)
    assert excinfo.value.location == ASSET_1
    assert str(missing) in excinfo.value.reason


def test_override_original_pointing_at_missing_file_blocks(tmp_path: Path) -> None:
    """오버라이드가 준 원본도 같은 준비 게이트를 지난다 — 예외가 아니다."""
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    missing = tmp_path / "no_such_original.mp4"
    document = parse_media_project(
        tmp_path, [asset("r2", rep(PROXY, file_uri(proxy)))], clips("r2")
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document, (MediaOverride("r2", original=missing),))
    assert excinfo.value.location == ASSET_1
    assert str(missing) in excinfo.value.reason


def test_override_original_can_rescue_proxy_only_asset(tmp_path: Path) -> None:
    """프록시뿐 애셋도 맵이 원본을 주면 준비가 열린다 — 원본은 언제나 필요하다."""
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(PROXY, file_uri(proxy)))], clips("r2")
    )
    resolved = resolve_media(document, (MediaOverride("r2", original=original),))
    assert resolved[0].original == original
    assert resolved[0].image_source == proxy
    assert resolved[0].used_proxy is True


# ─── 신원: 내용·리소스·문서 스코프 ───────────────────────────────────────


def test_asset_content_id_formula_is_exact(tmp_path: Path) -> None:
    """`sha256("source-content-v1\\0" + original_sha256)` — 공식 그대로다."""
    original = write_media(tmp_path, "original.mp4", b"formula-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2")
    )
    resolved = resolve_media(document)[0]
    original_sha256 = hashlib.sha256(b"formula-bytes").hexdigest()
    expected = hashlib.sha256(("source-content-v1\0" + original_sha256).encode()).hexdigest()
    assert resolved.asset_content_id == expected


def test_same_basename_different_content_stays_distinct(tmp_path: Path) -> None:
    """파일 이름이 같아도 내용이 다르면 다른 소스다 — 이름은 신원이 아니다."""
    first = write_media(media_dir(tmp_path, "a"), "source.mp4", b"first-content")
    second = write_media(media_dir(tmp_path, "b"), "source.mp4", b"second-content")
    document = parse_media_project(
        tmp_path,
        [
            asset("r2", rep(ORIGINAL, file_uri(first))),
            asset("r3", rep(ORIGINAL, file_uri(second))),
        ],
        clips("r2", "r3"),
    )
    resolved = resolve_media(document)
    assert len(resolved) == 2
    assert resolved[0].asset_content_id != resolved[1].asset_content_id
    assert resolved[0].original_sha256 != resolved[1].original_sha256


def test_identical_bytes_different_paths_share_asset_content_id(tmp_path: Path) -> None:
    """같은 바이트면 경로가 달라도 내용 신원은 하나다 — 리소스 신원은 둘 그대로다."""
    first = write_media(media_dir(tmp_path, "one"), "clip.mp4", b"same-bytes")
    second = write_media(media_dir(tmp_path, "two"), "다른이름.mov", b"same-bytes")
    document = parse_media_project(
        tmp_path,
        [
            asset("r2", rep(ORIGINAL, file_uri(first))),
            asset("r3", rep(ORIGINAL, file_uri(second))),
        ],
        clips("r2", "r3"),
    )
    resolved = resolve_media(document)
    assert resolved[0].asset_content_id == resolved[1].asset_content_id
    assert resolved[0].original_sha256 == resolved[1].original_sha256
    assert resolved[0].resource_scoped_id != resolved[1].resource_scoped_id
    assert resolved[0].original != resolved[1].original


def test_repeated_clip_instances_resolve_to_one_media(tmp_path: Path) -> None:
    """한 애셋을 세 번 써도 미디어는 하나다 — 전사·해시도 한 번뿐이다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)))],
        clips("r2", "r2", "r2"),
    )
    resolved = resolve_media(document)
    assert len(document.storyline) == 3
    assert len(resolved) == 1
    assert resolved[0].resource_scoped_id == document.resources[0].scoped_id


def test_two_documents_sharing_one_file_share_content_id_only(tmp_path: Path) -> None:
    """내용 신원은 문서 바깥이다 — 리소스 신원은 문서 해시 안에 머문다."""
    original = write_media(tmp_path, "original.mp4", b"shared-bytes")
    first = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2"), stem="first"
    )
    second = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)))],
        clips("r2", name="다른클립"),
        stem="second",
    )
    assert first.input_sha256 != second.input_sha256
    first_media = resolve_media(first)[0]
    second_media = resolve_media(second)[0]
    assert first_media.asset_content_id == second_media.asset_content_id
    assert first_media.resource_scoped_id != second_media.resource_scoped_id


def test_same_raw_id_in_two_documents_does_not_bind(tmp_path: Path) -> None:
    """`r2`는 룩업 키일 뿐이다 — 두 문서의 r2는 서로 다른 미디어로 산다."""
    first_file = write_media(media_dir(tmp_path, "one"), "source.mp4", b"first-doc-bytes")
    second_file = write_media(media_dir(tmp_path, "two"), "source.mp4", b"second-doc-bytes")
    first = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(first_file)))],
        clips("r2"),
        stem="first",
    )
    second = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(second_file)))],
        clips("r2"),
        stem="second",
    )
    first_media = resolve_media(first)[0]
    second_media = resolve_media(second)[0]
    assert first_media.resource_scoped_id != second_media.resource_scoped_id
    assert first_media.asset_content_id != second_media.asset_content_id
    assert first_media.original_sha256 != second_media.original_sha256


def test_equal_bytes_different_has_audio_keep_resource_truth(tmp_path: Path) -> None:
    """같은 파일을 가리키는 두 리소스가 hasAudio=1/0이면 각자의 선언이 산다.

    내용 신원은 하나지만 오디오 사실은 리소스의 것이다 — 나중 오디오 정책이
    내용 키를 공유하더라도 이 불을 합치면 안 된다(sol-no-audio-disposition).
    """
    original = write_media(tmp_path, "original.mp4", b"shared-bytes")
    document = parse_media_project(
        tmp_path,
        [
            asset("r2", rep(ORIGINAL, file_uri(original)), has_audio="1"),
            asset("r3", rep(ORIGINAL, file_uri(original)), has_audio="0"),
        ],
        clips("r2", "r3"),
    )
    resolved = resolve_media(document)
    assert len(resolved) == 2
    assert [media.has_audio for media in resolved] == [True, False]
    assert resolved[0].asset_content_id == resolved[1].asset_content_id
    assert resolved[0].resource_scoped_id != resolved[1].resource_scoped_id


def test_declared_no_audio_resolves_normally(tmp_path: Path) -> None:
    """명시적 0만으로 '오디오 없음'이 선언된다 — 신원·해시는 여전히 필요하다."""
    original = write_media(tmp_path, "silent.png", b"silent-bytes")
    document = parse_media_project(
        tmp_path,
        [asset("r2", rep(ORIGINAL, file_uri(original)), has_audio="0")],
        clips("r2"),
    )
    resolved = resolve_media(document)
    assert resolved[0].has_audio is False
    assert resolved[0].original_sha256 == hashlib.sha256(b"silent-bytes").hexdigest()


def test_results_follow_resource_document_order(tmp_path: Path) -> None:
    """해석 순서는 문서 순서다 — 정렬이나 해시 순서로 다시 섞지 않는다."""
    first = write_media(tmp_path, "a.mp4", b"aaa")
    second = write_media(tmp_path, "b.mp4", b"bbb")
    document = parse_media_project(
        tmp_path,
        [
            asset("r9", rep(ORIGINAL, file_uri(second))),
            asset("r2", rep(ORIGINAL, file_uri(first))),
        ],
        clips("r9", "r2"),
    )
    resolved = resolve_media(document)
    assert [media.original for media in resolved] == [second, first]


def test_document_without_assets_resolves_to_empty(tmp_path: Path) -> None:
    """애셋 없는 문서(간격뿐인 스토리라인)는 빈 미디어 셋이다."""
    document = parse_media_project(tmp_path, [], '<gap name="갭" offset="0s" duration="5s"/>')
    assert resolve_media(document) == ()


def test_resolution_is_deterministic(tmp_path: Path) -> None:
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2")
    )
    assert resolve_media(document) == resolve_media(document)


# ─── 오버라이드 키 검사 ──────────────────────────────────────────────────


def test_unknown_override_resource_id_is_rejected(tmp_path: Path) -> None:
    """문서에 없는 resource_id는 거부한다 — 조용히 무시하면 오타가 새어 나간다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2")
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(document, (MediaOverride("rX", original=original),))
    assert excinfo.value.location == str(document.input_path)
    assert "rX" in excinfo.value.reason


def test_duplicate_override_ids_are_rejected_at_resolve(tmp_path: Path) -> None:
    """같은 resource_id의 오버라이드가 둘이면 모순이다 — 나중 것을 잡지 않는다."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    other = write_media(tmp_path, "other.mp4", b"other-bytes")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(original)))], clips("r2")
    )
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_media(
            document,
            (MediaOverride("r2", original=original), MediaOverride("r2", original=other)),
        )
    assert "r2" in excinfo.value.reason


def test_override_matches_only_the_exact_resource_id(tmp_path: Path) -> None:
    """이웃 애셋이 같은 파일 이름을 가져도 오버라이드에 끌려가지 않는다."""
    first = write_media(media_dir(tmp_path, "a"), "source.mp4", b"first")
    second = write_media(media_dir(tmp_path, "b"), "source.mp4", b"second")
    third = write_media(media_dir(tmp_path, "c"), "source.mp4", b"third")
    document = parse_media_project(
        tmp_path,
        [
            asset("r2", rep(ORIGINAL, file_uri(first))),
            asset("r3", rep(ORIGINAL, file_uri(third))),
        ],
        clips("r2", "r3"),
    )
    resolved = resolve_media(document, (MediaOverride("r2", original=second),))
    assert resolved[0].original == second
    assert resolved[1].original == third
    assert len({media.asset_content_id for media in resolved}) == 2


# ─── 미디어 맵 파일 ──────────────────────────────────────────────────────


def write_map(tmp_path: Path, payload: str) -> Path:
    path = tmp_path / "media-map.json"
    path.write_text(payload, encoding="utf-8")
    return path


def test_load_media_map_reads_entries_in_order(tmp_path: Path) -> None:
    """절대 경로는 그대로, 상대 경로는 맵 파일의 부모에서 푼다 — 배열 순서 그대로."""
    original = write_media(tmp_path, "original.mp4", b"original-bytes")
    proxy = write_media(tmp_path, "proxy.mov", b"proxy-bytes")
    path = write_map(
        tmp_path,
        json.dumps(
            [
                {"resource_id": "r2", "original": str(original), "proxy": str(proxy)},
                {"resource_id": "r3", "proxy": "media/proxy.mov"},
                {"resource_id": "r4", "original": str(original)},
            ]
        ),
    )
    assert load_media_map(path) == (
        MediaOverride("r2", original=original, proxy=proxy),
        MediaOverride("r3", proxy=tmp_path / "media" / "proxy.mov"),
        MediaOverride("r4", original=original),
    )


def test_relative_map_paths_ignore_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """상대 경로의 기준은 맵 파일의 부모다 — cwd를 옮겨도 같은 곳을 가리킨다."""
    elsewhere = media_dir(tmp_path, "elsewhere")
    path = write_map(
        tmp_path, json.dumps([{"resource_id": "r2", "original": "media/original.mp4"}])
    )
    monkeypatch.chdir(elsewhere)
    override = load_media_map(path)[0]
    assert override.original == tmp_path / "media" / "original.mp4"


def test_tilde_and_env_stay_literal_without_expansion(tmp_path: Path) -> None:
    """`~`·`$HOME`은 펴지 않는다 — 문자 그대로 맵 부모 아래의 상대 경로로 남는다."""
    path = write_map(
        tmp_path,
        json.dumps(
            [
                {"resource_id": "r2", "original": "~/clips/a.mp4"},
                {"resource_id": "r3", "proxy": "$HOME/clips/b.mov"},
            ]
        ),
    )
    first, second = load_media_map(path)
    assert first.original == tmp_path / "~/clips/a.mp4"
    assert second.proxy == tmp_path / "$HOME/clips/b.mov"


def test_load_media_map_empty_list_is_allowed(tmp_path: Path) -> None:
    path = write_map(tmp_path, "[]")
    assert load_media_map(path) == ()


@pytest.mark.parametrize(
    ("payload", "token"),
    [
        ('{"r2": {}}', "배열"),
        ('["r2"]', "객체"),
        ('[{"original": "/a"}]', "resource_id"),
        ('[{"resource_id": "", "original": "/a"}]', "resource_id"),
        ('[{"resource_id": 7}]', "resource_id"),
        ('[{"resource_id": "r2", "originl": "/a"}]', "originl"),
        ('[{"resource_id": "r2", "original": 5}]', "original"),
        ('[{"resource_id": "r2", "proxy": true}]', "proxy"),
        ('[{"resource_id": "r2", "original": ""}]', "original"),
        ('[{"resource_id": "r2"}]', "original"),
        ('[{"resource_id": "r3"}]', "original"),
        ('[{"resource_id": "r2", "original": null}]', "null"),
        ('[{"resource_id": "r2", "proxy": null}]', "null"),
        ('[{"resource_id": "r2", "original": "file:///media/a.mp4"}]', "URI"),
        ('[{"resource_id": "r2", "proxy": "http://cdn/p.mov"}]', "URI"),
        ('[{"resource_id": "r2"}, {"resource_id": "r2"}]', "r2"),
        ('[{"resource_id": "r2", "original": "/a"}, {"resource_id": "r2"}]', "r2"),
        ("not json at all", "JSON"),
        ("", "JSON"),
    ],
)
def test_load_media_map_rejects_malformed_entries(tmp_path: Path, payload: str, token: str) -> None:
    """맵 스키마 위반은 ValueError다 — 맵 경로와 항목·키를 댄다(동결 계약)."""
    path = write_map(tmp_path, payload)
    with pytest.raises(ValueError) as excinfo:
        load_media_map(path)
    assert str(path) in str(excinfo.value)
    assert token in str(excinfo.value), str(excinfo.value)


def test_load_media_map_missing_file_is_named(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-map.json"
    with pytest.raises(ValueError) as excinfo:
        load_media_map(missing)
    assert str(missing) in str(excinfo.value)


def test_loaded_map_feeds_resolve_media(tmp_path: Path) -> None:
    """맵 파일 → 오버라이드 → 해석이 한 줄로 이어진다 — 둥근 통합 증거."""
    xml_original = write_media(tmp_path, "xml.mp4", b"xml")
    map_original = write_media(tmp_path, "map.mp4", b"map")
    document = parse_media_project(
        tmp_path, [asset("r2", rep(ORIGINAL, file_uri(xml_original)))], clips("r2")
    )
    path = write_map(tmp_path, json.dumps([{"resource_id": "r2", "original": str(map_original)}]))
    resolved = resolve_media(document, load_media_map(path))
    assert resolved[0].original == map_original


# ─── 해시 유틸리티 ───────────────────────────────────────────────────────


def test_sha256_file_matches_hashlib_digest(tmp_path: Path) -> None:
    path = write_media(tmp_path, "payload.bin", b"hello world\n")
    assert sha256_file(path) == hashlib.sha256(b"hello world\n").hexdigest()


def test_sha256_file_streams_large_payload(tmp_path: Path) -> None:
    """청크를 넘는 큰 파일도 통째로 해시한다 — 경계에서 잘리지 않는다."""
    payload = bytes(range(256)) * (1024 * 16)
    path = write_media(tmp_path, "large.bin", payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        sha256_file(tmp_path / "no-such.bin")


# ─── 얼어 붙은 계약 ──────────────────────────────────────────────────────


def test_media_models_are_frozen_and_slotted() -> None:
    for model in (MediaOverride, ResolvedMedia):
        assert model.__dataclass_params__.frozen, model
        field_names = tuple(field.name for field in fields(model))
        assert field_names == tuple(model.__slots__), model


def test_media_interface_signatures_match_the_contract() -> None:
    """P2·P3·P5·P7이 소비하는 이름·순서 — 계약 그대로다."""
    assert [field.name for field in fields(MediaOverride)] == [
        "resource_id",
        "original",
        "proxy",
    ]
    assert [field.name for field in fields(ResolvedMedia)] == [
        "resource_scoped_id",
        "asset_content_id",
        "original",
        "image_source",
        "original_sha256",
        "has_audio",
        "used_proxy",
    ]


def test_media_public_surface_is_frozen_to_the_contract() -> None:
    """공개 면은 두 데이터클래스와 네 함수뿐이다 — 새 예외 형을 내보내지 않는다."""
    import vlogsrt.project.media as media_module

    assert set(media_module.__all__) == {
        "MediaOverride",
        "ResolvedMedia",
        "load_media_map",
        "resolve_media",
        "sha256_file",
    }
    for name in media_module.__all__:
        assert getattr(media_module, name) is not None, name
    assert not hasattr(media_module, "MediaUnresolved")


def test_media_module_imports_only_allowed_roots() -> None:
    """표준 라이브러리와 모델만 쓴다 — subprocess·openai·FCP 데이터베이스가 없다."""
    module = sys.modules["vlogsrt.project.media"]
    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "hashlib",
        "json",
        "pathlib",
        "typing",
        "urllib",
        "urllib.parse",
    }
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [node.module or ""]
        else:
            continue
        for root in roots:
            assert root not in {"subprocess", "openai", "sqlite3"}, (
                f"media가 {root}을(를) import한다"
            )
            if root.startswith("vlogsrt"):
                assert root in {"vlogsrt.project", "vlogsrt.project.models"}, (
                    f"media가 허용 밖 모듈을 import한다: {root}"
                )
            else:
                assert root in allowed, f"media가 허용 밖 모듈을 import한다: {root}"
