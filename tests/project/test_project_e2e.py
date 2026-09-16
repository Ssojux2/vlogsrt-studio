"""PROTO-6 — 전 구간 e2e. 내부 단계는 진짜, 바깥 경계만 가짜.

`cli.main(["project", "run", ...])` 한 번으로 parse → media → PCM → STT → 자막 →
경계 → VAD → 가드 → 분할 → 표본 → 비전 → 계획 → EDIT 자막 → 패키지가 다 돈다.
러너 셋(`prepare_audio`·`analyze_audio`·`publish_project`)과 `commands`·`cli`는
갈아끼우지 않는다 — 가짜로 바꾸는 것은 경계뿐이다:

- ffmpeg/ffprobe 경계 — 도구 게이트·오디오 probe·PCM 추출(`test_runner_audio.py`
  의 기법 복제)과 프레임 probe·추출·해시(`test_runner_publish.py`의 기법 복제)
- 전사기 — `transcribe.get_transcriber`가 돌려주는 객체(스크립트된 가짜)
- 비전 클라이언트 생성 — `transcribe.create_vision_client`(가짜 클라이언트)
- ONNX 세션 — 기존 가짜 세션(결정적). 진짜 아티팩트는 onnxruntime extra가 없는
  환경에서 pytest를 깨뜨리므로 이 자리에 올리지 않는다 — 실물은 로컬 스모크가
  밟는다(docs/orchestration/progress.md의 PROTO-6 절).

xmllint는 진짜다 — 게시 자체가 실제 xmllint에 걸리고(`export._write_revision`),
이 파일도 `xml_validation.validate_xml`로 **디스크의 바이트**를 다시 본다.

유료 호출 0회를 몸으로 못 박는다: `openai.OpenAI` 생성 자체에 가드를 심는다.
가드가 살아 있다는 것은 전사기·비전 클라이언트 경계 밖에서 SDK가 만들어지지
않았다는 뜻이다. 시나리오가 `--online`을 흉내 내도 실제 클라이언트는 없다.
"""

from __future__ import annotations

import array
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from vlogsrt import cli, preprocess, transcribe
from vlogsrt.config import Config
from vlogsrt.project import pcm as pcm_module
from vlogsrt.project import runner, xml_validation
from vlogsrt.project.export import (
    EDIT_SRT_NAME,
    FCPXML_NAME,
    REVIEW_NAME,
    SOURCE_SRT_NAME,
    TRACE_NAME,
)
from vlogsrt.project.timebase import parse_fcpx_time
from vlogsrt.project.vision import sampling
from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, TranscriptionJob

RATE = 16000
"""합성 PCM의 샘플 레이트 — 정책(AudioPolicy)과 같은 값이라야 참조가 성립한다."""

CLIP_SECONDS = 24
"""클립 길이 — DEFAULT_SEGMENTS의 보호 여집합이 block_min(6s) 넘는 블록 둘을 낸다."""

FD30 = Fraction(1, 30)
"""합성 문서의 격자 — 가짜 비디오 프로브가 같은 값을 말해야 매핑이 정수가 된다."""

DEFAULT_SEGMENTS = (
    (0.5, 1.0, "첫 장면 인사"),
    (10.5, 11.0, "골목 소개"),
    (12.5, 13.0, "두 번째 클립"),
    (22.0, 22.5, "마무리"),
)
"""깨끗한 1차 전사 대본 — 되풀이가 없어 재전사(rescue) 호출은 없다."""

PACKAGE_FILES = frozenset({FCPXML_NAME, EDIT_SRT_NAME, SOURCE_SRT_NAME, TRACE_NAME, REVIEW_NAME})
"""한 리비전의 다섯 파일 — 이외의 것이 섞이면 묶음이 아니라 유출이다."""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 문서 · 프로젝트 설정 (test_project_cli.py에서 복제)
# ─────────────────────────────────────────────────────────────


def _file_uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _media_file(directory: Path, name: str, payload: bytes) -> Path:
    path = directory / name
    path.write_bytes(payload)
    return path


