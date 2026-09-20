"""PROTO-4 러너 3/3 — 블록 · 프레임 표본 · 비전 · EditPlan · EDIT 자막 · 패키지 (계약).

여기서 증명하는 것:

- `publish_project`가 `AnalyzedAudio`를 패키지로 출판한다 — 분할 → 블록마다 표본·
  페이로드·비전 판정 → 계획 → 검증 → EDIT 자막 → 다섯 파일 한 리비전.
- 비전 호출은 `transcribe.request_vision` **이름으로**만 나간다. `online=False`로
  부르면 그 게이트가 raise하고 판정이 전부 error가 된다 — 러너가 게이트를 우회해
  클라이언트를 직접 부르는 길은 이 테스트가 지나갈 수 없다(§2).
- 실패가 "판정 불필요"로 세탁되지 않는다 — 표본 실패·전송 실패(429 모양)·페이로드
  실패(샘플 해시 불일치)는 그 블록의 `status="error"` 판정이 되고 블록은 목록에서
  빠지지 않는다. PROTO-1의 난간이 실제로 문다: `decision_error`가 final_accepted를
  막는다(§3).
- 판정 정렬의 세 단언(개수·중복·순서·신원)이 `build_edit_plan` 앞과
  `PublishedProject.__post_init__` 양쪽에 선다(§4-c).
- 같은 `asset_content_id`에 `ResolvedMedia`가 둘인 문서는 문서 순서 첫 항목이
  쓰인다 — 프록시 연결이 다를 수 있으므로 선택이 결과를 바꾼다(§4-a).

합성 문서·손 참조·스크립트된 전사기·가짜 VAD 세션·가짜 비전 클라이언트만 쓴다.
프레임 probe/extract는 `vlogsrt.project.vision.sampling` 이름공간에서 갈아끼운다 —
가짜 추출은 실제 바이트를 쓴다(`build_payload`가 샘플 해시를 대조하므로). 유료
호출도 `tests/fixtures/**` 수정도 없다.
"""

from __future__ import annotations

import array
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as data_replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.preprocess import VideoStreamInfo
from vlogsrt.project import runner
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.pcm import AudioPolicy, PcmReference
from vlogsrt.project.runner import PreparedAudio
from vlogsrt.project.speech import UnavailableSourceAudio
from vlogsrt.project.vision import sampling
from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, TranscriptionJob

RATE = 16000
DEFAULT_FRAMES = 24 * RATE
"""기본 손 참조의 길이 — 24초. 보호 여집합이 블록 둘로 나온다."""

FD30 = Fraction(1, 30)
"""합성 문서의 격자 — 가짜 비디오 프로브가 같은 값을 말해야 매핑이 정수가 된다."""

FORMAT_30 = (
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>'
)

DEFAULT_SEGMENTS = (
    (0.5, 1.0, "첫 장면 인사"),
    (10.5, 11.0, "골목 소개"),
    (12.5, 13.0, "두 번째 클립"),
    (22.0, 22.5, "마무리"),
)
"""깨끗한 1차 전사 대본 — 되풀이가 없어 재전사(rescue) 호출은 없다.

보호(세그먼트 ±0.5초)를 제외하면 [1.5s, 10s)와 [13.5s, 21.5s)가 남고 둘 다
block_min(6s) 이상이라 블록이 둘 나온다 — 오류가 한 블록에만 붙는 시험의 재료.
"""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 문서 · 손 미디어 · 손 참조 · 부재 기록
#  (test_runner_analysis.py의 자를 이 파일용으로 복제했다 — 공유로 뽑는 길은 없다)
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AssetSpec:
    """애셋 하나 — kind가 둘째 칸(참조|부재 기록)을 정한다."""

    xml_id: str
    kind: str = "pcm"  # pcm | declared
    content: str | None = None  # 같은 바이트 시험 — 내용 신원을 직접 찍는다
    frames: int = DEFAULT_FRAMES


@dataclass(frozen=True)
class _ClipSpec:
    """칼날 하나 — 시각은 전부 초 문자열 그대로 XML에 간다."""

    ref: str
    offset: str = "0s"
    start: str = "0s"
    duration: str = "12s"


