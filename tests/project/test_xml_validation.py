"""xmllint-only XML 검증 경계의 계약 — P1-04가 여기서 얼어 붙는다.

정본은 태스크 p1-04와 `scripts/check_invariants.py`의 `_xmllint_boundary_failures`다.
정적 검사가 형태를 보고, 이 테스트가 **행동**을 증명한다: 흉내 낸 run이 받은 인자
전부(argv·shell·executable)를 검사하고, 실제 xmllint와 이 호스트에 설치된 읽기 전용
1.14 DTD로 참·거짓 판정을 잰다.

DTD를 파일 URI로 넘기는 계약은 취향이 아니라 실측이다 — 설치 경로에 빈칸이 있어
("Final Cut Pro.app") `--dtdvalid`에 평범한 경로를 주면 xmllint가 DTD를 아예 읽지
못한다(rc=2). 픽스처·골든은 만지지 않는다(실행 규율) — 실측은 전부 이 호스트의
설치 파일과 tmp_path 안 합성 문서다.

실측 반환 코드: 0=유효, 1=파서(잘 이루어지지 않음), 3=스키마 위반.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from vlogsrt.project import xml_validation
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

# ─── P1-02 지정 부분집합의 합성 문서 — 인라인이고 픽스처가 아니다 ────────

FORMAT_30 = (
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>'
)

ASSET_DEFAULT = (
    '<asset id="r2" name="source.mp4" uid="AAAA1111" start="0s" duration="600s"'
    ' hasAudio="1" hasVideo="1" format="r1">\n'
    '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
    '    <media-rep kind="proxy-media" src="file:///media/source_proxy.mov"/>\n'
    "  </asset>"
)

SPINE_DEFAULT = (
    '<asset-clip ref="r2" name="클립1" offset="0s" start="10s" duration="100s"'
    ' tcFormat="NDF"/>\n'
    '    <gap name="갭" offset="100s" start="0s" duration="10s"/>\n'
    '    <asset-clip ref="r2" name="클립2" offset="110s" start="200s"'
    ' duration="90s" enabled="0" srcEnable="video"/>'
)


def _operand(path: Path) -> str:
    """제품이 xmllint에 넘기는 입력 칸 — Windows는 ASCII `file:///` URI다(W-84a)."""
    resolved = path.resolve()
    return resolved.as_uri() if sys.platform == "win32" else str(resolved)


def build_fcpxml(*, proxy_kind: str = "proxy-media") -> str:
    """지정 부분집합의 기본형 — `proxy_kind`만 갈아끼워 스키마 오답을 만든다."""
    asset = ASSET_DEFAULT.replace('kind="proxy-media"', f'kind="{proxy_kind}"')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        '<fcpxml version="1.14">\n'
        f"  <resources>\n    {FORMAT_30}\n    {asset}\n  </resources>\n"
        "  <library>\n"
        '    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        f"            {SPINE_DEFAULT}\n"
        "          </spine>\n"
        "        </sequence>\n"
        "      </project>\n"
        "    </event>\n"
        "  </library>\n"
        "</fcpxml>\n"
    )


# ─── 흉내 — run이 받은 것을 전부 기록한다 ────────────────────────────────


def _stub_run(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stderr: str = ""
) -> list[tuple[list[str], dict[str, object]]]:
    """subprocess.run을 흉내 낸다 — argv와 키워드를 그대로 남긴다."""
    seen: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((list(argv), dict(kwargs)))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _stub_tool(monkeypatch: pytest.MonkeyPatch, tool: str = "/usr/bin/xmllint") -> None:
    """which를 흉내 낸다 — argv 검사를 도구 설치 여부와 분리한다."""
    monkeypatch.setattr(xml_validation.shutil, "which", lambda name: tool)


# ─── argv 경계 — 정확히 두 형태, 셸 없음 ─────────────────────────────────


def test_wellformedness_argv_is_exactly_the_frozen_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DTD 없이 부르면 argv는 [xmllint, --nonet, --noout, path] 그 자체다."""
    seen = _stub_run(monkeypatch)
    _stub_tool(monkeypatch)
    path = tmp_path / "문서.fcpxml"

    result = validate_xml(path)

    assert len(seen) == 1, "subprocess 호출은 정확히 한 번이다"
    argv, kwargs = seen[0]
    assert argv == ["/usr/bin/xmllint", "--nonet", "--noout", _operand(path)]
    assert kwargs["shell"] is False
    assert "executable" not in kwargs
    assert result.valid is True
    assert result.dtd_checked is False
    assert result.diagnostics == ()


