"""PROTO-2 러너 1/3 — 파싱 → 미디어 해석 → probe → PCM → 부재의 기록.

여기서 증명하는 것:

- 오디오 있는 리소스는 `(ResolvedMedia, PcmReference)` 쌍이 되고, 그 쌍이
  `speech._validated_pairs`를 **그대로** 통과한다 — 러너가 내는 모양이 곧 소비자의
  입력이다(쌍을 깨서 두 리스트로 돌려주지 않는다).
- 세 reason_code는 정해진 자리에서만 난다: `declared_no_audio`는 선언에서(probe
  없음), `stream_unavailable`은 probe 실패에서, `pcm_unavailable`은 참조 실패에서.
  `reason`에는 실제 예외 메시지가 실린다.
- 어떤 리소스도 조용히 빠지지 않는다(규칙 9). 혼합 프로젝트의 결과 길이는 입력
  리소스 수와 같고, 문서 순서가 유지된다. 해석 불가능한 미디어는 부재 기록이 아니라
  `UnsupportedProject` 그대로 올라온다(media.py의 준비 게이트).
- PCM 논리 주소는 `asset_content_id`로 키잡는다 — 서로 다른 xml_id를 가진 리소스
  둘이 같은 바이트를 가리키면 추출은 한 번이다(리소스 단위 접기가 못 하는 것).
- ffmpeg 미설치는 per-resource 부재 N개가 아니라 한 번의 하드 실패로 전파된다.

합성 문서·합성 미디어·스텁 probe/추출만 쓴다. 픽스처·골든은 만지지 않는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace as data_replace
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

import pytest

from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.project import pcm as pcm_module
from vlogsrt.project import runner
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.media import MediaOverride, ResolvedMedia
from vlogsrt.project.models import UnsupportedProject
from vlogsrt.project.pcm import AudioPolicy, PcmReference
from vlogsrt.project.speech import (
    UnavailableSourceAudio,
    _validated_pairs,
    transcribe_unique_sources,
)
from vlogsrt.transcribe import TranscriptionJob

# ─── 합성 문서·미디어 빌더 ────────────────────────────────────────────────

FORMAT_30 = (
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>'
)


def _file_uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _asset(xml_id: str, original: Path, *, has_audio: str = "1") -> str:
    return (
        f'<asset id="{xml_id}" name="{xml_id}.mp4" start="0s" duration="600s"'
        f' hasAudio="{has_audio}" format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(original)}"/>\n    </asset>'
    )


def _clips(*refs: str) -> str:
    parts = [
        f'<asset-clip ref="{ref}" name="클립{index}" offset="{index * 5}s"'
        ' start="0s" duration="5s"/>'
        for index, ref in enumerate(refs)
    ]
    return "\n            ".join(parts)


def _write_project(
    tmp_path: Path, assets: list[str], spine_items: str, *, stem: str = "project"
) -> Path:
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
    return path


def _media_file(directory: Path, name: str, payload: bytes) -> Path:
    path = directory / name
    path.write_bytes(payload)
    return path


# ─── 스텁 — 도구 게이트·probe·추출 ───────────────────────────────────────


def _tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")


def _forbidden(name: str):
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name}가 불렸다 — 이 경로에서는 없어야 한다")

    return boom


def _forbid_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "probe_audio_stream", _forbidden("probe_audio_stream"))


def _forbid_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "build_reference", _forbidden("build_reference"))


def _forbid_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "require_ffmpeg", _forbidden("require_ffmpeg"))


def _stream_info() -> preprocess.AudioStreamInfo:
    return preprocess.AudioStreamInfo(
        stream_index=1, start_time=Fraction(0), sample_rate=48000, channels=1
    )


def _probe_ok(monkeypatch: pytest.MonkeyPatch, calls: list[Path] | None = None) -> None:
    def probe(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
        if calls is not None:
            calls.append(source)
        return _stream_info()

    monkeypatch.setattr(runner, "probe_audio_stream", probe)


def _probe_failing_on(monkeypatch: pytest.MonkeyPatch, bad: Path, message: str) -> None:
    def probe(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
        if source == bad:
            raise RuntimeError(message)
        return _stream_info()

    monkeypatch.setattr(runner, "probe_audio_stream", probe)


def _fake_extract(monkeypatch: pytest.MonkeyPatch, pcm_bytes: bytes) -> list[Path]:
    """진짜 `build_reference` 밑에서 추출만 흉내 낸다 — 받은 목적지를 세는 것이 관찰점."""
    asked: list[Path] = []

    def extract(
        source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        asked.append(destination)
        destination.write_bytes(pcm_bytes)
        return preprocess.PcmExtraction(
            path=destination,
            stream=preprocess.AudioStreamInfo(
                stream_index=stream_index + 1,
                start_time=Fraction(0),
                sample_rate=48000,
                channels=1,
            ),
            sample_count=len(pcm_bytes) // 2,
            sample_width=2,
            pcm_sample0_file_time=Fraction(0),
        )

    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", extract)
    return asked


class _NeverTranscriber:
    """불리면 안 되는 전사기 — unavailable만 있으면 유료 호출은 없다."""

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[object]:
        raise AssertionError(f"전사기가 불렸다: {job.kind}")


# ─── 정상 경로 — 쌍·설정·경로 ─────────────────────────────────────────────


def test_an_audio_resource_becomes_a_validated_pcm_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """오디오 있는 리소스는 쌍이 되고, 쌍은 speech 검증을 그대로 통과한다."""
    original = _media_file(tmp_path, "clip.mp4", b"synthetic-original")
    project = _write_project(tmp_path, [_asset("r2", original)], _clips("r2"))
    _tools_present(monkeypatch)
    _probe_ok(monkeypatch)
    extract_calls = _fake_extract(monkeypatch, b"\x01\x00" * 1600)
    policy, config, project_config = AudioPolicy(), Config(), ProjectConfig()

    result = runner.prepare_audio(
        project,
        tmp_path / "artifacts",
        policy=policy,
        config=config,
        project_config=project_config,
    )

    assert result.policy is policy
    assert result.config is config
    assert result.project_config is project_config
    assert len(result.audio_inputs) == 1
    media, second = result.audio_inputs[0]
    assert isinstance(media, ResolvedMedia)
    assert isinstance(second, PcmReference)
    assert second.source_id == media.asset_content_id
    assert second.original_sha256 == media.original_sha256
    assert second.original_stem == media.original.stem
    assert len(extract_calls) == 1
    assert result.artifact_dir == tmp_path / "artifacts"
    assert result.artifact_dir.is_dir()
    assert result.pcm_dir == tmp_path / "artifacts" / "pcm"
    assert result.pcm_dir.is_dir()
    assert result.frames_dir == tmp_path / "artifacts" / "frames"
    assert result.frames_dir.is_dir()
    # PCM은 애셋 네임스페이스(source·boundary)와 갈라진 별도 네임스페이스에 산다
    assert second.path.is_relative_to(result.pcm_dir)
    assert not second.path.is_relative_to(result.pcm_dir.parent / media.asset_content_id)
    _validated_pairs(result.audio_inputs)


def test_overrides_remap_the_original_before_pcm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """명시적 오버라이드는 XML media-rep을 덮어쓴다 — remap 경로가 러너에 살아 있다."""
    xml_original = _media_file(tmp_path, "xml_source.mp4", b"xml-bytes")
    remapped = _media_file(tmp_path, "remapped.mp4", b"remapped-bytes")
    project = _write_project(tmp_path, [_asset("r2", xml_original)], _clips("r2"))
    _tools_present(monkeypatch)
    _probe_ok(monkeypatch)
    _fake_extract(monkeypatch, b"\x01\x00" * 800)

    result = runner.prepare_audio(
        project,
        tmp_path / "artifacts",
        overrides=(MediaOverride("r2", original=remapped),),
    )

    media, second = result.audio_inputs[0]
    assert media.original == remapped
    assert media.original_sha256 == hashlib.sha256(b"remapped-bytes").hexdigest()
    assert isinstance(second, PcmReference)
    assert second.original_sha256 == media.original_sha256


# ─── 세 reason_code — 정해진 자리에서만 난다 ──────────────────────────────


def test_declared_no_audio_is_recorded_without_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hasAudio=0은 리소스 선언 그대로다 — probe도 도구도 부르지 않는다."""
    original = _media_file(tmp_path, "still.png", b"synthetic-still")
    project = _write_project(tmp_path, [_asset("r2", original, has_audio="0")], _clips("r2"))
    _forbid_probe(monkeypatch)
    _forbid_build(monkeypatch)
    # 오디오가 하나도 없으면 도구 게이트조차 필요 없다
    _forbid_tools(monkeypatch)

    result = runner.prepare_audio(project, tmp_path / "artifacts")

    media, second = result.audio_inputs[0]
    assert isinstance(second, UnavailableSourceAudio)
    assert second.status == "no_audio"
    assert second.reason_code == "declared_no_audio"
    assert second.resource_scoped_id == media.resource_scoped_id
    assert second.source_id == media.asset_content_id
    assert second.original_sha256 == media.original_sha256