def _asset_xml(spec: _AssetSpec) -> str:
    has_audio = "0" if spec.kind == "declared" else "1"
    return (
        f'<asset id="{spec.xml_id}" name="{spec.xml_id}.mp4" start="0s" duration="600s"'
        f' hasAudio="{has_audio}" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file:///media/{spec.xml_id}.mp4"/></asset>'
    )


def _clip_xml(clip: _ClipSpec, index: int) -> str:
    return (
        f'<asset-clip ref="{clip.ref}" name="클립{index}" offset="{clip.offset}"'
        f' start="{clip.start}" duration="{clip.duration}"/>'
    )


def _document(
    tmp_path: Path, specs: tuple[_AssetSpec, ...], clips: tuple[_ClipSpec, ...]
) -> ProjectDocument:
    """격자가 정확한 합성 문서 — 파싱만 쓰고 ffmpeg는 모른다."""
    assets_xml = "\n    ".join(_asset_xml(spec) for spec in specs)
    clips_xml = "\n            ".join(_clip_xml(clip, index) for index, clip in enumerate(clips))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        + FORMAT_30
        + "\n    "
        + assets_xml
        + '\n  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n            "
        + clips_xml
        + "\n          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "project.fcpxml"
    path.write_text(body, encoding="utf-8")
    return parse_project(path)


def _content_of(spec: _AssetSpec) -> str:
    return spec.content or f"acid-{spec.xml_id}"


def _media(document: ProjectDocument, specs: tuple[_AssetSpec, ...]) -> tuple[ResolvedMedia, ...]:
    """손 미디어 — 내용 신원·해시는 스펙이 정하는 대로다(문서 순서 그대로).

    같은 내용 시험에서 **프록시 연결만** 다르게 만들 수 없는 게 이 손 미디어의
    한계다 — 결정성 증명은 가짜 프로브가 본 경로로 한다(아래 `probes`).
    """
    by_xml = {spec.xml_id: spec for spec in specs}
    return tuple(
        ResolvedMedia(
            resource_scoped_id=resource.scoped_id,
            asset_content_id=_content_of(by_xml[resource.xml_id]),
            original=Path(f"/media/{resource.xml_id}.mp4"),
            image_source=Path(f"/media/{resource.xml_id}.mp4"),
            original_sha256=f"sha-{_content_of(by_xml[resource.xml_id])}",
            has_audio=resource.has_audio,
            used_proxy=False,
        )
        for resource in document.resources
    )


def _pattern_pcm(frames: int) -> bytes:
    """프레임마다 다른 값 — 남의 프레임을 실어 오면 바로 드러난다."""
    values = array.array("h", ((frame * 3) % 28001 - 14000 for frame in range(frames)))
    return values.tobytes()


def _reference(pcm_root: Path, spec: _AssetSpec) -> PcmReference:
    """손 참조 — PCM 바이트·해시·프레임 수가 실제 파일과 정확히 맞는다(VAD가 잰다)."""
    content = _content_of(spec)
    pcm = _pattern_pcm(spec.frames)
    path = pcm_root / content / "reference.pcm"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm)
    return PcmReference(
        source_id=content,
        original_stem=f"{spec.xml_id}-stem",
        original_sha256=f"sha-{content}",
        path=path,
        pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        analysis_key=f"pcm-{content}",
        pcm_sample0_file_time=Fraction(0),
        sample_rate=RATE,
        channels=1,
        sample_count=spec.frames,
    )


def _unavailable(spec: _AssetSpec, scoped_id: str) -> UnavailableSourceAudio:
    return UnavailableSourceAudio(
        variant="unavailable_source_audio",
        resource_scoped_id=scoped_id,
        source_id=_content_of(spec),
        original_sha256=f"sha-{_content_of(spec)}",
        status="no_audio",
        reason_code="declared_no_audio",
        reason="리소스 hasAudio=0",
    )