def _write_project(tmp_path: Path, media: Path, *, stem: str = "project") -> Path:
    """24초 클립 하나의 합성 문서 — 블록이 둘 나오는 재료(판정 재료와 같은 대본)."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip.mp4" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(media)}"/>\n    </asset>\n'
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s"'
        ' duration="24s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    path = tmp_path / f"{stem}.fcpxml"
    path.write_text(body, encoding="utf-8")
    return path


def _write_project_config(directory: Path) -> Path:
    """cwd 의존를 끊는다 — 저장소 뿌리의 vlogsrt.project.toml을 절대 안 본다."""
    path = directory / "test.project.toml"
    path.write_text("schema_version = 1\n", encoding="utf-8")
    return path


def _argv(
    project: Path, out: Path, project_config: Path, *, extra: list[str] | None = None
) -> list[str]:
    return [
        "project",
        "run",
        str(project),
        "-o",
        str(out),
        "--project-config",
        str(project_config),
        *(extra or []),
    ]


def _squash(text: str) -> str:
    """rich의 줄바꿈을 지운다 — 문구 단언은 접힘 위치에 무관해야 한다."""
    return re.sub(r"\s+", "", text)


def _sequence_duration(fcpxml: Path) -> Fraction:
    """내보낸 문서의 시퀀스 길이 — 출력 전체를 덮는 유일한 시각 속성이다."""
    sequence = ET.parse(fcpxml).getroot().find("library/event/project/sequence")
    assert sequence is not None, "출력 문서에 sequence가 없다"
    duration = sequence.get("duration")
    assert duration is not None, "출력 sequence에 duration이 없다"
    return parse_fcpx_time(duration)


# ─────────────────────────────────────────────────────────────
#  스텁 — 경계만 가짜 (러너 · commands · cli · export · xmllint는 진짜)
# ─────────────────────────────────────────────────────────────


def _fail_if_called(name: str) -> Callable[..., None]:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name}가 불렸다 — 이 경로에서는 없어야 한다")

    return boom


def _seg(start: float, end: float, text: str) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text}


@dataclass
class _Scripted:
    """가짜 전사기 — 잡을 그대로 기록하고 깨끗한 오디오엔 재전사를 예약하지 않는다."""

    whisper: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None
    calls: list[TranscriptionJob] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.whisper is None:
            self.whisper = self._default_whisper

    @staticmethod
    def _default_whisper(job: TranscriptionJob) -> list[ChunkResponse]:
        payload = {"segments": [_seg(*row) for row in DEFAULT_SEGMENTS]}
        return [
            ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
            for chunk in job.chunks
        ]

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls.append(job)
        if job.kind != FIXTURE_KIND:
            raise RuntimeError(f"예약하지 않은 {job.kind} 호출이다")
        assert self.whisper is not None
        return list(self.whisper(job))


def _stub_transcriber(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, bool]]:
    """`get_transcriber`를 기록하는 스텁으로 갈아끼운다 — kwargs가 곧 계약이다."""
    calls: list[dict[str, bool]] = []

    def factory(
        cfg: Config | None = None, *, online: bool = False, force_offline: bool = False
    ) -> _Scripted:
        calls.append({"online": online, "force_offline": force_offline})
        return _Scripted()

    monkeypatch.setattr(transcribe, "get_transcriber", factory)
    return calls


class _FakeArray:
    """최소 ndarray — 모양·타입·tolist만 흉내낸다(세션 출력 계약에 충분하다)."""

    __slots__ = ("data", "shape", "dtype")

    def __init__(self, data: object, dtype: str) -> None:
        self.data = data
        self.dtype = dtype
        dims: list[int] = []
        level = data
        while isinstance(level, list):
            dims.append(len(level))
            level = level[0] if level else None
        self.shape = tuple(dims)

    def __getitem__(self, index: int) -> object:
        return self.data[index]  # type: ignore[index]

    def tolist(self) -> object:
        return self.data


class _ConstantSession:
    """모든 창에 같은 확률로 답하는 가짜 세션 — 상태는 0을 유지한다."""

    def __init__(self, probability: float) -> None:
        self.probability = probability

    def run(self, output_names: object, feeds: dict[str, object]) -> list[_FakeArray]:
        return [
            _FakeArray([[self.probability]], "float32"),
            _FakeArray([[[0.0] * 128], [[0.0] * 128]], "float32"),
        ]


def _pattern_pcm(frames: int) -> bytes:
    """프레임마다 다른 값 — 남의 프레임을 실어 오면 바로 드러난다."""
    values = array.array("h", ((frame * 3) % 28001 - 14000 for frame in range(frames)))
    return values.tobytes()


def _probe_stream(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
    return preprocess.AudioStreamInfo(
        stream_index=1, start_time=Fraction(0), sample_rate=48000, channels=1
    )


def _extract_pattern_pcm(
    source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
) -> preprocess.PcmExtraction:
    """진짜 `build_reference` 밑에서 추출만 흉내 낸다 — 참조 칸은 참조 코드가 계산한다."""
    pcm = _pattern_pcm(CLIP_SECONDS * RATE)
    destination.write_bytes(pcm)
    return preprocess.PcmExtraction(
        path=destination,
        stream=preprocess.AudioStreamInfo(
            stream_index=stream_index + 1,
            start_time=Fraction(0),
            sample_rate=48000,
            channels=1,
        ),
        sample_count=len(pcm) // 2,
        sample_width=2,
        pcm_sample0_file_time=Fraction(0),
    )


def _fake_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """프레임 probe/extract/해시를 흉내 낸다 — 추출 바이트는 실제로 쓴다."""

    def probe(source: Path, stream_index: int = 0) -> preprocess.VideoStreamInfo:
        return preprocess.VideoStreamInfo(
            stream_index=0,
            presentation_origin=Fraction(0),
            frame_duration=FD30,
            frame_count=720,
        )

    def extract(
        source: Path,
        stream_index: int,
        frame_index: int,
        destination: Path,
        *,
        long_edge: int,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"frame:{frame_index}".encode())
        return destination

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(sampling, "probe_video_stream", probe)
    monkeypatch.setattr(sampling, "extract_frame_index", extract)
    monkeypatch.setattr(sampling, "sha256_file", digest)


def _patch_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """가짜는 경계뿐이다 — ffmpeg·PCM·프레임·ONNX 세션. 러너 셋은 건드리지 않는다."""
    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    monkeypatch.setattr(runner, "probe_audio_stream", _probe_stream)
    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", _extract_pattern_pcm)
    monkeypatch.setattr(runner, "bundled_artifact", lambda cfg: Path("/fake/silero.onnx"))
    monkeypatch.setattr(runner, "create_cpu_session", lambda artifact: _ConstantSession(0.1))
    _fake_frames(monkeypatch)


def _forbid_real_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """진짜 openai 클라이언트 생성을 못 박는다 — 살아남으면 유료 경로가 열린 것이다."""
    import openai

    monkeypatch.setattr(openai, "OpenAI", _fail_if_called("openai.OpenAI"))


class _FakeCompletions:
    def __init__(self, respond: Callable[[str, list[dict]], dict], calls: list) -> None:
        self._respond = respond
        self._calls = calls

    def create(self, *, model: str, messages: list[dict]) -> dict:
        self._calls.append((model, messages))
        return self._respond(model, messages)


class _FakeChat:
    def __init__(self, respond: Callable[[str, list[dict]], dict], calls: list) -> None:
        self.completions = _FakeCompletions(respond, calls)


class _FakeVisionClient:
    """가짜 비전 클라이언트 — `request_vision`이 부르는 모양만 흉내낸다."""

    def __init__(self, respond: Callable[[str, list[dict]], dict]) -> None:
        self.calls: list[tuple[str, list[dict]]] = []
        self.chat = _FakeChat(respond, self.calls)


def _front_candidate(messages: list[dict]) -> str:
    """안내 문장의 첫 후보 아이디 — 모델이 안내를 읽고 앞 후보를 고르는 흉내."""
    text = messages[0]["content"][0]["text"]
    for line in text.splitlines():
        if line.startswith("후보(블록 앞쪽부터): "):
            return line.split("후보(블록 앞쪽부터): ", 1)[1].split(",")[0].strip()
    raise AssertionError("안내 문장에서 후보 목록을 찾지 못했다")


def _accept_front(model: str, messages: list[dict]) -> dict:
    """모든 블록을 accepted로 답하는 가짜 응답 — 초안 경로를 끝까지 흘린다."""
    content = json.dumps(
        {
            "choice": _front_candidate(messages),
            "reason": "같은 내용의 단순 되풀이 B-roll이다",
            "reason_kind": "repetitive_ordinary_broll",
            "evidence_frame_ids": [],
            "confidence": 0.9,
        },
        ensure_ascii=False,
    )
    return {"choices": [{"message": {"content": content}}]}


# ─────────────────────────────────────────────────────────────
#  1 — 온라인 경로 흉내: 여든 단계가 다 돌고 시퀀스가 실제로 짧아진다
# ─────────────────────────────────────────────────────────────


def test_online_mimic_runs_every_stage_and_shortens_the_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--online`의 온라인 판정이 전부 accepted면 0 — 패키지 다섯 파일·짧아진 출력."""
    project = _write_project(tmp_path, _media_file(tmp_path, "clip.mp4", b"synthetic-original"))
    project_config = _write_project_config(tmp_path)
    out = tmp_path / "out"
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    _forbid_real_openai(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-vision-key-value")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://vision.example/v1")
    monkeypatch.setattr(
        transcribe,
        "create_vision_client",
        lambda *, api_key, base_url: _FakeVisionClient(_accept_front),
    )

    code = cli.main(_argv(project, out, project_config, extra=["--online"]))
    captured = capsys.readouterr()

    assert code == 0
    # current가 가리키는 리비전에 다섯 파일 — 정확히 다섯, 하나도 더덜지 않게.
    pointer = out / "package" / "current"
    assert pointer.is_symlink()  # 차단 없음도 심링크 뒤 한 리비전으로만 나온다
    revision = pointer.resolve()
    assert revision.parent.name == "revisions"
    assert {entry.name for entry in revision.iterdir()} == set(PACKAGE_FILES)
    # 디스크의 바이트가 실제 xmllint를 통과한다 — 게시 관문과 같은 경로로 다시 본다.
    validation = xml_validation.validate_xml(revision / FCPXML_NAME)
    assert validation.valid, validation.diagnostics
    # 출력 시퀀스가 실제로 짧아졌다 — 컷이 문서에 남았다는 기하의 증거다.
    output_duration = _sequence_duration(revision / FCPXML_NAME)
    assert Fraction(0) < output_duration < Fraction(CLIP_SECONDS)
    # 초안 경고 — accepted는 승인이 아니라 차단 부재다.
    squashed = _squash(captured.out + captured.err)
    assert "자동컷" in squashed and "사람검토없음(초안)" in squashed