def test_probe_failure_is_stream_unavailable_with_the_real_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe 실패는 stream_unavailable/error — reason에 원래 메시지가 실린다."""
    original = _media_file(tmp_path, "broken.mp4", b"synthetic-broken")
    project = _write_project(tmp_path, [_asset("r2", original)], _clips("r2"))
    _tools_present(monkeypatch)
    message = "오디오 스트림 a:0이 없습니다: /tmp/broken.mp4 — 범위를 벗어난 선택이다"
    _probe_failing_on(monkeypatch, original, message)
    _forbid_build(monkeypatch)

    result = runner.prepare_audio(project, tmp_path / "artifacts")

    media, second = result.audio_inputs[0]
    assert isinstance(second, UnavailableSourceAudio)
    assert second.status == "error"
    assert second.reason_code == "stream_unavailable"
    assert message in second.reason
    assert second.resource_scoped_id == media.resource_scoped_id


@pytest.mark.parametrize(
    "error",
    [
        ValueError("원본 해시가 주어진 정체성과 다르다"),
        RuntimeError("ashowinfo 기록이 파일 전체를 설명하지 못합니다"),
        OSError("디스크가 찼다"),
    ],
)
def test_reference_failure_is_pcm_unavailable_with_the_real_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """probe는 됐는데 참조가 못 나오면 pcm_unavailable/error — 이름 박은 예외만 잡는다."""
    original = _media_file(tmp_path, "half.mp4", b"synthetic-half")
    project = _write_project(tmp_path, [_asset("r2", original)], _clips("r2"))
    _tools_present(monkeypatch)
    _probe_ok(monkeypatch)

    def build(*args: object, **kwargs: object) -> PcmReference:
        raise error

    monkeypatch.setattr(runner, "build_reference", build)

    result = runner.prepare_audio(project, tmp_path / "artifacts")

    media, second = result.audio_inputs[0]
    assert isinstance(second, UnavailableSourceAudio)
    assert second.status == "error"
    assert second.reason_code == "pcm_unavailable"
    assert str(error) in second.reason
    assert second.source_id == media.asset_content_id


# ─── 규칙 9 — 조용히 사라지는 리소스는 없다 ────────────────────────────────


def test_mixed_project_keeps_every_resource_in_document_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정상 하나 + probe 실패 하나 — 결과 길이는 입력 리소스 수와 같다."""
    good = _media_file(tmp_path, "good.mp4", b"good-bytes")
    bad = _media_file(tmp_path, "bad.mp4", b"bad-bytes")
    project = _write_project(tmp_path, [_asset("r2", good), _asset("r3", bad)], _clips("r2", "r3"))
    _tools_present(monkeypatch)
    _probe_failing_on(monkeypatch, bad, "프로브가 죽었다")
    _fake_extract(monkeypatch, b"\x01\x00" * 800)

    result = runner.prepare_audio(project, tmp_path / "artifacts")

    assert len(result.audio_inputs) == len(result.document.resources) == 2
    good_media, good_second = result.audio_inputs[0]
    bad_media, bad_second = result.audio_inputs[1]
    assert isinstance(good_second, PcmReference)
    assert isinstance(bad_second, UnavailableSourceAudio)
    assert bad_second.resource_scoped_id == bad_media.resource_scoped_id
    assert good_media.resource_scoped_id != bad_media.resource_scoped_id
    # 매핑은 for 루프 없이 자원 신원으로 찾는다
    assert result.inputs_by_resource[bad_media.resource_scoped_id] is result.audio_inputs[1]
    assert result.inputs_by_resource[good_media.resource_scoped_id] is result.audio_inputs[0]


