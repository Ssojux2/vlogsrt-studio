"""W-29a — 1단계 whisper 요청에 단수 `language`를 싣는다(D77).

실데이터 34개 중 5개에서 whisper가 언어 자동 감지로 일본어·영어·칸나다 끝인사를 지어냈다 — 작업대
whisper 요청에는 `language`가 없었고(레거시 CLI `transcribe._extras`는 `cfg.language`를 보낸다).
유료 호출은 없다. 시험이 못 박는 것:

- 시간 요청에 `language`는 있고 `prompt`·`keywords`·복수형 `languages`는 없다(규칙 6 그대로).
- 값은 자막 계약의 예상 언어 첫째(기본 "ko")이고, 비었으면 칸째 빠진다.
- 전송 경계가 그 칸을 싣고 모양을 검사한다(ISO 639-1 두 글자 소문자).
- 기능 등록표는 시간 역할에 단수 `language`를 확인하고 복수형은 여전히 미확인이다.
- 청크 캐시 열쇠·1단계 승인 지문은 그대로다(이미 산 전사를 다시 사지 않는다).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.caption_provider import timing_transcription_request
from vlogsrt.project.raw.config import RawWorkflowConfig, caption_problems
from vlogsrt.transcribe import (
    AudioTranscriptionRequest,
    _transport_fields,
    transport_request_problems,
)

BEFORE_SHUTDOWN = date(2026, 10, 2)


def chunk(tmp_path: Path) -> Chunk:
    wav = tmp_path / "chunk.wav"
    wav.write_bytes(b"wav")
    return Chunk(path=wav, offset=0.0, duration=5.0, index=0)


def test_the_timing_request_carries_language_and_no_hints(tmp_path: Path) -> None:
    request = timing_transcription_request(
        chunk(tmp_path), settings=RawWorkflowConfig(), temperature=0.6
    )
    assert request.language == "ko"
    assert request.prompt == "" and request.keywords == () and request.languages == ()
    required, extras = _transport_fields(request)
    assert extras["language"] == "ko"
    assert not {"prompt", "keywords", "languages"} & set(extras)
    assert required == {"model": "whisper-1", "response_format": "verbose_json"}


def test_an_empty_expected_language_leaves_the_field_out(tmp_path: Path) -> None:
    config = RawWorkflowConfig()
    settings = replace(config, caption=replace(config.caption, languages=()))
    request = timing_transcription_request(chunk(tmp_path), settings=settings)
    assert request.language == ""
    assert "language" not in _transport_fields(request)[1]


@pytest.mark.parametrize("value", ["korean", "KO", "k", "ko\n", "ko-KR"])
def test_the_transport_refuses_a_language_that_is_not_two_lowercase_letters(
    tmp_path: Path, value: str
) -> None:
    request = AudioTranscriptionRequest(
        chunk=chunk(tmp_path), model="whisper-1", response_format="verbose_json", language=value
    )
    assert any("언어" in problem for problem in transport_request_problems(request))


def test_the_registry_confirms_singular_language_for_timing_only() -> None:
    text, timing = capabilities.confirmed_capability_snapshots()
    assert timing.supports(capabilities.FEATURE_LANGUAGE)
    assert not timing.supports(capabilities.FEATURE_LANGUAGES)
    assert not text.supports(capabilities.FEATURE_LANGUAGE)


def test_the_approval_fingerprint_and_chunk_key_ignore_the_request_only_language(
    tmp_path: Path,
) -> None:
    """승인 지문·청크 열쇠는 설정 해시이고 요청에만 실리는 칸은 거기 없다 — 고정 값 그대로다."""
    from tests.raw.test_timing_fixes import (
        ASR_APPROVAL_FP_BEFORE_FIX,
        ASR_CACHE_KEY_BEFORE_FIX,
        RecordingSender,
        deterministic_audio,
    )
    from vlogsrt.config import Config
    from vlogsrt.project.raw import understanding as stage1
    from vlogsrt.project.raw.analysis_approval import asr_settings_fingerprint
    from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
    from vlogsrt.project.raw.caption_provider import TransportTimingProvider

    fingerprint = asr_settings_fingerprint(RawUnderstandingSettings(), RawWorkflowConfig())
    assert fingerprint == ASR_APPROVAL_FP_BEFORE_FIX
    sender = RecordingSender()
    cache = tmp_path / "cache"
    stage1.transcribe_selected_audio(
        deterministic_audio(tmp_path),
        (stage1.SampleSpan(0, 160000),),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=sender, cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        cache_dir=cache,
    )
    assert [path.stem for path in cache.glob("*.json")] == [ASR_CACHE_KEY_BEFORE_FIX]
    (request,) = sender.requests  # 전송 제공자를 지나 보낸 요청에 language가 실렸다
    assert request.language == "ko" and _transport_fields(request)[1]["language"] == "ko"


@pytest.mark.parametrize("bad", [("Korean",), ("ko-KR",), ("KO",)])
def test_a_contract_whose_first_language_is_not_iso_639_1_is_refused_at_load(
    bad: tuple[str, ...],
) -> None:
    contract = replace(RawWorkflowConfig().caption, languages=bad)
    assert any("whisper 언어 칸" in problem for problem in caption_problems(contract))
