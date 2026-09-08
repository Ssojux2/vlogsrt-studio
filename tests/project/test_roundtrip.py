"""P1-05 왕복의 계약 — 신원 보존 복사만을 증명하고 다른 아무것도 않는다.

`write_identity`는 `ProjectDocument.raw_xml`을 **바이트 그대로** 같은 디렉터리
임시파일＋`os.replace`로 옮긴다 — 파싱·직렬화 정규화는 일어나지 않는다.
`validate_roundtrip`은 먼저 P1-04 xmllint 경계에 문서를 맡기고(도구·DTD가 없으면
`XmlValidationUnavailable`이 그대로 샌다), 다음 P1-02 파서로 다시 읽어 원본과
비교한다. 비교는 바이트 동등성 ＋ 해시와 무관한 의미 동등성이다 — `input_path`·
`info_path`·`input_sha256`·`scoped_id`·`instance_id`는 문서마다 스코프가 다르므로
비교하지 않는다. 둘 중 무엇이 어긋나도 `XmlValidation(valid=False)`과 **상한 있는**
진단으로 돌아온다 — 합성된 통과도, 새 오류 계층도 없다.

이것은 편집된 FCPXML 직렬화가 아니다(P6의 소관). 모든 XML은 인라인 합성이고
픽스처·골든은 만지지 않는다. 실제 Final Cut 가져오기는 사람의 몫이다(PENDING_HUMAN).
"""

from __future__ import annotations

import ast
import hashlib
import shutil
import sys
import tempfile
from dataclasses import fields
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project import fcpxml, xml_validation
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.models import ClipInstance, GapInstance
from vlogsrt.project.timebase import format_fcpx_time
from vlogsrt.project.xml_validation import (
    XmlValidation,
    XmlValidationUnavailable,
    validate_xml,
)

# ─── 실측 재료 — 이 호스트의 읽기 전용 설치 DTD ──────────────────────────

INSTALLED_DTD = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework"
    "/Versions/A/Resources/FCPXMLv1_14.dtd"
)
INSTALLED_DTD_SHA256 = "33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5"

needs_xmllint = pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint이 없는 환경")
needs_installed_dtd = pytest.mark.skipif(
    not INSTALLED_DTD.is_file(), reason="설치된 Final Cut Pro 1.14 DTD가 없는 환경"
)

RATES: list[tuple[str, Fraction]] = [
    ("1/24s", Fraction(1, 24)),
    ("1/25s", Fraction(1, 25)),
    ("1/30s", Fraction(1, 30)),
    ("1/50s", Fraction(1, 50)),
    ("1/60s", Fraction(1, 60)),
    ("1001/24000s", Fraction(1001, 24000)),
    ("1001/30000s", Fraction(1001, 30000)),
    ("1001/60000s", Fraction(1001, 60000)),
]
"""지원 프레임 레이트 여덟 전부 — 정수계와 1001계(23.976·29.97·59.94)."""


def _assert_installed_dtd_digest() -> None:
    """해시 재검증 — 설치 DTD가 달라지면 못 넘긴다(과거 평가가 무의미해진다)."""
    digest = hashlib.sha256(INSTALLED_DTD.read_bytes()).hexdigest()
    assert digest == INSTALLED_DTD_SHA256, f"설치 DTD가 바뀌었다: {digest}"


# ─── 합성 문서 — 여덟 레이트에서 요구된 구성을 전부 담는다 ────────────────
#
# 0이 아닌 `sequence.tcStart`·`asset.start`, 같은 애셋(r2)의 반복 사용, 두 번째
# 애셋(r3 — kind 생략 = DTD 기본값 original-media, hasAudio="0"), 갭, `enabled="0"`＋
# `srcEnable="video"` 클립. 모든 시각은 프레임 수의 정준 형태다(1001계에서 정수 초는
# 격자 위가 아니다). 이 형태는 실제 xmllint＋설치 1.14 DTD에서 여덟 레이트 전부
# 종료 코드 0으로 통과했다. 임계값이 아니라 문서의 골격이므로 인라인 상수다.