def test_dtd_argv_is_exactly_the_frozen_shape_with_a_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DTD를 주면 --dtdvalid 자리는 resolve().as_uri()다 — 빈칸이 한 칸에 산다."""
    seen = _stub_run(monkeypatch)
    _stub_tool(monkeypatch)
    dtd = tmp_path / "DTD with  spaces v1.dtd"
    dtd.write_text("<!ELEMENT fcpxml ANY>", encoding="utf-8")
    path = tmp_path / "문서.fcpxml"

    result = validate_xml(path, dtd_path=dtd)

    assert len(seen) == 1
    argv, kwargs = seen[0]
    assert argv == [
        "/usr/bin/xmllint",
        "--nonet",
        "--noout",
        "--dtdvalid",
        dtd.resolve().as_uri(),
        _operand(path),
    ]
    assert " " not in argv[4], "빈칸은 %20으로 인코딩되어 한 argv 칸에 있어야 한다"
    assert "%20" in argv[4]
    assert kwargs["shell"] is False
    assert "executable" not in kwargs
    assert result.dtd_checked is True


def test_a_malicious_looking_path_stays_one_plain_argv_element(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """경로 문자열은 조립되지 않고 해석되지도 않는다 — argv의 마지막 칸 그대로다."""
    seen = _stub_run(monkeypatch)
    _stub_tool(monkeypatch)
    path = tmp_path / "evil '; rm -rf *; $(touch pwned) `id`.fcpxml"

    validate_xml(path)

    argv, kwargs = seen[0]
    assert len(argv) == 4
    assert argv[-1] == _operand(path)
    assert kwargs["shell"] is False


def test_a_relative_option_looking_path_becomes_an_absolute_operand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xmllint는 '-'로 시작하는 글자를 깃발로 읽는다 — 절대 경로만이 피연산자다.

    실측 블로커: `Path('--version')`을 그대로 넘기면 xmllint가 깃발로 받아들여
    버전 출력과 함께 종료 0 — 없는 파일이 valid=True로 둔갑한다. 임시 cwd로
    격리해 주변 파일이 결론을 못 바꾸게 한다.
    """
    seen = _stub_run(monkeypatch)
    _stub_tool(monkeypatch)
    monkeypatch.chdir(tmp_path)

    validate_xml(Path("--version"))

    argv, _kwargs = seen[0]
    assert argv[-1] == _operand(Path("--version"))
    assert argv[-1].startswith("file:///" if sys.platform == "win32" else "/")
    assert "--version" not in argv


def test_an_ordinary_relative_input_is_normalized_to_its_absolute_spot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정규화는 '--version' 특이사항이 아니다 — 평범한 상대 이름도 절대 자리로 간다."""
    seen = _stub_run(monkeypatch)
    _stub_tool(monkeypatch)
    monkeypatch.chdir(tmp_path)

    validate_xml(Path("plain/ordinary.fcpxml"))

    argv, _kwargs = seen[0]
    assert argv[-1] == _operand(tmp_path / "plain" / "ordinary.fcpxml")
    assert argv[-1].startswith("file:///" if sys.platform == "win32" else "/")


# ─── 없음은 판정이 아니다 — unavailable ─────────────────────────────────


def test_missing_tool_raises_unavailable_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xmllint가 없으면 자식 프로세스는 하나도 생기지 않는다."""
    seen = _stub_run(monkeypatch)
    monkeypatch.setattr(xml_validation.shutil, "which", lambda name: None)

    with pytest.raises(XmlValidationUnavailable, match="xmllint"):
        validate_xml(tmp_path / "문서.fcpxml")

    assert seen == []