def _prepared(
    tmp_path: Path, specs: tuple[_AssetSpec, ...], clips: tuple[_ClipSpec, ...]
) -> PreparedAudio:
    """`prepare_audio`를 부르지 않고 손으로 조립 — 입력이 PreparedAudio 하나로 고정된 덕."""
    document = _document(tmp_path, specs, clips)
    artifacts = tmp_path / "artifacts"
    pcm_root = artifacts / "pcm"
    media = _media(document, specs)
    by_xml = {spec.xml_id: spec for spec in specs}
    pairs = tuple(
        (
            row,
            (
                _reference(pcm_root, by_xml[resource.xml_id])
                if by_xml[resource.xml_id].kind == "pcm"
                else _unavailable(by_xml[resource.xml_id], resource.scoped_id)
            ),
        )
        for row, resource in zip(media, document.resources, strict=True)
    )
    return PreparedAudio(
        document=document,
        media=media,
        audio_inputs=pairs,
        policy=AudioPolicy(),
        config=Config(),
        project_config=ProjectConfig(),
        artifact_dir=artifacts,
        pcm_dir=pcm_root,
        frames_dir=artifacts / "frames",
        inputs_by_resource={pair[0].resource_scoped_id: pair for pair in pairs},
    )


def _clip_ids(document: ProjectDocument) -> tuple[str, ...]:
    return tuple(item.instance_id for item in document.storyline if isinstance(item, ClipInstance))


# ─────────────────────────────────────────────────────────────
#  가짜 전사기 · 가짜 VAD 세션
# ─────────────────────────────────────────────────────────────


def _seg(start: float, end: float, text: str) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text}


def _response(chunk: Chunk, payload: dict[str, Any]) -> ChunkResponse:
    return ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)


def _default_whisper(job: TranscriptionJob) -> list[ChunkResponse]:
    payload = {"segments": [_seg(*row) for row in DEFAULT_SEGMENTS]}
    return [_response(chunk, payload) for chunk in job.chunks]


@dataclass
class _Scripted:
    """가짜 전사기 — 잡을 그대로 기록하고 깨끗한 오디오엔 재전사를 예약하지 않는다."""

    whisper: Callable[[TranscriptionJob], list[ChunkResponse]] = field(
        default_factory=lambda: _default_whisper
    )
    calls: list[TranscriptionJob] = field(default_factory=list)

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls.append(job)
        if job.kind != FIXTURE_KIND:
            raise RuntimeError(f"예약하지 않은 {job.kind} 호출이다")
        return list(self.whisper(job))


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
        self.calls = 0

    def run(self, output_names: object, feeds: dict[str, object]) -> list[_FakeArray]:
        self.calls += 1
        return [
            _FakeArray([[self.probability]], "float32"),
            _FakeArray([[[0.0] * 128], [[0.0] * 128]], "float32"),
        ]


class _BrokenSession:
    """run이 죽는 세션 — 가드가 keep_all로 닫혀 블록이 하나도 없는 세계를 만든다."""

    def run(self, output_names: object, feeds: dict[str, object]) -> list[object]:
        raise RuntimeError("VAD 세션이 죽었다")


# ─────────────────────────────────────────────────────────────
#  가짜 프레임 경계 — probe/extract를 sampling 이름공간에서 갈아끼운다
# ─────────────────────────────────────────────────────────────


@dataclass
class _FrameFake:
    """가짜 probe/extract가 본 호출 기록 — 어느 파일을 몇 장 뽑았는지가 계약이다."""

    probes: list[Path] = field(default_factory=list)
    extractions: list[int] = field(default_factory=list)


