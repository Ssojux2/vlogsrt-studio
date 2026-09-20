"""PROTO-3 러너 2/3 — 전사 · 자막 투영 · 경계 · VAD · 가드 (계약).

여기서 증명하는 것:

- `analyze_audio`가 `PreparedAudio`를 **분석이 끝난 상태**로 만든다 — 전사·자막·
  경계 재전사·VAD·가드가 같은 참조 위에서 한 번에 흐르고, 산출물은
  `partition_blocks`를 그대로 통과한다.
- 부재 소스의 인스턴스마다 **합성 가드가 정확히 하나** 붙고 대응표
  (declared→inaudible, stream/pcm→keep_all)를 어기면 `AnalyzedAudio.__post_init__`이
  그 자리에서 죽는다 — 하류는 이 방향을 검사하지 않는다(아래 A1·B3 시험).
- **선이 어디인지 못 박는다.** `stream_unavailable` 인스턴스에 `status="success"`·
  빈 protected를 붙여도 `partition_blocks`는 거부하지 **않고** 클립 전체가 제거
  후보가 된다(A1). 진짜 난간은 잠금·부재 쌍이고, 대응표 자체는 이 단계의
  `__post_init__`이 유일하게 지킨다(B3은 하류가 조용히 통과시킨다).
- ONNX 게이트는 유료 호출 **앞**에 선다 — 아티팩트가 깨진 환경에서 77분 회차
  전사비를 다 치르고 죽지 않는다. 참조가 하나도 없으면 세션을 아예 만들지
  않는다(전 리소스가 선언된 무음인 프로젝트는 numpy 없이도 끝까지 흐른다).
- references는 `source_id`로 유일화한다 — 서로 다른 xml_id가 같은 바이트를
  가리키는 문서가 경계 계획에서 죽지 않는다.

합성 문서·손 참조·스크립트된 전사기·가짜 세션만 쓴다. 유료 호출도
`tests/fixtures/**` 수정도 없다.
"""

from __future__ import annotations

import array
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as data_replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project import runner
from vlogsrt.project.boundary import UnavailableInstance
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.edit import partition_blocks
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.pcm import AudioPolicy, PcmReference, SampleRange
from vlogsrt.project.runner import AnalyzedAudio, PreparedAudio
from vlogsrt.project.speech import UnavailableSourceAudio
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vad.runtime import VadUnavailable
from vlogsrt.transcribe import FIXTURE_KIND, VERIFY_KIND, ChunkResponse, TranscriptionJob

RATE = 16000
DEFAULT_FRAMES = 24 * RATE
"""기본 손 참조의 길이 — 24초. 클립 둘이 [0,12)·[12,24)로 나눠 쓴다."""

FORMAT_30 = (
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>'
)

DEFAULT_SEGMENTS = (
    (0.5, 1.0, "첫 장면 인사"),
    (10.5, 11.0, "골목 소개"),
    (12.5, 13.0, "두 번째 클립"),
    (22.0, 22.5, "마무리"),
)
"""깨끗한 1차 전사 대본 — 되풀이가 없어 재전사(rescue) 호출은 없다."""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 문서 · 손 미디어 · 손 참조 · 부재 기록
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AssetSpec:
    """애셋 하나 — kind가 둘째 칸(참조|부재 기록)을 정한다."""

    xml_id: str
    kind: str = "pcm"  # pcm | declared | stream | pcm_fail
    content: str | None = None  # 같은 바이트 시험 — 내용 신원을 직접 찍는다
    frames: int = DEFAULT_FRAMES


@dataclass(frozen=True)
class _ClipSpec:
    """칼날 하나 — 시각은 전부 초 문자열 그대로 XML에 간다."""

    ref: str
    offset: str = "0s"
    start: str = "0s"
    duration: str = "12s"
    enabled: bool = True