def test_units_survive_a_path_without_xmllint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈 PATH를 진짜 which로 본다 — 단위 검사는 호스트 도구 없이도 같은 길을 걷는다."""
    monkeypatch.setenv("PATH", str(tmp_path))
    seen = _stub_run(monkeypatch)

    with pytest.raises(XmlValidationUnavailable, match="xmllint"):
        validate_xml(tmp_path / "문서.fcpxml")

    assert seen == []


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
@pytest.mark.parametrize("with_dtd", [False, True])
def test_a_dead_executable_is_unavailable_not_a_leaked_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[OSError],
    with_dtd: bool,
) -> None:
    """which는 통과했는데 실행이 죽는 경주 — 판정은 일어나지 않았다, 0이 아니다."""
    _stub_tool(monkeypatch, tool="/방금-사라진/xmllint")

    def dying_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure("xmllint 실행 실패")

    monkeypatch.setattr(subprocess, "run", dying_run)
    dtd = tmp_path / "DTD with  spaces v1.dtd"
    dtd.write_text("<!ELEMENT fcpxml ANY>", encoding="utf-8")

    with pytest.raises(XmlValidationUnavailable, match="실행하지 못했다"):
        validate_xml(tmp_path / "문서.fcpxml", dtd_path=dtd if with_dtd else None)


def test_missing_requested_dtd_raises_unavailable_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청한 DTD가 없으면 '틀렸다'가 아니라 '못 봤다'다 — 부르지도 않는다."""
    seen = _stub_run(monkeypatch)
    _stub_tool(monkeypatch)

    with pytest.raises(XmlValidationUnavailable, match="DTD"):
        validate_xml(tmp_path / "문서.fcpxml", dtd_path=tmp_path / "없는.dtd")

    assert seen == []


def test_unavailable_is_a_runtime_error_with_the_frozen_shape() -> None:
    """얼어 붙은 계약대로 RuntimeError의 서브클래스다 — 다른 뿌리가 아니다."""
    assert issubclass(XmlValidationUnavailable, RuntimeError)


# ─── 0이 아닌 종료 — 예외가 아니라 진단이다 ──────────────────────────────


@pytest.mark.parametrize("returncode", [1, 3])
def test_nonzero_exit_is_a_result_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    """파서 오류(1)·스키마 위반(3) 모두 valid=False와 진단으로 돌아온다."""
    _stub_tool(monkeypatch)
    _stub_run(monkeypatch, returncode=returncode, stderr="문서.fcpxml:1: parser error : 조각났다\n")

    result = validate_xml(tmp_path / "문서.fcpxml")

    assert result.valid is False
    assert result.diagnostics == ("문서.fcpxml:1: parser error : 조각났다",)


