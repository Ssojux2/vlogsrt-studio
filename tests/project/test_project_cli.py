"""PROTO-5 — `vlogsrt project run` CLI. 비용 경계 · 종료 코드 · 초안 경고 (계약).

여기서 증명하는 것:

- CLI가 보는 것은 `project.commands` 하나다(불변식 18) — 파싱은 cli.py, 계산·
  설정·자격증명·예외 변환은 commands.py다.
- 오프라인은 비전 클라이언트를 만들지 않는다: `vision_client=None`·`online=False`로
  러너를 불러 블록마다 오류 판정 → blockers `['decision_error']` → 종료 코드 1.
- `--offline`과 무플래그는 `force_offline=True`로 전사기를 만든다 — 셸의
  `VLOGSRT_OFFLINE=0`이 유료 전사로 샌다는 구멍(transcribe.py:399)을 CLI 쪽에서
  막는다. 레거시 `run`이 같은 선을 그린다(cli.py:185).
- `--online`은 키 값과 base URL 값이 **둘 다** 있어야 파이프라인을 시작한다 —
  base URL이 비면 `create_vision_client`가 공개 엔드포인트로 가서 Google 키가
  다른 공급자로 전송된다(transcribe.py:438). 오류 문구에는 이름만 있고 값은 없다.
- 비전 자격증명은 `vlogsrt.project.toml`이 지정한 환경변수 이름에서만 온다 —
  `env.require_api_key`(STT)를 부르지 않는다.
- `--online --offline`은 argparse 상호 배타 그룹이 종료 코드 2로 거절한다.
- 파이프라인 예외는 2다(1이 아니다) — 막힌 패키지(1)와 실패(2)가 갈린다.
- 성공은 "승인"이 아니라 "차단 없음"이고, 컷이 있으면 사람 검토 없음(초안)이
  눈에 띄게 붙는다(D35 — `EditPlan`에 사람 검토 상태가 없다).
- `-o OUT`은 artifacts·package **형제** 배치다(§4-a) — 두 번째 실행이 관리 뿌리
  검사(`_require_managed_root`)에서 죽지 않는다.

유료 호출 0회: 전사기·비전 클라이언트·ffmpeg·VAD 세션은 전부 스텁이다. 저장소
뿌리에 `vlogsrt.project.toml`이 실재하므로 cwd 의존를 끊는다 — 항상 명시적
`--project-config`(tmp)를 준다. `tests/fixtures/**`도 `test_runner_publish.py`도
수정하지 않는다 — 재료는 복제했다.
"""

from __future__ import annotations

import array
import hashlib
import json
import re
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
from vlogsrt.project import runner
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


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 문서 · 프로젝트 설정 · 미디어
#  (test_runner_audio.py·test_runner_publish.py의 것을 이 파일용으로 복제했다)
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