def _asset_xml(spec: _AssetSpec) -> str:
    has_audio = "0" if spec.kind == "declared" else "1"
    return (
        f'<asset id="{spec.xml_id}" name="{spec.xml_id}.mp4" start="0s" duration="600s"'
        f' hasAudio="{has_audio}" format="r1">'
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
    """손 미디어 — 내용 신원·해시는 스펙이 정하는 대로다(문서 순서 그대로)."""
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
    kind = spec.kind
    if kind == "declared":
        status, reason_code, reason = "no_audio", "declared_no_audio", "리소스 hasAudio=0"
    elif kind == "stream":
        status, reason_code, reason = "error", "stream_unavailable", "프로브가 죽었다"
    else:
        status, reason_code, reason = "error", "pcm_unavailable", "참조가 죽었다"
    return UnavailableSourceAudio(
        variant="unavailable_source_audio",
        resource_scoped_id=scoped_id,
        source_id=_content_of(spec),
        original_sha256=f"sha-{_content_of(spec)}",
        status=status,  # type: ignore[arg-type]
        reason_code=reason_code,  # type: ignore[arg-type]
        reason=reason,
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


def _partition_of(result: AnalyzedAudio) -> object:
    """산출물이 P6 분할을 실제로 통과하는지 — 그 함수를 직접 불러서 증명한다."""
    prepared = result.prepared
    return partition_blocks(
        prepared.document,
        prepared.media,
        result.instance_guards,
        result.locked_instance_ids,
        result.unavailable_instances,
        prepared.project_config,
    )


# ─────────────────────────────────────────────────────────────
#  가짜 전사기 · 가짜 세션
# ─────────────────────────────────────────────────────────────


def _seg(start: float, end: float, text: str) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text}


def _response(chunk: Chunk, payload: dict[str, Any]) -> ChunkResponse:
    return ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)


def _segments_whisper(
    segments: tuple[tuple[float, float, str], ...],
) -> Callable[[TranscriptionJob], list[ChunkResponse]]:
    """소스 전사는 정해든 세그먼트로, 경계 재전사는 창에 맞춘 한 장으로 답한다."""

    def handler(job: TranscriptionJob) -> list[ChunkResponse]:
        if job.namespace is not None and "boundary" in job.namespace.parts:
            return _boundary_whisper(job)
        payload = {"segments": [_seg(*row) for row in segments]}
        return [_response(chunk, payload) for chunk in job.chunks]

    return handler


def _boundary_whisper(job: TranscriptionJob) -> list[ChunkResponse]:
    """남긴 창 안쪽으로 맞춘 한 장 — 정제(lead_in/lead_out·최소 길이)를 감안해 여유를 둔다."""
    responses: list[ChunkResponse] = []
    for chunk in job.chunks:
        start = min(0.2, chunk.duration * 0.2)
        end = min(0.7, start + chunk.duration * 0.45)
        responses.append(_response(chunk, {"segments": [_seg(start, end, "경계 구간 자막")]}))
    return responses


def _default_whisper(job: TranscriptionJob) -> list[ChunkResponse]:
    return _segments_whisper(DEFAULT_SEGMENTS)(job)


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

    def source_calls(self) -> list[TranscriptionJob]:
        return [
            job
            for job in self.calls
            if job.namespace is None or "boundary" not in job.namespace.parts
        ]


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
    """run이 죽는 세션 — infer_vad가 자산 전체 error로 닫는 길을 연다."""

    def run(self, output_names: object, feeds: dict[str, object]) -> list[object]:
        raise RuntimeError("VAD 세션이 죽었다")


def _analyze(
    prepared: PreparedAudio,
    *,
    session: object | None = _ConstantSession(0.1),
    whisper: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None,
) -> tuple[AnalyzedAudio, _Scripted]:
    scripted = _Scripted(whisper or _default_whisper)
    result = runner.analyze_audio(prepared, transcriber=scripted, session=session)
    return result, scripted