def test_diagnostics_are_bounded_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stderr은 상한이 있고 제어 문자는 걷어낸다 — 악의적 문서가 로그를 채우지 못한다."""
    noisy = (
        "\x07벨\n"
        + "A" * 1000
        + "\n\n\n"
        + "\x1b[31m빨강\x1b[0m\n"
        + "".join(f"줄 {number}\n" for number in range(100))
    )
    _stub_tool(monkeypatch)
    _stub_run(monkeypatch, returncode=3, stderr=noisy)

    result = validate_xml(tmp_path / "문서.fcpxml")

    assert result.valid is False
    assert len(result.diagnostics) <= xml_validation._MAX_DIAGNOSTIC_LINES
    overflow = "A" * xml_validation._MAX_DIAGNOSTIC_LINE_CHARS
    for line in result.diagnostics:
        assert len(line) <= xml_validation._MAX_DIAGNOSTIC_LINE_CHARS + 1  # 잘림 표지 포함
        assert not [ch for ch in line if ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0]
    assert any(line.startswith(overflow) for line in result.diagnostics)
    assert result.diagnostics[-1].startswith("…")


def test_result_is_an_immutable_record_of_exactly_three_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """판정 기록은 세 필드의 불변 값이다 — 검증은 표시할 뿐이다."""
    _stub_tool(monkeypatch)
    _stub_run(monkeypatch)
    result = validate_xml(tmp_path / "문서.fcpxml")

    assert [field.name for field in fields(XmlValidation)] == [
        "valid",
        "diagnostics",
        "dtd_checked",
    ]
    assert isinstance(result.diagnostics, tuple)
    with pytest.raises(FrozenInstanceError):
        result.valid = True  # type: ignore[misc]


# ─── 실측 — 진짜 xmllint와 설치된 1.14 DTD ──────────────────────────────


def _assert_installed_dtd_digest() -> None:
    """해시 재검증 — 설치 DTD가 달라지면 못 넘긴다(과거 평가가 무의미해진다)."""
    digest = hashlib.sha256(INSTALLED_DTD.read_bytes()).hexdigest()
    assert digest == INSTALLED_DTD_SHA256, f"설치 DTD가 바뀌었다: {digest}"


@needs_xmllint
@needs_installed_dtd
def test_real_supported_document_passes_the_installed_dtd(tmp_path: Path) -> None:
    """P1-02 지정 부분집합 문서는 설치 DTD 대조에서 종료 코드 0으로 지난다."""
    _assert_installed_dtd_digest()
    path = tmp_path / "supported.fcpxml"
    path.write_text(build_fcpxml(), encoding="utf-8")

    result = validate_xml(path, dtd_path=INSTALLED_DTD)

    assert result.valid is True
    assert result.dtd_checked is True
    assert result.diagnostics == ()


@needs_xmllint
@needs_installed_dtd
def test_real_schema_invalid_spelling_fails_only_under_the_dtd(tmp_path: Path) -> None:
    """`kind="banana-media"`는 잘 이루어진 문서다 — 깨진 것은 스키마 철자다."""
    _assert_installed_dtd_digest()
    path = tmp_path / "invalid.fcpxml"
    path.write_text(build_fcpxml(proxy_kind="banana-media"), encoding="utf-8")

    under_dtd = validate_xml(path, dtd_path=INSTALLED_DTD)
    without_dtd = validate_xml(path)

    assert under_dtd.valid is False
    assert under_dtd.dtd_checked is True
    assert any("banana-media" in line for line in under_dtd.diagnostics)
    assert without_dtd.valid is True, "DTD 없이는 잘 이루어져 있다 — 실패는 스키마에서 왔다"
    assert without_dtd.dtd_checked is False


@needs_xmllint
def test_real_malformed_xml_reports_diagnostics_without_raising(tmp_path: Path) -> None:
    """잘 이루어지지 않은 문서는 종료 코드 1 — 진단을 들고 valid=False로 돌아온다."""
    path = tmp_path / "broken.fcpxml"
    path.write_text('<fcpxml version="1.14"><event name="열린 채로">', encoding="utf-8")

    result = validate_xml(path)

    assert result.valid is False
    assert result.dtd_checked is False
    assert result.diagnostics


@needs_xmllint
def test_real_malicious_name_is_validated_never_executed(tmp_path: Path) -> None:
    """셸 확장 문구가 이름에 실려 있어도 자막처럼 검증될 뿐 실행되지 않는다."""
    # Windows 파일 이름에는 `*`을 쓸 수 없다 — 나머지 셸 글자(`$( )`·`;`·`` ` ``)는 그대로다.
    star = "*" if sys.platform != "win32" else "STAR"
    path = tmp_path / f"evil $(touch pwned) '; rm -rf {star}; `id`.fcpxml"
    path.write_text(build_fcpxml(), encoding="utf-8")

    result = validate_xml(path)

    assert result.valid is True
    assert not (tmp_path / "pwned").exists(), "셸이었다면 이 흔적이 생겼을 것이다"


@needs_xmllint
def test_real_nonexistent_option_looking_input_is_not_version_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """없는 '--version' 입력이 libxml 출력과 valid=True를 내는 일은 없다 — 정규화가 막는다.

    임시 cwd로 격리한다 — 작업 폴러에 같은 이름 파일이 있어도 결론이 못 바뀌게.
    """
    monkeypatch.chdir(tmp_path)

    result = validate_xml(Path("--version"))

    assert result.valid is False
    assert result.diagnostics
    assert not any("libxml" in line for line in result.diagnostics)


@needs_xmllint
def test_real_ordinary_relative_input_is_validated_at_its_resolved_spot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """평범한 상대 입력도 부르는 그대로 받는다 — 판정은 절대 자리에서 일어난다."""
    (tmp_path / "ordinary.fcpxml").write_text(build_fcpxml(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = validate_xml(Path("ordinary.fcpxml"))

    assert result.valid is True
    assert result.diagnostics == ()


@needs_xmllint
def test_real_dash_prefixed_filename_is_a_plain_operand(tmp_path: Path) -> None:
    """'-'로 시작하는 실재 파일은 피연산자다 — 잘 이루어졌으면 유효하다."""
    path = tmp_path / "-대시로-시작하는.fcpxml"
    path.write_text(build_fcpxml(), encoding="utf-8")

    result = validate_xml(path)

    assert result.valid is True
    assert result.diagnostics == ()