# ─────────────────────────────────────────────────────────────
#  스텁 — 전사기 · VAD 세션 · ffmpeg 계층 · 프레임 경계 · 비전 클라이언트
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


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg·VAD·프레임 경계를 스텁으로 갈아끼운다 — 전사기·비전은 각 시험이 정한다."""
    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    monkeypatch.setattr(runner, "probe_audio_stream", _probe_stream)
    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", _extract_pattern_pcm)
    monkeypatch.setattr(runner, "bundled_artifact", lambda cfg: Path("/fake/silero.onnx"))
    monkeypatch.setattr(runner, "create_cpu_session", lambda artifact: _ConstantSession(0.1))
    _fake_frames(monkeypatch)


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
#  1 · 9 — 오프라인 전체 흐름: 차단된 초안, 형제 배치
# ─────────────────────────────────────────────────────────────


def test_offline_run_publishes_a_blocked_draft_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """오프라인은 비전 클라이언트를 만들지 않고 끝까지 흘러 차단된 초안을 낸다."""
    project = _write_project(tmp_path, _media_file(tmp_path, "clip.mp4", b"synthetic-original"))
    project_config = _write_project_config(tmp_path)
    out = tmp_path / "out"
    _patch_pipeline(monkeypatch)
    _stub_transcriber(monkeypatch)
    monkeypatch.setattr(transcribe, "create_vision_client", _fail_if_called("create_vision_client"))

    code = cli.main(_argv(project, out, project_config))

    assert code == 1
    review = json.loads((out / "package" / "current" / "review.json").read_text(encoding="utf-8"))
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]
    assert (out / "package" / "current").is_symlink()  # 차단된 패키지지 패키지 없음이 아니다
    assert (out / "artifacts").is_dir()


def test_a_second_run_into_the_same_out_passes_the_managed_root_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-o OUT`의 산출은 형제 배치다 — 두 번째 실행이 관리 뿌리 검사에서 죽지 않는다."""
    project = _write_project(tmp_path, _media_file(tmp_path, "clip.mp4", b"synthetic-original"))
    project_config = _write_project_config(tmp_path)
    out = tmp_path / "out"
    _patch_pipeline(monkeypatch)
    _stub_transcriber(monkeypatch)

    assert cli.main(_argv(project, out, project_config)) == 1
    second = cli.main(_argv(project, out, project_config))  # 같은 명령을 그대로 두 번째
    captured = capsys.readouterr()

    # destination을 OUT 자체로 두면 artifacts가 관리 뿌리 안에 들어가 이 검사에 걸린다.
    # 형제 배치면 통과한다 — 같은 리비전의 재게시는 배타 생성에서 실패하는데(export),
    # 그건 관리 뿌리 검사와는 다른, 의도된 닫힘이다.
    assert "관리 밖 내용" not in captured.err
    # 재게시의 실제 모습을 버리지 않고 못 박는다: 같은 입력·같은 설정은 같은 계획
    # 리비전을 내고 `_create_revision`의 배타 mkdir이 ValueError로 죽는다 — 종료 2.
    assert second == 2
    assert "리비전 디렉터리가 이미 있다" in captured.err
    assert (out / "package" / "current").is_symlink()
    assert (out / "artifacts").is_dir()