def _fake_frames(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_extract: bool = False,
    corrupt_digest_from: int | None = None,
) -> _FrameFake:
    """probe/extract/샘플 해시를 흉내 낸다 — 추출 바이트는 실제로 쓴다.

    `corrupt_digest_from` 이상의 프레임 인덱스는 **샘플 해시만** 틀리게 찍는다 —
    디스크의 바이트와 기록된 해시가 어긋나는 상황을 만들어 `build_payload`의
    바이트 해시 대조(policy)가 실제로 닫히는 길을 연다.
    """
    fake = _FrameFake()

    def probe(source: Path, stream_index: int = 0) -> VideoStreamInfo:
        fake.probes.append(Path(source))
        return VideoStreamInfo(
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
        fake.extractions.append(frame_index)
        if fail_extract:
            raise RuntimeError("ffmpeg가 죽었다 (exit 69)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"frame:{frame_index}".encode())
        return destination

    def digest(path: Path) -> str:
        raw = path.read_bytes()
        index = int(raw.split(b":")[1])
        if corrupt_digest_from is not None and index >= corrupt_digest_from:
            return hashlib.sha256(b"tampered").hexdigest()
        return hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(sampling, "probe_video_stream", probe)
    monkeypatch.setattr(sampling, "extract_frame_index", extract)
    monkeypatch.setattr(sampling, "sha256_file", digest)
    return fake


# ─────────────────────────────────────────────────────────────
#  가짜 비전 클라이언트 — 주입받은 클라이언트의 계약만 흉내낸다
# ─────────────────────────────────────────────────────────────


class _HTTP429(Exception):
    """SDK 예외의 관습 모양 — status_code 속성으로 429를 알린다."""

    def __init__(self) -> None:
        super().__init__("429 Too Many Requests — 할당량 초과")
        self.status_code = 429


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
    """가짜 비전 클라이언트 — 러너가 만들지 않고 주입받는 대상.

    `request_vision`이 부르는 `client.chat.completions.create(model=…, **payload)`
    모양만 흉내낸다. 호출은 (model, messages)로 기록된다 — 직접 호출 우회가
    있으면 기록이 남는다.
    """

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


def _reject_with_429(model: str, messages: list[dict]) -> dict:
    raise _HTTP429()


def _explode_if_called(model: str, messages: list[dict]) -> dict:
    raise AssertionError("비전 호출이 불렸다 — 이 경로에서는 없어야 한다")


# ─────────────────────────────────────────────────────────────
#  조립 — 분석 → 출판까지 한 번에
# ─────────────────────────────────────────────────────────────


def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    specs: tuple[_AssetSpec, ...] = (_AssetSpec("r2"),),
    clips: tuple[_ClipSpec, ...] = (_ClipSpec("r2", duration="24s"),),
    session: object | None = _ConstantSession(0.1),
    respond: Callable[[str, list[dict]], dict] = _accept_front,
    online: bool = True,
    fail_extract: bool = False,
    corrupt_digest_from: int | None = None,
) -> tuple[runner.PublishedProject, _FakeVisionClient, _FrameFake]:
    """기본 장면(블록 둘)을 분석해 출판한다 — 나머지 선택은 깃발로 고른다."""
    prepared = _prepared(tmp_path, specs, clips)
    analyzed = runner.analyze_audio(prepared, transcriber=_Scripted(), session=session)
    frames = _fake_frames(
        monkeypatch, fail_extract=fail_extract, corrupt_digest_from=corrupt_digest_from
    )
    client = _FakeVisionClient(respond)
    result = runner.publish_project(
        analyzed,
        vision_client=client,
        destination=tmp_path / "export",
        online=online,
    )
    return result, client, frames


def _review_of(result: runner.PublishedProject) -> dict:
    return json.loads(result.package.review.read_text(encoding="utf-8"))


def _output_frames(result: runner.PublishedProject) -> int:
    """출력 타임라인 전체 — 유지 런과 갭의 출력 끝 중 최댓값이다."""
    return max(
        (
            *(run.output_timeline_frames.end_frame for run in result.plan.runs),
            *(gap.output_timeline_frames.end_frame for gap in result.plan.gaps),
        ),
        default=0,
    )


def _removed_frames(result: runner.PublishedProject) -> int:
    return sum(
        row.source_frames.end_frame - row.source_frames.start_frame for row in result.plan.removed
    )


# ─────────────────────────────────────────────────────────────
#  1 · 정상 경로 — 승인 판정이 패키지를 실제로 짧게 만든다
# ─────────────────────────────────────────────────────────────


def test_accepted_decisions_shrink_the_published_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, client, frames = _publish(tmp_path, monkeypatch)

    assert len(result.partition.blocks) == 2
    assert [decision.status for decision in result.decisions] == ["accepted", "accepted"]
    assert len(client.calls) == len(result.partition.blocks)
    # 모델 이름은 설정에서 온다 — 러너가 리터럴을 알지 못한다(계약 §7).
    vision_model = result.analyzed.prepared.project_config.vision_model
    assert {model for model, _messages in client.calls} == {vision_model}
    assert frames.extractions  # 표본이 실제로 뽑혔다

    removed = _removed_frames(result)
    assert removed > 0
    assert _output_frames(result) == 24 * 30 - removed  # 타일링: 출력 = 입력 − 제거

    assert result.findings == ()
    assert result.dtd_path is None
    review = _review_of(result)
    assert review["final_accepted"] is True
    assert review["final_blockers"] == []
    assert result.package.fcpxml.is_file()
    assert (tmp_path / "export" / "current").is_symlink()


# ─────────────────────────────────────────────────────────────
#  2 · 3 · 4 — 실패는 그 블록의 error 판정이고 난간이 문다
# ─────────────────────────────────────────────────────────────


def test_frame_sampling_failure_leaves_an_error_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, client, _frames = _publish(tmp_path, monkeypatch, fail_extract=True)

    assert len(result.decisions) == len(result.partition.blocks)  # 블록이 빠지지 않는다
    assert all(decision.status == "error" for decision in result.decisions)
    assert "프레임 추출이 실패했다" in result.decisions[0].reason
    assert "exit 69" in result.decisions[0].reason  # 실제 오류 메시지가 살아 있다
    assert client.calls == []  # 표본이 죽었으니 유료 호출은 없다
    review = _review_of(result)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]
    assert result.package.fcpxml.is_file()  # 차단된 패키지지 패키지 없음이 아니다