def build_rate_document(frame_duration_text: str, expected: Fraction) -> str:
    def frame(count: int) -> str:
        return format_fcpx_time(count * expected)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        '<fcpxml version="1.14">\n'
        "  <resources>\n"
        f'    <format id="r1" name="FFVideoFormat1080p" frameDuration="{frame_duration_text}"'
        ' width="1920" height="1080"/>\n'
        f'    <asset id="r2" name="source.mp4" uid="AAAA1111" start="{frame(7200)}"'
        f' duration="{frame(9000)}" hasAudio="1" hasVideo="1" format="r1">\n'
        '      <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        '      <media-rep kind="proxy-media" src="file:///media/source_proxy.mov"/>\n'
        "    </asset>\n"
        f'    <asset id="r3" start="{frame(1500)}" duration="{frame(4000)}" hasAudio="0"'
        ' format="r1">\n'
        '      <media-rep src="file:///media/silent.mp4"/>\n'
        "    </asset>\n"
        "  </resources>\n"
        "  <library>\n"
        '    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        f'        <sequence format="r1" tcStart="{frame(3600)}" duration="{frame(7800)}">\n'
        "          <spine>\n"
        f'            <asset-clip ref="r2" name="클립1" offset="{frame(3600)}"'
        f' start="{frame(7200)}" duration="{frame(3000)}" tcFormat="NDF"/>\n'
        f'            <gap name="갭" offset="{frame(6600)}" start="0s"'
        f' duration="{frame(600)}"/>\n'
        f'            <asset-clip ref="r2" name="클립2" offset="{frame(7200)}"'
        f' start="{frame(8200)}" duration="{frame(2700)}" enabled="0" srcEnable="video"/>\n'
        f'            <asset-clip ref="r3" name="클립3" offset="{frame(9900)}"'
        f' start="{frame(1500)}" duration="{frame(1500)}"/>\n'
        "          </spine>\n"
        "        </sequence>\n"
        "      </project>\n"
        "    </event>\n"
        "  </library>\n"
        "</fcpxml>\n"
    )


def write_source(tmp_path: Path, body: str, name: str = "source.fcpxml") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


PREEXISTING = b"<old-content/>\n"
"""실패 주입 전 목적지에 있던 바이트 — 이 값 그대로 남아야 한다."""