def test_two_ids_over_the_same_bytes_extract_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """서로 다른 xml_id가 같은 바이트를 가리키면 PCM 주소는 하나로 접힌다.

    추출이 정확히 한 번인 것으로 재사용을 증명한다 — 두 번째 호출은 포인터가
    가리키는 세대를 검증하고 되살린다(같은 바이트, 같은 세대). 접지 않으면 추출이
    두 번 일어나 비용·디스크가 두 배가 되고, 세대가 갈라지면 정합성 문제다.
    """
    shared = _media_file(tmp_path, "shared.mp4", b"shared-bytes")
    project = _write_project(
        tmp_path, [_asset("r2", shared), _asset("r3", shared)], _clips("r2", "r3")
    )
    _tools_present(monkeypatch)
    _probe_ok(monkeypatch)
    extract_calls = _fake_extract(monkeypatch, b"\x02\x00" * 800)

    result = runner.prepare_audio(project, tmp_path / "artifacts")

    assert len(result.audio_inputs) == 2  # 리소스 둘은 접히지 않는다
    first_media, first = result.audio_inputs[0]
    second_media, second = result.audio_inputs[1]
    assert isinstance(first, PcmReference)
    assert isinstance(second, PcmReference)
    assert first_media.resource_scoped_id != second_media.resource_scoped_id
    assert first_media.asset_content_id == second_media.asset_content_id
    assert len(extract_calls) == 1  # 추출은 한 번
    assert second is not first
    assert second.path == first.path  # 같은 불변 세대를 가리킨다
    assert first.analysis_key == second.analysis_key
    assert first.path.parent == result.pcm_dir / first_media.asset_content_id