def test_transport_failure_leaves_an_error_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, client, frames = _publish(tmp_path, monkeypatch, respond=_reject_with_429)

    assert len(result.decisions) == len(result.partition.blocks)
    assert all(decision.status == "error" for decision in result.decisions)
    assert "429" in result.decisions[0].reason
    assert len(client.calls) == len(result.partition.blocks)  # 물어는 봤다
    assert frames.extractions  # 표본은 성공했다 — 실패는 전송에서 났다
    review = _review_of(result)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]
    assert result.package.fcpxml.is_file()


# ─────────────────────────────────────────────────────────────
#  5 — online=False는 정직한 닫힘이다 (§2-b 회귀 테스트)
# ─────────────────────────────────────────────────────────────


def test_online_false_closes_every_decision_and_blocks_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, client, _frames = _publish(tmp_path, monkeypatch, online=False)

    assert [decision.status for decision in result.decisions] == ["error", "error"]
    assert "닫혀 있다" in result.decisions[0].reason  # 게이트의 문구가 그대로 실렸다
    assert client.calls == []  # 승인 없이는 클라이언트가 불리지도 않는다
    review = _review_of(result)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]
    assert result.package.fcpxml.is_file()


# ─────────────────────────────────────────────────────────────
#  6 — build_payload 실패는 그 블록만 죽인다 (샘플 해시 불일치)
# ─────────────────────────────────────────────────────────────


def test_payload_hash_failure_isolates_to_its_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, client, _frames = _publish(tmp_path, monkeypatch, corrupt_digest_from=350)

    assert len(result.partition.blocks) == 2
    assert result.decisions[0].status == "accepted"  # 앞 블록은 그대로 흐른다
    assert result.decisions[1].status == "error"
    assert "샘플 해시와 다르다" in result.decisions[1].reason
    assert len(client.calls) == 1  # 뒤 블록은 페이로드에서 죽어 호출까지 못 간다
    review = _review_of(result)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]


# ─────────────────────────────────────────────────────────────
#  7 · 9 — 블록 0개 세계: 판정 0개, 비전 호출 0회
# ─────────────────────────────────────────────────────────────