def _dir_entries(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


def _assert_bounded(result: XmlValidation) -> None:
    """진단은 상한이 있고 한 줄도 토해내지 않는다 — XML 덤프는 금지다."""
    assert len(result.diagnostics) <= fcpxml._MAX_MISMATCH_LINES
    for line in result.diagnostics:
        assert len(line) <= fcpxml._MAX_MISMATCH_LINE_CHARS + 1  # 잘림 표지 포함
        assert not [ch for ch in line if ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0]


# ─── 바이트 동등 왕복 — 여덟 레이트 전부 ─────────────────────────────────


@needs_xmllint
@needs_installed_dtd
@pytest.mark.parametrize(("frame_duration_text", "expected"), RATES)
def test_identity_roundtrip_is_byte_exact_and_valid_across_all_rates(
    tmp_path: Path, frame_duration_text: str, expected: Fraction
) -> None:
    """쓴 것은 원본 바이트 그대로다 — DTD는 앞뒤로 통과, 원본은 한 자도 안 변한다."""
    _assert_installed_dtd_digest()
    body = build_rate_document(frame_duration_text, expected)
    source_path = write_source(tmp_path, body)
    before = validate_xml(source_path, dtd_path=INSTALLED_DTD)
    assert before.valid is True, before.diagnostics

    source = parse_project(source_path)
    raw_before = source.raw_xml
    mtime_before = source_path.stat().st_mtime_ns

    written = fcpxml.write_identity(source, tmp_path / "written.fcpxml")

    assert written == tmp_path / "written.fcpxml"
    assert (tmp_path / "written.fcpxml").read_bytes() == raw_before
    assert source.raw_xml == raw_before
    assert source_path.read_bytes() == raw_before
    assert source_path.stat().st_mtime_ns == mtime_before
    assert _dir_entries(tmp_path) == {"source.fcpxml", "written.fcpxml"}, "임시파일 잔여가 없다"

    after = fcpxml.validate_roundtrip(source, tmp_path / "written.fcpxml", dtd_path=INSTALLED_DTD)
    assert after.valid is True, after.diagnostics
    assert after.dtd_checked is True
    assert after.diagnostics == ()

    without_dtd = fcpxml.validate_roundtrip(source, tmp_path / "written.fcpxml")
    assert without_dtd.valid is True
    assert without_dtd.dtd_checked is False

    reparsed = parse_project(tmp_path / "written.fcpxml")
    assert reparsed.frame_duration == expected
    assert reparsed.sequence_tc_start == 3600 * expected
    assert [type(item).__name__ for item in reparsed.storyline] == [
        "ClipInstance",
        "GapInstance",
        "ClipInstance",
        "ClipInstance",
    ]


@needs_xmllint
def test_source_document_and_file_survive_the_roundtrip(tmp_path: Path) -> None:
    """쓰기와 검증 어느 쪽도 원본 문서·원본 파일을 건드리지 않는다."""
    body = build_rate_document("1/30s", Fraction(1, 30))
    source_path = write_source(tmp_path, body)
    source = parse_project(source_path)
    twin = parse_project(source_path)

    fcpxml.write_identity(source, tmp_path / "written.fcpxml")
    fcpxml.validate_roundtrip(source, tmp_path / "written.fcpxml")

    assert source == twin, "쓰기·검증이 원본 문서를 바꿨으면 안 된다"
    assert source.raw_xml == twin.raw_xml
    assert source == parse_project(source_path)


# ─── 의미 보존 — 해시와 무관한 동등성 ────────────────────────────────────


@needs_xmllint
def test_roundtrip_preserves_exact_semantics_at_30fps(tmp_path: Path) -> None:
    """길이·순서·참조·UID·정적 속성·비활성·갭 — 원본 그대로다."""
    body = build_rate_document("1/30s", Fraction(1, 30))
    source = parse_project(write_source(tmp_path / "원본", body, "project.fcpxml"))
    written = tmp_path / "사본" / "roundtripped.fcpxml"
    written.parent.mkdir(parents=True, exist_ok=True)
    fcpxml.write_identity(source, written)

    result = fcpxml.validate_roundtrip(source, written)
    assert result.valid is True, result.diagnostics

    reparsed = parse_project(written)
    assert reparsed.version == "1.14"
    assert reparsed.frame_duration == Fraction(1, 30)
    assert reparsed.sequence_tc_start == Fraction(120)  # 3600프레임
    r2, r3 = reparsed.resources
    assert (r2.xml_id, r2.uid, r2.name, r2.format_id) == ("r2", "AAAA1111", "source.mp4", "r1")
    assert (r2.start, r2.duration, r2.has_audio) == (Fraction(240), Fraction(300), True)
    assert [(rep.kind, rep.src) for rep in r2.media_reps] == [
        ("original", "file:///media/source.mp4"),
        ("proxy", "file:///media/source_proxy.mov"),
    ]
    assert (r3.xml_id, r3.uid, r3.name) == ("r3", "", "")
    assert (r3.start, r3.duration, r3.has_audio) == (Fraction(50), Fraction(400, 3), False)
    assert [rep.kind for rep in r3.media_reps] == ["original"]  # kind 생략 = DTD 기본값

    clip1, gap, clip2, clip3 = reparsed.storyline
    assert isinstance(clip1, ClipInstance) and isinstance(clip2, ClipInstance)
    assert isinstance(clip3, ClipInstance) and isinstance(gap, GapInstance)
    assert (clip1.offset, clip1.start, clip1.duration) == (
        Fraction(120),
        Fraction(240),
        Fraction(100),
    )
    assert clip1.enabled is True
    assert clip1.src_enable == "all"
    assert (gap.offset, gap.duration) == (Fraction(220), Fraction(20))
    assert not hasattr(gap, "resource_scoped_id")
    assert (clip2.offset, clip2.start, clip2.duration) == (
        Fraction(240),
        Fraction(820, 3),
        Fraction(90),
    )
    assert clip2.enabled is False
    assert clip2.src_enable == "video"
    assert (clip3.offset, clip3.start, clip3.duration) == (
        Fraction(330),
        Fraction(50),
        Fraction(50),
    )
    assert clip1.resource_scoped_id == r2.scoped_id, "반복 애셋 참조는 r2로 남는다"
    assert clip2.resource_scoped_id == r2.scoped_id
    assert clip3.resource_scoped_id == r3.scoped_id
    assert clip1.element_path.endswith("asset-clip[1]")
    assert gap.element_path.endswith("gap[1]")
    assert clip2.element_path.endswith("asset-clip[2]")
    assert clip3.element_path.endswith("asset-clip[3]")


@needs_xmllint
def test_semantics_do_not_depend_on_input_path_or_hash_scope(tmp_path: Path) -> None:
    """원본과 사본은 디렉터리·이름이 달라도 의미가 같으면 통과한다.

    경로·해시 스코프(input_path·info_path·input_sha256·scoped_id·instance_id)는
    문서마다 다른 것이 정상이므로 왕복 비교에서 제외한다.
    """
    body = build_rate_document("1/50s", Fraction(1, 50))
    source = parse_project(write_source(tmp_path / "a", body, "one.fcpxml"))
    written = tmp_path / "b" / "two.fcpxml"
    written.parent.mkdir(parents=True, exist_ok=True)
    fcpxml.write_identity(source, written)
    reparsed = parse_project(written)

    assert reparsed.input_path != source.input_path
    assert reparsed.info_path != source.info_path

    result = fcpxml.validate_roundtrip(source, written)
    assert result.valid is True, result.diagnostics


@needs_xmllint
@needs_installed_dtd
def test_30_and_2997_are_distinct_and_stay_distinct(tmp_path: Path) -> None:
    """30fps와 29.97fps는 다른 격자다 — 왕복은 각자 온전하고 서로 섞이지 않는다."""
    _assert_installed_dtd_digest()
    flat = build_rate_document("1/30s", Fraction(1, 30))
    ntsc = build_rate_document("1001/30000s", Fraction(1001, 30000))
    assert Fraction(1, 30) != Fraction(1001, 30000)
    assert format_fcpx_time(Fraction(1, 30)) != format_fcpx_time(Fraction(1001, 30000))

    source_flat = parse_project(write_source(tmp_path, flat, "flat.fcpxml"))
    source_ntsc = parse_project(write_source(tmp_path, ntsc, "ntsc.fcpxml"))
    fcpxml.write_identity(source_flat, tmp_path / "flat-out.fcpxml")
    fcpxml.write_identity(source_ntsc, tmp_path / "ntsc-out.fcpxml")
    assert parse_project(tmp_path / "flat-out.fcpxml").frame_duration == Fraction(1, 30)
    assert parse_project(tmp_path / "ntsc-out.fcpxml").frame_duration == Fraction(1001, 30000)

    crossed = fcpxml.validate_roundtrip(source_flat, tmp_path / "ntsc-out.fcpxml")
    assert crossed.valid is False
    assert crossed.dtd_checked is False
    assert any("frame_duration" in line for line in crossed.diagnostics)


# ─── 원자적 쓰기 — 실패해도 예전 목적지와 흔적 없음 ────────────────────────


def test_replacement_overwrites_a_preexisting_destination(tmp_path: Path) -> None:
    """목적지에 다른 내용이 있어도 원본 바이트로 통째로 바뀐다 — 잔여 없음."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    destination = tmp_path / "destination.fcpxml"
    destination.write_bytes(PREEXISTING)

    fcpxml.write_identity(source, destination)

    assert destination.read_bytes() == source.raw_xml
    assert _dir_entries(tmp_path) == {"source.fcpxml", "destination.fcpxml"}


def test_injected_write_failure_preserves_destination_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """절반쯤 쓰다 실패해도 — 목적지는 바이트 그대로, 임시파일은 사라진다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/60s", Fraction(1, 60))))
    destination = tmp_path / "destination.fcpxml"
    destination.write_bytes(PREEXISTING)
    entries_before = _dir_entries(tmp_path)

    real_temporary_file = tempfile.NamedTemporaryFile

    def exploding_factory(**kwargs: object) -> object:
        handle = real_temporary_file(**kwargs)  # type: ignore[arg-type]
        original_write = handle.write

        def half_then_fail(data: bytes) -> int:
            original_write(data[: max(len(data) // 2, 1)])
            raise OSError("주입한 쓰기 실패")

        handle.write = half_then_fail  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(fcpxml.tempfile, "NamedTemporaryFile", exploding_factory)

    with pytest.raises(OSError, match="주입한 쓰기 실패"):
        fcpxml.write_identity(source, destination)

    assert destination.read_bytes() == PREEXISTING
    assert _dir_entries(tmp_path) == entries_before, "절반짜리 임시파일이 남으면 안 된다"


def test_injected_replace_failure_preserves_destination_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """교체 단계가 실패해도 같다 — 목적지 불변, 임시파일 청소."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/24s", Fraction(1, 24))))
    destination = tmp_path / "destination.fcpxml"
    destination.write_bytes(PREEXISTING)
    entries_before = _dir_entries(tmp_path)

    def boom(source_path: object, destination_path: object) -> None:
        raise OSError("주입한 교체 실패")

    monkeypatch.setattr(fcpxml.os, "replace", boom)

    with pytest.raises(OSError, match="주입한 교체 실패"):
        fcpxml.write_identity(source, destination)

    assert destination.read_bytes() == PREEXISTING
    assert _dir_entries(tmp_path) == entries_before, "임시파일이 목적지 폴더에 남으면 안 된다"


# ─── 어긋남의 증거 — 상한 있고 결정적이다 ────────────────────────────────


@needs_xmllint
def test_byte_mismatch_is_reported_not_canonicalized(tmp_path: Path) -> None:
    """줄바꿈 하나의 차이도 통과가 아니다 — 정규화로 덮지 않고 어긋남으로 말한다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    padded = source.raw_xml + b"\n"
    written = tmp_path / "padded.fcpxml"
    written.write_bytes(padded)

    result = fcpxml.validate_roundtrip(source, written)

    assert result.valid is False
    assert result.dtd_checked is False
    _assert_bounded(result)
    assert any("바이트" in line for line in result.diagnostics)
    assert not any("scoped" in line or "instance" in line for line in result.diagnostics)
    assert fcpxml.validate_roundtrip(source, written) == result, "진단은 결정적이다"


@needs_xmllint
@needs_installed_dtd
def test_validator_invalid_written_document_fails_with_dtd_evidence(tmp_path: Path) -> None:
    """DTD 철자가 틀린 사본은 P1-04 판정 그대로 돌아온다 — 합성 통과가 아니다."""
    _assert_installed_dtd_digest()
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    wrong = build_rate_document("1/30s", Fraction(1, 30)).replace(
        'kind="proxy-media"', 'kind="banana-media"'
    )
    written = write_source(tmp_path, wrong, "banana.fcpxml")

    under_dtd = fcpxml.validate_roundtrip(source, written, dtd_path=INSTALLED_DTD)
    without_dtd = fcpxml.validate_roundtrip(source, written)

    assert under_dtd.valid is False
    assert under_dtd.dtd_checked is True
    _assert_bounded(under_dtd)
    assert any("banana-media" in line for line in under_dtd.diagnostics)
    assert without_dtd.valid is False, "DTD 없어도 재파싱이 같은 철자를 거절한다"
    assert without_dtd.dtd_checked is False
    assert any("banana-media" in line for line in without_dtd.diagnostics)


@needs_xmllint
def test_malformed_written_document_fails_at_the_validator_first(tmp_path: Path) -> None:
    """조각난 문서는 xmllint 경계에서 먼저 걸린다 — 재파싱 전에 결정난다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    written = tmp_path / "broken.fcpxml"
    written.write_bytes(b"<fcpxml version='1.14'><library>")

    result = fcpxml.validate_roundtrip(source, written)

    assert result.valid is False
    assert result.dtd_checked is False
    _assert_bounded(result)
    assert result.diagnostics


@needs_xmllint
def test_unsupported_structure_in_written_document_is_bounded_false(tmp_path: Path) -> None:
    """잘 이루어졌지만 부분집합 밖(타이틀)인 사본 — 위치와 이유로 거절한다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/25s", Fraction(1, 25))))
    titled = build_rate_document("1/25s", Fraction(1, 25)).replace(
        "</spine>",
        '            <title name="타이틀" offset="0s" duration="1s"/>\n          </spine>',
    )
    written = write_source(tmp_path, titled, "titled.fcpxml")

    result = fcpxml.validate_roundtrip(source, written)

    assert result.valid is False
    assert result.dtd_checked is False
    _assert_bounded(result)
    assert any("title" in line for line in result.diagnostics)


@needs_xmllint
@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda body: body.replace('duration="100s"', 'duration="97s"'), "storyline[0].duration"),
        (lambda body: body.replace('name="source.mp4"', 'name="renamed.mp4"'), "resources[0].name"),
        (lambda body: body.replace('enabled="0"', 'enabled="1"'), "storyline[2].enabled"),
        (lambda body: body.replace('duration="20s"', 'duration="19s"'), "storyline[1].duration"),
        (
            lambda body: body.replace(
                '<media-rep src="file:///media/silent.mp4"/>',
                '<media-rep kind="proxy-media" src="file:///media/silent.mp4"/>',
            ),
            "resources[1].media_reps",
        ),
    ],
)
def test_semantic_mismatch_returns_bounded_deterministic_evidence(
    tmp_path: Path, mutate: object, needle: str
) -> None:
    """의미가 하나라도 어긋나면 그 자리를 짚는다 — 재판정해도 같은 진단이다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    written = write_source(tmp_path, mutate(build_rate_document("1/30s", Fraction(1, 30))))

    result = fcpxml.validate_roundtrip(source, written)

    assert result.valid is False
    assert result.dtd_checked is False
    _assert_bounded(result)
    assert any(needle in line for line in result.diagnostics), result.diagnostics
    assert fcpxml.validate_roundtrip(source, written) == result, "진단은 결정적이다"


@needs_xmllint
def test_semantic_mismatch_lists_every_changed_field_in_order(tmp_path: Path) -> None:
    """바뀐 필드는 순서대로 전부 나열된다 — 첫 하나만 보고 조용히 끝나지 않는다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    mutated = build_rate_document("1/30s", Fraction(1, 30))
    mutated = mutated.replace('name="source.mp4"', 'name="renamed.mp4"')
    mutated = mutated.replace('duration="100s"', 'duration="98s"')
    written = write_source(tmp_path, mutated)

    result = fcpxml.validate_roundtrip(source, written)

    assert result.valid is False
    joined = "\n".join(result.diagnostics)
    assert "resources[0].name" in joined
    assert "storyline[0].duration" in joined


# ─── 없음은 판정이 아니다 — 경계 위임 ────────────────────────────────────


def _stub_tool(monkeypatch: pytest.MonkeyPatch, tool: str = "/usr/bin/xmllint") -> None:
    """which를 흉내 낸다 — 도구 설치 여부와 검사를 분리한다."""
    monkeypatch.setattr(xml_validation.shutil, "which", lambda name: tool)


def test_missing_xmllint_propagates_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """도구가 없으면 왕복 검증도 판정을 내리지 않는다 — 예외가 그대로 샌다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    written = tmp_path / "written.fcpxml"
    fcpxml.write_identity(source, written)
    monkeypatch.setattr(xml_validation.shutil, "which", lambda name: None)

    with pytest.raises(XmlValidationUnavailable, match="xmllint"):
        fcpxml.validate_roundtrip(source, written)


def test_missing_requested_dtd_propagates_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청한 DTD가 없으면 합성 통과가 아니라 '못 봤다'다."""
    _stub_tool(monkeypatch)
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    written = tmp_path / "written.fcpxml"
    fcpxml.write_identity(source, written)

    with pytest.raises(XmlValidationUnavailable, match="DTD"):
        fcpxml.validate_roundtrip(source, written, dtd_path=tmp_path / "없는.dtd")


@needs_xmllint
def test_validate_roundtrip_delegates_to_the_p1_04_boundary_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XML 검증은 xmllint 경계를 통과한다 — 왕복이 검증기를 따로 만들지 않는다."""
    source = parse_project(write_source(tmp_path, build_rate_document("1/30s", Fraction(1, 30))))
    written = tmp_path / "written.fcpxml"
    fcpxml.write_identity(source, written)
    calls: list[tuple[Path, Path | None]] = []
    real_validate = xml_validation.validate_xml

    def spy(path: Path, *, dtd_path: Path | None = None) -> XmlValidation:
        calls.append((path, dtd_path))
        return real_validate(path, dtd_path=dtd_path)

    monkeypatch.setattr(xml_validation, "validate_xml", spy)

    result = fcpxml.validate_roundtrip(source, written, dtd_path=INSTALLED_DTD)

    assert calls == [(written, INSTALLED_DTD)]
    assert result.valid is True


# ─── 얼어 붙은 계약의 형태 ────────────────────────────────────────────────


def test_roundtrip_reuses_the_frozen_result_record() -> None:
    """결과는 P1-04의 불변 3필드 기록이다 — 새 계층을 만들지 않는다."""
    assert [field.name for field in fields(XmlValidation)] == [
        "valid",
        "diagnostics",
        "dtd_checked",
    ]


def test_runtime_code_uses_no_bare_assert() -> None:
    """제품 코드의 옳음을 assert에 맡기지 않는다 — 판정은 반환값이다."""
    module = sys.modules["vlogsrt.project.fcpxml"]
    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    asserts = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assert)]
    assert not asserts, f"fcpxml.py에 bare assert가 있다: {asserts}"