# ─────────────────────────────────────────────────────────────
#  2 — `--offline`·무플래그: force_offline이 유료 전사로 새는 구멍을 막는다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", [["--offline"], []], ids=["offline-flag", "default"])
def test_offline_and_default_force_the_fixture_transcriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: list[str]
) -> None:
    """`VLOGSRT_OFFLINE=0`인 셸에서도 `--offline`과 무플래그는 픽스처 전사기다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)  # 몽키패치로만 — 환경은 안 고친다
    # 미디어가 없어 파이프라인은 시작 못 한다 — 전사기 선택은 그 앞에서 증명된다.
    project = _write_project(tmp_path, tmp_path / "nowhere.mp4")
    calls = _stub_transcriber(monkeypatch)

    code = cli.main(_argv(project, tmp_path / "out", _write_project_config(tmp_path), extra=flag))

    assert code == 2  # 전사기 선택은 prepare_audio 앞에서 증명된 뒤에 죽는다
    assert calls == [{"online": False, "force_offline": True}]


# ─────────────────────────────────────────────────────────────
#  3 · 4 · 5 — `--online` 자격증명: 파이프라인 시작 전의 문
# ─────────────────────────────────────────────────────────────


def test_online_with_an_empty_vision_key_stops_before_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """키 값이 비면 시작 전에 2 — 문구에는 이름이 있고 값은 없다."""
    project = _write_project(tmp_path, tmp_path / "nowhere.mp4")
    _stub_transcriber(monkeypatch)
    monkeypatch.setattr(runner, "prepare_audio", _fail_if_called("prepare_audio"))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    code = cli.main(
        _argv(project, tmp_path / "out", _write_project_config(tmp_path), extra=["--online"])
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "GOOGLE_API_KEY" in captured.err


def test_online_with_an_empty_base_url_stops_before_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """base URL 값이 비면 클라이언트를 만들지 않고 2 — 공개 엔드포인트로 키가 샌다."""
    project = _write_project(tmp_path, tmp_path / "nowhere.mp4")
    _stub_transcriber(monkeypatch)
    monkeypatch.setattr(runner, "prepare_audio", _fail_if_called("prepare_audio"))
    monkeypatch.setattr(transcribe, "create_vision_client", _fail_if_called("create_vision_client"))
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-vision-key-value")
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)

    code = cli.main(
        _argv(project, tmp_path / "out", _write_project_config(tmp_path), extra=["--online"])
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "GEMINI_BASE_URL" in captured.err
    assert "fake-vision-key-value" not in captured.out + captured.err  # 값은 어디에도 없다


def test_vision_credentials_come_from_the_project_config_env_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """비전 자격증명은 설정이 지정한 이름에서 온다 — STT의 require_api_key를 모른다."""
    project = _write_project(tmp_path, tmp_path / "nowhere.mp4")
    project_config = tmp_path / "custom.project.toml"
    project_config.write_text(
        "schema_version = 1\n"
        'vision_api_key_env = "MY_VISION_KEY"\n'
        'vision_base_url_env = "MY_VISION_URL"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_VISION_KEY", "my-vision-key-value")
    monkeypatch.setenv("MY_VISION_URL", "https://vision.example/v1")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.setattr("vlogsrt.env.require_api_key", _fail_if_called("env.require_api_key"))
    _stub_transcriber(monkeypatch)
    client_calls: list[dict[str, str]] = []

    def factory(*, api_key: str, base_url: str) -> _FakeVisionClient:
        client_calls.append({"api_key": api_key, "base_url": base_url})
        return _FakeVisionClient(_accept_front)

    monkeypatch.setattr(transcribe, "create_vision_client", factory)

    code = cli.main(_argv(project, tmp_path / "out", project_config, extra=["--online"]))
    captured = capsys.readouterr()

    # 미디어가 없어 파이프라인은 UnsupportedProject로 2에 닫힌다 — 자격증명은 그 앞에서 증명된다.
    assert code == 2
    assert client_calls == [
        {"api_key": "my-vision-key-value", "base_url": "https://vision.example/v1"}
    ]
    assert "my-vision-key-value" not in captured.out + captured.err


# ─────────────────────────────────────────────────────────────
#  6 · 7 — 플래그 충돌과 파이프라인 예외의 종료 코드
# ─────────────────────────────────────────────────────────────


def test_online_and_offline_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_config = _write_project_config(tmp_path)
    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "project",
                "run",
                str(tmp_path / "project.fcpxml"),
                "-o",
                str(tmp_path / "out"),
                "--project-config",
                str(project_config),
                "--online",
                "--offline",
            ]
        )
    captured = capsys.readouterr()

    assert caught.value.code == 2
    assert "invalid choice" not in captured.err  # 'project'를 모르는 게 아니라 충돌이다
    assert "not allowed with" in captured.err


def test_a_pipeline_exception_exits_2_not_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미디어 없음(UnsupportedProject)은 2다 — 막힌 패키지(1)와 갈린다."""
    project = _write_project(tmp_path, tmp_path / "nowhere.mp4")
    _stub_transcriber(monkeypatch)

    code = cli.main(_argv(project, tmp_path / "out", _write_project_config(tmp_path)))

    assert code == 2
    assert (tmp_path / "out" / "package").exists() is False


# ─────────────────────────────────────────────────────────────
#  8 — 성공 경로: 초안 경고 · 차단 없음 · 리비전 · DTD
# ─────────────────────────────────────────────────────────────


def test_accepted_online_run_reports_the_draft_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """전부 accepted여도 출력은 초안이다 — `final_accepted`는 승인이 아니라 차단 부재다."""
    project = _write_project(tmp_path, _media_file(tmp_path, "clip.mp4", b"synthetic-original"))
    project_config = _write_project_config(tmp_path)
    out = tmp_path / "out"
    _patch_pipeline(monkeypatch)
    _stub_transcriber(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-vision-key-value")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://vision.example/v1")
    monkeypatch.setattr(
        transcribe,
        "create_vision_client",
        lambda *, api_key, base_url: _FakeVisionClient(_accept_front),
    )

    code = cli.main(_argv(project, out, project_config, extra=["--online"]))
    captured = capsys.readouterr()
    squashed = _squash(captured.out + captured.err)

    assert code == 0
    review = json.loads((out / "package" / "current" / "review.json").read_text(encoding="utf-8"))
    assert review["final_accepted"] is True
    assert review["final_blockers"] == []
    assert "자동컷" in squashed and "사람검토없음(초안)" in squashed
    assert "차단없음" in squashed
    assert "승인" not in squashed  # final_accepted를 승인으로 번역하지 않는다
    assert "DTD대조안함" in squashed
    revision_dir = (out / "package" / "current").resolve()
    assert _squash(str(revision_dir)) in squashed  # 리비전 경로가 출력에 있다