def test_pairs_feed_transcribe_unique_sources_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """러너의 산출물은 소비자 검증을 그대로 통과하고, unavailable엔 호출이 없다."""
    first = _media_file(tmp_path, "one.png", b"one-bytes")
    second_file = _media_file(tmp_path, "two.png", b"two-bytes")
    project = _write_project(
        tmp_path,
        [_asset("r2", first, has_audio="0"), _asset("r3", second_file, has_audio="0")],
        _clips("r2", "r3"),
    )
    _forbid_probe(monkeypatch)
    _forbid_build(monkeypatch)

    result = runner.prepare_audio(project, tmp_path / "artifacts")

    pairs = _validated_pairs(result.audio_inputs)
    assert len(pairs) == 2
    analyses = transcribe_unique_sources(
        result.audio_inputs,
        result.config,
        _NeverTranscriber(),
        tmp_path / "artifacts",
    )
    assert [analysis.status for analysis in analyses] == ["no_audio", "no_audio"]


def test_the_result_refuses_dropped_or_reordered_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """규칙 9를 타입으로 못 깨게 한다 — 빼거나 순서를 바꾸면 그 자리에서 죽는다."""
    good = _media_file(tmp_path, "good.mp4", b"good-bytes")
    bad = _media_file(tmp_path, "bad.mp4", b"bad-bytes")
    project = _write_project(tmp_path, [_asset("r2", good), _asset("r3", bad)], _clips("r2", "r3"))
    _tools_present(monkeypatch)
    _probe_failing_on(monkeypatch, bad, "프로브가 죽었다")
    _fake_extract(monkeypatch, b"\x01\x00" * 800)
    result = runner.prepare_audio(project, tmp_path / "artifacts")

    with pytest.raises(ValueError, match="리소스 수"):
        data_replace(result, audio_inputs=result.audio_inputs[:1])
    with pytest.raises(ValueError, match="순서"):
        data_replace(result, audio_inputs=(result.audio_inputs[1], result.audio_inputs[0]))


# ─── 하드 게이트 — 부재 기록으로 강등되지 않는 것 ─────────────────────────


def test_unresolvable_media_propagates_the_preparation_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """해석 불가능한 미디어는 UnsupportedProject 그대로 올라온다 — 네 번째 코드가 아니다."""
    good = _media_file(tmp_path, "good.mp4", b"good-bytes")
    missing = tmp_path / "nowhere.mp4"
    project = _write_project(
        tmp_path, [_asset("r2", good), _asset("r3", missing)], _clips("r2", "r3")
    )
    _tools_present(monkeypatch)
    _probe_ok(monkeypatch)
    _fake_extract(monkeypatch, b"\x01\x00" * 800)

    with pytest.raises(UnsupportedProject):
        runner.prepare_audio(project, tmp_path / "artifacts")


def test_missing_ffmpeg_is_one_hard_failure_not_absences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """도구가 없으면 환경 오류 하나로 죽는다 — N개의 부재 기록으로 번역하지 않는다."""
    first = _media_file(tmp_path, "one.mp4", b"one-bytes")
    second_file = _media_file(tmp_path, "two.mp4", b"two-bytes")
    project = _write_project(
        tmp_path, [_asset("r2", first), _asset("r3", second_file)], _clips("r2", "r3")
    )

    def missing(tool: str = "ffmpeg") -> str:
        raise RuntimeError(f"{tool} 를 찾지 못했습니다. `brew install ffmpeg` 후 다시 실행하세요.")

    monkeypatch.setattr(runner, "require_ffmpeg", missing)

    with pytest.raises(RuntimeError, match="찾지 못했습니다"):
        runner.prepare_audio(project, tmp_path / "artifacts")