def _forbid_vad_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAD 런타임이 불리면 안 되는 경로 — 세션 게이트가 묻지도 않는 것을 증명한다."""

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("VAD 런타임이 불렸다 — 이 경로에서는 없어야 한다")

    monkeypatch.setattr(runner, "bundled_artifact", boom)
    monkeypatch.setattr(runner, "create_cpu_session", boom)


# ─────────────────────────────────────────────────────────────
#  1 · 정상 경로 — 전사·자막·경계·VAD·가드·분할
# ─────────────────────────────────────────────────────────────


def test_one_audio_source_flows_to_guards_and_partition(tmp_path: Path) -> None:
    """전사 한 번으로 이웃 클립 둘이 다 자막을 얻는다 — 산출물은 P6을 통과한다."""
    specs = (_AssetSpec("r2"),)
    clips = (
        _ClipSpec("r2", offset="0s", start="0s", duration="12s"),
        _ClipSpec("r2", offset="12s", start="12s", duration="12s"),
    )
    prepared = _prepared(tmp_path, specs, clips)

    result, scripted = _analyze(prepared, session=_ConstantSession(0.1))

    assert [analysis.status for analysis in result.analyses] == ["success"]
    assert result.source_captions.source_cues
    assert [len(slice_.instance_ids) for slice_ in result.slices] == [2]  # 이웃 합침
    assert result.boundary_plan.requests == ()  # 잘린 세그먼트가 없다
    assert result.transcripts == ()
    assert set(result.vad_by_source) == {"acid-r2"}
    assert result.vad_by_source["acid-r2"].status == "success"
    assert tuple(guard.instance_id for guard in result.instance_guards) == _clip_ids(
        prepared.document
    )
    assert all(guard.status == "success" for guard in result.instance_guards)
    assert len(result.source_guards) == 1
    assert result.locked_instance_ids == frozenset()
    assert [job.kind for job in scripted.calls] == [
        FIXTURE_KIND,
        VERIFY_KIND,
    ]  # 소스 전사 한 번(교정 호출이 따라 붙는다, D39)
    partition = _partition_of(result)
    assert partition.blocks  # 보호의 여집합이 블록으로 나온다
    # 블록은 문서의 클립 인스턴스에서 왔다 — 지어낸 인스턴스가 없다
    assert {block.instance_id for block in partition.blocks} <= set(_clip_ids(prepared.document))


# ─────────────────────────────────────────────────────────────
#  2 · 3 — 부재 세 코드의 합성 가드
# ─────────────────────────────────────────────────────────────


def test_declared_no_audio_instance_gets_exactly_one_inaudible_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """선언된 무음 — 세션도 전사도 없이 끝까지 흐르고 가드는 inaudible 하나다."""
    _forbid_vad_runtime(monkeypatch)
    specs = (_AssetSpec("r2", kind="declared"),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="8s"),))

    result, scripted = _analyze(prepared, session=None)

    assert scripted.calls == []  # 유료 호출이 없다
    guard = result.instance_guards[0]
    assert guard.status == "inaudible"
    assert guard.protected_frames == ()
    assert guard.source_id == "acid-r2"
    assert result.source_guards == ()
    assert result.vad_by_source == {}
    (clip_id,) = _clip_ids(prepared.document)
    assert result.locked_instance_ids == frozenset({clip_id})
    assert [record.reason_code for record in result.unavailable_instances] == ["declared_no_audio"]
    partition = _partition_of(result)
    assert partition.blocks == ()


@pytest.mark.parametrize("kind", ["stream", "pcm_fail"])
def test_pre_pcm_failure_instance_gets_keep_all_guard(tmp_path: Path, kind: str) -> None:
    """들은 수 있었는데 못 들은 실패 — 무음이 아니라 keep_all 닫힘이다."""
    _code = "stream_unavailable" if kind == "stream" else "pcm_unavailable"
    specs = (_AssetSpec("r2", kind=kind),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="8s"),))

    result, scripted = _analyze(prepared, session=None)

    assert scripted.calls == []
    guard = result.instance_guards[0]
    assert guard.status == "keep_all"
    assert guard.protected_frames == ()
    assert [record.reason_code for record in result.unavailable_instances] == [_code]
    partition = _partition_of(result)
    assert partition.blocks == ()


# ─────────────────────────────────────────────────────────────
#  4 — 세 줄: 선이 어디인지 못 박는다 (A1 · A2 · A3 · B1 · B3)
# ─────────────────────────────────────────────────────────────


def _scene(
    tmp_path: Path, *, has_audio: bool
) -> tuple[ProjectDocument, tuple[ResolvedMedia, ...], str]:
    specs = (_AssetSpec("r2", kind="pcm" if has_audio else "declared"),)
    document = _document(tmp_path, specs, (_ClipSpec("r2", duration="8s"),))
    return document, _media(document, specs), _clip_ids(document)[0]


def _forged_guard(clip_id: str, status: str) -> InstanceGuard:
    return InstanceGuard(
        instance_id=clip_id,
        source_id="acid-r2",
        status=status,  # type: ignore[arg-type]
        protected_frames=(),
        review=(),
    )


def test_stream_failure_success_guard_without_lock_becomes_blocks(
    tmp_path: Path,
) -> None:
    """A1 — 하류는 이 짝을 거부하지 않는다. 보호 없는 가청 클립 전체가 제거 후보다.

    `stream_unavailable` 소스에 `status="success"`·빈 protected를 붙이면
    `partition_blocks`가 **raise하지 않고** 클립 전체(8초 = 240프레임)를 블록으로
    낸다. 이것이 §2-a가 드러낸 결함의 모양이고, 진짜 난간은 가드가 아니라
    잠금·부재 쌍이다(아래 A3).
    """
    document, media, clip_id = _scene(tmp_path, has_audio=True)

    partition = partition_blocks(
        document, media, (_forged_guard(clip_id, "success"),), frozenset(), (), ProjectConfig()
    )

    assert len(partition.blocks) == 1
    block = partition.blocks[0]
    assert block.instance_id == clip_id
    assert (block.source_frames.start_frame, block.source_frames.end_frame) == (0, 240)


def test_declared_failure_success_guard_raises(tmp_path: Path) -> None:
    """B1 — 하류가 거부하는 유일한 방향: 불가청 클립의 success 가드다."""
    document, media, clip_id = _scene(tmp_path, has_audio=False)
    with pytest.raises(ValueError, match="불가청"):
        partition_blocks(
            document,
            media,
            (_forged_guard(clip_id, "success"),),
            frozenset(),
            (),
            ProjectConfig(),
        )


def test_declared_failure_keep_all_guard_passes_silently(tmp_path: Path) -> None:
    """B3 — 하류는 keep_all을 조용히 통과시킨다. 대응표를 지키는 곳은 __post_init__뿐이다."""
    document, media, clip_id = _scene(tmp_path, has_audio=False)
    partition = partition_blocks(
        document, media, (_forged_guard(clip_id, "keep_all"),), frozenset(), (), ProjectConfig()
    )
    assert partition.blocks == ()


def test_stream_failure_keep_all_or_lock_yields_no_blocks(tmp_path: Path) -> None:
    """A2·A3 — 처방(keep_all)이거나 잠금·부재가 동반되면 블록이 없다."""
    document, media, clip_id = _scene(tmp_path, has_audio=True)

    kept = partition_blocks(
        document, media, (_forged_guard(clip_id, "keep_all"),), frozenset(), (), ProjectConfig()
    )
    assert kept.blocks == ()

    record = UnavailableInstance(
        resource_scoped_id=document.resources[0].scoped_id,
        instance_id=clip_id,
        reason_code="stream_unavailable",
    )
    locked = partition_blocks(
        document,
        media,
        (_forged_guard(clip_id, "success"),),
        frozenset({clip_id}),
        (record,),
        ProjectConfig(),
    )
    assert locked.blocks == ()


# ─────────────────────────────────────────────────────────────
#  5 · 6 — VAD 실패 닫힘과 ONNX 게이트
# ─────────────────────────────────────────────────────────────


def test_vad_error_closes_the_source_guard_keep_all(tmp_path: Path) -> None:
    """세션이 죽으면 그 소스의 가드가 전 구간 보호다 — 기존 경로가 그대로 탄다."""
    specs = (_AssetSpec("r2"),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="12s"),))

    result, _scripted = _analyze(prepared, session=_BrokenSession())

    vad = result.vad_by_source["acid-r2"]
    assert vad.status == "error"
    assert vad.error is not None and "죽었다" in vad.error
    source_guard = result.source_guards[0]
    assert source_guard.status == "keep_all"
    assert source_guard.protected == (SampleRange(0, DEFAULT_FRAMES),)
    assert all(guard.status == "keep_all" for guard in result.instance_guards)


def test_broken_artifact_is_one_hard_failure_before_paid_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """게이트는 유료 호출 앞에 선다 — 전사비를 치르고 나서 죽지 않는다."""

    def broken(project_config: object) -> object:
        raise VadUnavailable("꾸러미 VAD 아티팩트이 깨졌다")

    monkeypatch.setattr(runner, "bundled_artifact", broken)
    specs = (_AssetSpec("r2"),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="12s"),))
    scripted = _Scripted()

    with pytest.raises(VadUnavailable):
        runner.analyze_audio(prepared, transcriber=scripted, session=None)
    assert scripted.calls == []


# ─────────────────────────────────────────────────────────────
#  7 — 혼합 프로젝트: 가드 수 == 클립 인스턴스 수
# ─────────────────────────────────────────────────────────────


def test_mixed_project_guards_every_clip_instance(tmp_path: Path) -> None:
    """정상 + 부재 셋이 섞여도 인스턴스마다 가드가 정확히 하나다."""
    specs = (
        _AssetSpec("r2"),
        _AssetSpec("r3", kind="declared"),
        _AssetSpec("r4", kind="stream"),
        _AssetSpec("r5", kind="pcm_fail"),
    )
    clips = (
        _ClipSpec("r2", duration="12s"),
        _ClipSpec("r3", offset="12s", duration="8s"),
        _ClipSpec("r4", offset="20s", duration="8s"),
        _ClipSpec("r5", offset="28s", duration="8s"),
    )
    prepared = _prepared(tmp_path, specs, clips)

    result, _scripted = _analyze(prepared, session=_ConstantSession(0.1))

    ids = _clip_ids(prepared.document)
    assert len(result.instance_guards) == len(ids) == 4
    statuses = {guard.instance_id: guard.status for guard in result.instance_guards}
    assert statuses[ids[0]] == "success"
    assert statuses[ids[1]] == "inaudible"
    assert statuses[ids[2]] == "keep_all"
    assert statuses[ids[3]] == "keep_all"
    assert set(result.locked_instance_ids) == set(ids[1:])
    partition = _partition_of(result)
    assert partition.blocks  # 정상 소스만 블록을 낸다
    assert {block.instance_id for block in partition.blocks} == {ids[0]}


# ─────────────────────────────────────────────────────────────
#  8 — __post_init__ 다섯 단언 (위조 입력 red-first)
# ─────────────────────────────────────────────────────────────


def _normal_scene(tmp_path: Path) -> AnalyzedAudio:
    specs = (_AssetSpec("r2"),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="12s"),))
    result, _scripted = _analyze(prepared, session=_ConstantSession(0.1))
    return result


def _stream_scene(tmp_path: Path) -> AnalyzedAudio:
    specs = (_AssetSpec("r2", kind="stream"),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="8s"),))
    result, _scripted = _analyze(prepared, session=None)
    return result


def _declared_scene(tmp_path: Path) -> AnalyzedAudio:
    specs = (_AssetSpec("r2", kind="declared"),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="8s"),))
    result, _scripted = _analyze(prepared, session=None)
    return result


def test_post_init_rejects_a_dropped_guard(tmp_path: Path) -> None:
    """단언 1 — 개수가 문서의 클립 인스턴스 수와 다르면 죽는다."""
    specs = (_AssetSpec("r2"), _AssetSpec("r3", kind="declared"))
    clips = (_ClipSpec("r2", duration="8s"), _ClipSpec("r3", offset="8s", duration="8s"))
    prepared = _prepared(tmp_path, specs, clips)
    result, _scripted = _analyze(prepared, session=_ConstantSession(0.1))
    with pytest.raises(ValueError, match="클립 인스턴스 수"):
        data_replace(result, instance_guards=result.instance_guards[:1])


def test_post_init_rejects_reordered_guards(tmp_path: Path) -> None:
    """단언 2 — 순서·신원이 문서와 어긋나면 죽는다."""
    specs = (_AssetSpec("r2"), _AssetSpec("r3", kind="declared"))
    clips = (_ClipSpec("r2", duration="8s"), _ClipSpec("r3", offset="8s", duration="8s"))
    prepared = _prepared(tmp_path, specs, clips)
    result, _scripted = _analyze(prepared, session=_ConstantSession(0.1))
    guards = list(result.instance_guards)
    guards[0], guards[1] = guards[1], guards[0]
    with pytest.raises(ValueError, match="순서"):
        data_replace(result, instance_guards=tuple(guards))


def test_post_init_rejects_status_that_breaks_the_table(tmp_path: Path) -> None:
    """단언 3 — 오기 방향 셋: declared에 keep_all·success, stream에 inaudible."""
    declared = _declared_scene(tmp_path)
    guard = declared.instance_guards[0]
    with pytest.raises(ValueError, match="대응"):
        data_replace(declared, instance_guards=(data_replace(guard, status="keep_all"),))
    with pytest.raises(ValueError, match="대응"):
        data_replace(declared, instance_guards=(data_replace(guard, status="success"),))

    stream = _stream_scene(tmp_path)
    stream_guard = stream.instance_guards[0]
    with pytest.raises(ValueError, match="대응"):
        data_replace(stream, instance_guards=(data_replace(stream_guard, status="inaudible"),))


def test_post_init_rejects_unavailable_instance_without_lock_or_record(
    tmp_path: Path,
) -> None:
    """단언 4 — 부재 인스턴스는 잠금·부재 기록과 함께 와야 한다(A1을 표현 불가능하게)."""
    result = _stream_scene(tmp_path)
    with pytest.raises(ValueError, match="잠금"):
        data_replace(result, locked_instance_ids=frozenset())
    with pytest.raises(ValueError, match="잠금"):
        data_replace(result, unavailable_instances=())


def test_post_init_rejects_vad_keys_that_differ_from_pcm_sources(tmp_path: Path) -> None:
    """단언 5 — '키가 없다'는 '검사할 것이 없다'로 읽히는 길을 끊는다."""
    result = _normal_scene(tmp_path)
    with pytest.raises(ValueError, match="vad_by_source"):
        data_replace(result, vad_by_source={})


# ─────────────────────────────────────────────────────────────
#  9 — 같은 바이트를 가리키는 xml_id 둘 (references 유일화)
# ─────────────────────────────────────────────────────────────


def test_two_ids_over_the_same_bytes_flow_to_the_end(tmp_path: Path) -> None:
    """서로 다른 xml_id가 같은 내용을 가리켜도 경계 계획이 죽지 않는다(§4-a)."""
    specs = (
        _AssetSpec("r2", content="acid-shared"),
        _AssetSpec("r3", content="acid-shared"),
    )
    clips = (
        _ClipSpec("r2", offset="0s", start="0s", duration="12s"),
        _ClipSpec("r3", offset="12s", start="12s", duration="12s"),
    )
    prepared = _prepared(tmp_path, specs, clips)
    first, second = prepared.audio_inputs[0][1], prepared.audio_inputs[1][1]
    assert isinstance(first, PcmReference) and isinstance(second, PcmReference)
    assert first is not second  # prepare_audio는 참조를 둘 낸다 — 접는 것은 주소뿐이다

    result, scripted = _analyze(prepared, session=_ConstantSession(0.1))

    assert len(result.vad_by_source) == 1  # 참조는 source_id로 유일화된다
    assert len([item for item in result.analyses if item.status == "success"]) == 1
    assert [job.kind for job in scripted.source_calls()] == [
        FIXTURE_KIND,
        VERIFY_KIND,
    ]  # 전사도 한 번이다(교정 호출이 따라 붙는다, D39)
    assert len(result.instance_guards) == 2
    partition = _partition_of(result)
    assert partition.blocks


# ─────────────────────────────────────────────────────────────
#  10 — 잘린 세그먼트가 경계 재전사를 몬다
# ─────────────────────────────────────────────────────────────


def test_a_cut_segment_drives_boundary_retranscription(tmp_path: Path) -> None:
    """클립 창이 세그먼트를 자르면 요청이 하나 나오고 갈아끼운 큐는 provenance=boundary다."""
    specs = (_AssetSpec("r2", frames=20 * RATE),)
    prepared = _prepared(tmp_path, specs, (_ClipSpec("r2", duration="5s"),))
    segments = (
        (0.5, 2.0, "첫 문장"),
        (3.0, 5.5, "잘리는 문장"),
        (8.0, 9.5, "뒤 문장"),
    )

    result, scripted = _analyze(
        prepared, session=_ConstantSession(0.1), whisper=_segments_whisper(segments)
    )

    assert len(result.boundary_plan.requests) == 1
    assert [transcript.status for transcript in result.transcripts] == ["success"]
    assert result.transcripts[0].source_id == "acid-r2"
    assert result.boundary.unresolved_request_ids == ()
    assert any(cue.provenance == "boundary" for cue in result.boundary.cues)
    assert [job.kind for job in scripted.calls] == [
        FIXTURE_KIND,
        VERIFY_KIND,
        FIXTURE_KIND,
        VERIFY_KIND,
    ]  # 소스 전사·경계 전사 각각 교정 호출이 따라 붙는다(D39)
    partition = _partition_of(result)
    assert partition.blocks == ()  # 보호가 클립을 덮는다 — 분할은 통과만 한다