# ─────────────────────────────────────────────────────────────
#  2 — 오프라인(무플래그): 비전 생성자 0회 · decision_error 차단 · 종료 1
# ─────────────────────────────────────────────────────────────


def test_offline_blocks_on_decision_error_without_a_vision_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """무플래그는 비전 클라이언트를 한 번도 만들지 않고 막힌 초안으로 끝난다."""
    project = _write_project(tmp_path, _media_file(tmp_path, "clip.mp4", b"synthetic-original"))
    project_config = _write_project_config(tmp_path)
    out = tmp_path / "out"
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    _forbid_real_openai(monkeypatch)
    vision_calls: list[dict[str, str]] = []

    def factory(*, api_key: str, base_url: str) -> _FakeVisionClient:
        vision_calls.append({"api_key": api_key, "base_url": base_url})
        return _FakeVisionClient(_accept_front)

    monkeypatch.setattr(transcribe, "create_vision_client", factory)

    code = cli.main(_argv(project, out, project_config))

    assert code == 1
    review = json.loads((out / "package" / "current" / "review.json").read_text(encoding="utf-8"))
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]
    assert vision_calls == []  # 비전 생성자 0회 — off라서가 아니라 만들 이유가 없다
    assert (out / "package" / "current").is_symlink()  # 막힌 패키지지 패키지 없음이 아니다
    assert (out / "artifacts").is_dir()