def test_zero_block_project_publishes_with_zero_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """가드가 keep_all로 닫힌 세계 — 오디오는 있어도 블록은 없다."""
    result, client, _frames = _publish(
        tmp_path, monkeypatch, clips=(_ClipSpec("r2", duration="24s"),), session=_BrokenSession()
    )

    assert result.partition.blocks == ()
    assert result.decisions == ()
    assert client.calls == []
    review = _review_of(result)
    assert review["final_accepted"] is True
    assert result.package.fcpxml.is_file()


def test_all_declared_no_audio_flows_to_the_end_without_vision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전 리소스가 선언된 무음 — 가짜 클라이언트를 '부르면 실패'로 꽂아 0호출을 증명한다."""
    result, client, _frames = _publish(
        tmp_path,
        monkeypatch,
        specs=(_AssetSpec("r2", kind="declared"),),
        clips=(_ClipSpec("r2", duration="8s"),),
        session=None,
        respond=_explode_if_called,
    )

    assert result.partition.blocks == ()
    assert result.decisions == ()
    review = _review_of(result)
    assert review["final_accepted"] is True
    assert result.package.fcpxml.is_file()


# ─────────────────────────────────────────────────────────────
#  8 — 판정 정렬 세 단언: 출판 앞 검사와 __post_init__ 양쪽에서 (위조 입력)
# ─────────────────────────────────────────────────────────────


def _published_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> runner.PublishedProject:
    result, _client, _frames = _publish(tmp_path, monkeypatch)
    assert len(result.partition.blocks) == 2
    return result


def test_alignment_gate_rejects_a_dropped_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """단언 1 — 개수가 블록 수와 다르면 출판 앞 검사가 문다."""
    result = _published_once(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="다르다"):
        runner._require_decision_alignment(result.partition.blocks, result.decisions[:1])
    with pytest.raises(ValueError, match="다르다"):
        data_replace(result, decisions=result.decisions[:1])


def test_alignment_gate_rejects_a_duplicated_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """단언 2 — 같은 블록을 두 번 가리키는 판정은 출판 앞 검사가 문다."""
    result = _published_once(tmp_path, monkeypatch)
    duplicated = (result.decisions[0], result.decisions[0])
    with pytest.raises(ValueError, match="두 번"):
        runner._require_decision_alignment(result.partition.blocks, duplicated)
    with pytest.raises(ValueError, match="두 번"):
        data_replace(result, decisions=duplicated)


def test_alignment_gate_rejects_reordered_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """단언 3 — 순서·신원이 어긋나면 출판 앞 검사가 문다."""
    result = _published_once(tmp_path, monkeypatch)
    reordered = (result.decisions[1], result.decisions[0])
    with pytest.raises(ValueError, match="어긋난다"):
        runner._require_decision_alignment(result.partition.blocks, reordered)
    with pytest.raises(ValueError, match="어긋난다"):
        data_replace(result, decisions=reordered)


# ─────────────────────────────────────────────────────────────
#  10 — 같은 내용 신원에 미디어가 둘: 문서 순서 첫 항목이 쓰인다 (§4-a)
# ─────────────────────────────────────────────────────────────


def test_shared_content_picks_the_first_media_in_document_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = (
        _AssetSpec("r2", content="acid-shared"),
        _AssetSpec("r3", content="acid-shared"),
    )
    clips = (
        _ClipSpec("r2", offset="0s", start="0s", duration="12s"),
        _ClipSpec("r3", offset="12s", start="12s", duration="12s"),
    )
    result, client, frames = _publish(tmp_path, monkeypatch, specs=specs, clips=clips)

    assert len(result.partition.blocks) == 2  # 인스턴스마다 블록 하나
    assert {block.source_id for block in result.partition.blocks} == {"acid-shared"}
    # 두 블록 모두 문서 순서 첫 미디어(r2)에서 뽑혔다 — r3의 프록시 연결은 쓰이지
    # 않는다. 마지막이나 dict 순서를 골랐다면 /media/r3.mp4가 probes에 남는다.
    assert set(frames.probes) == {Path("/media/r2.mp4")}
    assert [decision.status for decision in result.decisions] == ["accepted", "accepted"]
    review = _review_of(result)
    assert review["final_accepted"] is True
