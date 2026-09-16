"""러너 1/3 — 파싱 · 미디어 해석 · PCM, 그리고 부재의 기록 (PROTO-2).

파이프라인의 단계들은 이미 다 있다 — 이 모듈은 그 첫 조각을 잇는다: 문서를 파싱하고,
미디어를 해석하고, 오디오를 PCM으로 저장하고, 전사할 수 없는 리소스는 **기록으로**
남긴다. 산출물은 `speech.transcribe_unique_sources`가 이미 고정한 쌍 모양 그대로다 —
러너가 새 모양을 발명하면 소비자의 신원 검사가 짝짓기 오류를 흘려보낸다.

세 가지 부재가 여기서만 난다:

- `declared_no_audio` — 리소스가 `hasAudio=0`으로 선언했다. probe하지 않는다:
  `has_audio`은 정확히 그 리소스의 선언에서만 온다(media.py 계약).
- `stream_unavailable` — 선언은 오디오가 있는데 `probe_audio_stream`이 죽었다.
- `pcm_unavailable` — probe는 됐는데 `build_reference`가 죽었다.

`reason`에는 실제 예외 메시지를 실는다 — 상수 문구는 진단을 죽인다. 해석 불가능한
미디어(`UnsupportedProject`)는 부재 기록으로 강등되지 않고 그대로 올라온다 —
media.py의 준비 게이트가 의도적으로 하드하기 때문이다. ffmpeg 미설치도 마찬가지로
한 번의 하드 실패다: 도구 게이트를 자원 게이트 앞에 두어 환경 오류 하나가 N개의
부재 기록으로 번역되는 길을 끊는다.

PCM 논리 주소는 **내용 신원**(`asset_content_id`)으로 키잡는다 — 리소스가 아니라.
서로 다른 xml_id를 가진 리소스 둘이 같은 바이트를 가리키면(파서는 중복 id만
거부한다) 주소가 하나로 접혀 추출은 한 번이다. 주소는 애셋 네임스페이스
(`artifact_dir/<content_id>` — source·boundary가 이미 쓴다)와 갈라진
`artifact_dir/pcm/<content_id>`에 산다.

**알려진 한계**: `AudioPolicy.stream_index`(기본 0)은 첫 오디오 트랙만 듣는다.
카메라 + 랩마이크처럼 트랙이 둘 이상인 애셋에서 나머지는 듣지 않으며, 듣지 않았다는
기록도 없다 — 기존 `pcm.py`의 성질이지 이 단계가 만든 것이 아니다. 결과에 정책을
실어 돌려주는 것이 유일한 완화다.

ffmpeg·ffprobe는 `preprocess.probe_audio_stream`과 `pcm.build_reference`를 통해서만
닿는다(계약 §5). 이 파일은 subprocess를 모른다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vlogsrt import log
from vlogsrt.config import Config
from vlogsrt.preprocess import probe_audio_stream, require_ffmpeg
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import MediaOverride, ResolvedMedia, resolve_media
from vlogsrt.project.models import ProjectDocument
from vlogsrt.project.pcm import AudioPolicy, build_reference
from vlogsrt.project.speech import AudioSourceInput, UnavailableSourceAudio

_PCM_DIR = "pcm"
"""PCM 논리 주소의 네임스페이스 — source·boundary가 쓰는 애셋 디렉터리와 갈라진다."""

_FRAMES_DIR = "frames"
"""프레임 표본(PROTO-4)의 네임스페이스 뿌리 — 미리 만들어 경로로 돌려준다."""

_POINTER_NAME = "reference.pcm"
"""논리 캐시 주소의 파일 이름 — 세대 PCM·세대 기록은 이 이름을 앞두고 나란히 산다."""


@dataclass(frozen=True, slots=True)
class PreparedAudio:
    """오디오 준비 단계의 산출물 — 나머지 러너 태스크(PROTO-3·4)의 입력.

    frozen이지만 계약 §8의 기계 검사는 `models.py` 전용이라 여기는 문서로만 선을
    긋는다. 실제로 쓴 `config`·`project_config`를 실어 돌려주는 이유: 소스 분석 키가
    설정 지문을 먹으므로, 다음 단계가 자기 config를 따로 읽으면 출판 자리·세그먼트
    신원·중복 제거가 어긋난다. `artifact_dir`이 source·boundary 네임스페이스의 뿌리다
    (speech·boundary가 이미 그 아래 애셋 디렉터리로 쓴다).
    """

    document: ProjectDocument
    media: tuple[ResolvedMedia, ...]
    audio_inputs: tuple[AudioSourceInput, ...]
    policy: AudioPolicy
    config: Config
    project_config: ProjectConfig
    artifact_dir: Path
    pcm_dir: Path
    frames_dir: Path
    inputs_by_resource: Mapping[str, AudioSourceInput]

    def __post_init__(self) -> None:
        """규칙 9를 타입으로 못 깨게 한다 — 이 저장소에서 `protected=[]` 계열 결함이
        여섯 번 재발했고, 전부 "없음"이 "제한 없음"으로 읽힌 것이다."""
        if len(self.audio_inputs) != len(self.document.resources):
            raise ValueError(
                f"audio_inputs({len(self.audio_inputs)})가 문서 리소스 수"
                f"({len(self.document.resources)})와 다르다 — 리소스는 조용히 사라지지 않는다"
            )
        for index, (media, _second) in enumerate(self.audio_inputs):
            expected = self.document.resources[index].scoped_id
            if media.resource_scoped_id != expected:
                raise ValueError(
                    f"audio_inputs[{index}]의 자원 신원이 문서 순서와 어긋난다: "
                    f"{media.resource_scoped_id!r} != {expected!r}"
                )


def pcm_destination(pcm_root: Path, asset_content_id: str) -> Path:
    """PCM 논리 캐시 주소 — 내용 신원으로 키잡는다(리소스 신원이 아니라).

    접는 것은 이 주소뿐이다: `_reusable_reference`가 이 포인터를 보고 재사용을
    정하므로, 같은 바이트를 가리키는 리소스 몇 개든 추출은 한 번이다.
    """
    return pcm_root / asset_content_id / _POINTER_NAME


def prepare_audio(
    project_path: Path,
    artifact_dir: Path,
    *,
    overrides: Sequence[MediaOverride] = (),
    policy: AudioPolicy | None = None,
    config: Config | None = None,
    project_config: ProjectConfig | None = None,
) -> PreparedAudio:
    """문서를 파싱해 미디어를 해석하고 PCM 참조를 만든다 — 모든 리소스가 쌍으로 돌아온다.

    쌍은 문서 리소스 순서 그대로다. 둘째 칸은 PCM 참조이거나 부재 기록이다 — 비는
    칸, 조용히 빠지는 리소스가 없다(규칙 9). 해석 불가능한 미디어는 준비 게이트가
    그대로 올려 보낸다. 오디오가 하나라도 있으면 도구 게이트가 먼저 선다.
    """
    used_policy = policy if policy is not None else AudioPolicy()
    document = parse_project(project_path)
    resolved = resolve_media(document, overrides)
    pcm_root, frames_root = _artifact_roots(artifact_dir)
    _require_audio_tools(resolved)
    audio_inputs = tuple(_audio_input(item, used_policy, pcm_root) for item in resolved)
    result = PreparedAudio(
        document=document,
        media=resolved,
        audio_inputs=audio_inputs,
        policy=used_policy,
        config=config if config is not None else Config(),
        project_config=project_config if project_config is not None else ProjectConfig(),
        artifact_dir=artifact_dir,
        pcm_dir=pcm_root,
        frames_dir=frames_root,
        inputs_by_resource={pair[0].resource_scoped_id: pair for pair in audio_inputs},
    )
    _log_summary(result)
    return result


def _artifact_roots(artifact_dir: Path) -> tuple[Path, Path]:
    """쓸 하위 네임스페이스를 미리 만들어 (PCM 뿌리, 프레임 뿌리)로 돌려준다."""
    pcm_root = artifact_dir / _PCM_DIR
    frames_root = artifact_dir / _FRAMES_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    pcm_root.mkdir(parents=True, exist_ok=True)
    frames_root.mkdir(parents=True, exist_ok=True)
    return pcm_root, frames_root


def _require_audio_tools(resolved: Sequence[ResolvedMedia]) -> None:
    """오디오를 저장할 때만 도구를 요구한다 — 없으면 한 번의 하드 실패로 끝난다.

    도구가 없는 것은 자원의 결함이 아니라 환경의 결함이다. 자원 게이트 안에서
    죽으면 부재 기록 N개로 번역되어 진단이 죽는다 — 게이트를 앞으로 뺀 것이 그
    길을 끊는다. 오디오가 하나도 없으면 묻지도 않는다.
    """
    if not any(item.has_audio for item in resolved):
        return
    require_ffmpeg("ffprobe")
    require_ffmpeg("ffmpeg")


def _audio_input(media: ResolvedMedia, policy: AudioPolicy, pcm_root: Path) -> AudioSourceInput:
    """한 리소스를 (미디어, PCM 참조 또는 부재 기록)으로 만든다 — 둘째 칸은 비지 않는다."""
    if not media.has_audio:
        # 선언 그대로다 — probe하지 않는다. has_audio은 그 리소스에서만 온다.
        return media, _declared_no_audio(media)
    try:
        stream = probe_audio_stream(media.original, policy.stream_index)
    except RuntimeError as exc:
        return media, _from_exception(media, "stream_unavailable", exc)
    log.debug(f"오디오 스트림 확인: {media.original.name} → 전역 {stream.stream_index}번")
    try:
        reference = build_reference(
            media.asset_content_id,
            media.original.stem,
            media.original,
            media.original_sha256,
            policy,
            pcm_destination(pcm_root, media.asset_content_id),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return media, _from_exception(media, "pcm_unavailable", exc)
    return media, reference


def _declared_no_audio(media: ResolvedMedia) -> UnavailableSourceAudio:
    """선언된 무음 — 실패가 아니라 사실이므로 warn이 아니라 info로 남긴다."""
    record = _record(
        media,
        "no_audio",
        "declared_no_audio",
        "리소스가 hasAudio=0으로 선언했다 — 선언을 그대로 옮긴다",
    )
    log.info(f"선언된 무음: {media.resource_scoped_id}")
    return record


def _from_exception(
    media: ResolvedMedia,
    reason_code: Literal["stream_unavailable", "pcm_unavailable"],
    exc: BaseException,
) -> UnavailableSourceAudio:
    """실패의 기록 — reason에는 실제 예외 메시지를 실어 진단을 살린다."""
    reason = " ".join(str(exc).split()) or exc.__class__.__name__
    record = _record(media, "error", reason_code, reason)
    log.warn(f"소스 오디오 부재: {media.resource_scoped_id} — {reason_code}: {reason}")
    return record


def _record(
    media: ResolvedMedia,
    status: Literal["no_audio", "error"],
    reason_code: Literal["declared_no_audio", "stream_unavailable", "pcm_unavailable"],
    reason: str,
) -> UnavailableSourceAudio:
    """부재 기록의 조립 — 신원은 미디어에서 그대로 옮기고 지어내지 않는다."""
    return UnavailableSourceAudio(
        variant="unavailable_source_audio",
        resource_scoped_id=media.resource_scoped_id,
        source_id=media.asset_content_id,
        original_sha256=media.original_sha256,
        status=status,
        reason_code=reason_code,
        reason=reason,
    )


def _log_summary(result: PreparedAudio) -> None:
    """단계의 결산 — 부재도 세어 보고한다(있는 것만 세면 조용한 손실이 된다)."""
    unavailable = sum(
        1 for _media, second in result.audio_inputs if isinstance(second, UnavailableSourceAudio)
    )
    log.info(
        f"오디오 준비 완료: 리소스 {len(result.audio_inputs)}개 — "
        f"PCM {len(result.audio_inputs) - unavailable}, 부재 {unavailable}"
    )
