"""비전 프레임 샘플 — 원본 증명 위의 프록시 우선 추출 (P5-04).

이 모듈은 **받은 시각 목록을 이미지로 바꾸는** 것만 한다. 어떤 시각을 뽑을지
고르는 것(P6 블록 분할)도 뽑은 이미지를 재는 것(P5-05 비전 정책)도 여기 없다.
probe와 추출 subprocess는 `vlogsrt.preprocess`(불변 규칙 7)가 유일한 경계다 —
이 모듈은 순수한 대응·매핑·중복 처리만 한다. 지키는 선:

- **짝은 연결로만 선다.** 원본/프록시 짝은 `ResolvedMedia`가 싣는 미디어-rep
  연결뿐이다. 파일 이름이 비슷하다고 짝으로 인정하는 길은 없다(P1-03).
- **원본 증명이 먼저고 예외다.** 원본 그리드가 증명되지 않으면 그 미디어의
  프레임 시계는 없다 — `FrameSamplingError`로 닫히고 프록시 폴백은 무효
  요청을 구하지 못한다. 프록시 증명 실패는 닫히지 않는다: 증명된 원본으로
  돌아가고 안정 사유를 남긴다.
- **매핑은 정확 정수 하나.** `original_frame_index = (source_time −
  original.presentation_origin) / frame_duration`이 `[0, frame_count)`의 정확한
  정수여야 한다. 격자 밖·범위 밖은 명시적 오류이다. `asset.start` 같은 XML
  애셋 시계 변환은 이 층의 입력이 아니다 — P6가 소스 격자 인덱스로
  `source_time`을 만들어 들고 온다. 요청-원본의 정수 검사가 곧 매핑의
  정본이라서, 0이 아닌 원점을 통째로 거부하는 규칙은 따로 없다.
- **프록시 호환은 보수적으로.** 프록시는 원본과 같은 정확 프레임 길이와 같은
  프레임 수를 **둘 다** 요구한다 — 하나라도 어긋나면 오프셋을 추측하지 않고
  원본을 뽑는다. 서로 다른/0이 아닌 PTS 원점 자체는 거부 대상이 아니다:
  같은 디코드 인덱스를 프록시 **자기 원점**에 얹을 뿐이다.
- **결정성.** 같은 요청은 같은 순서·같은 파일 이름·같은 바이트를 낸다.
  `(source_id, source_time)`이 같은 요청은 이미지 파일 하나를 공유한다.

이미지는 장면 품질의 증거가 아니다 — 이 과제는 움직임·블러·노출을 추론하지
않고 HDR/Log 색을 매기지 않는다. 시각·개수 검사는 대응 관계의 증명이지 프록시
화소가 원본과 의미까지 같다는 증명도 아니다(브리프).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vlogsrt import log
from vlogsrt.preprocess import VideoStreamInfo, extract_frame_index, probe_video_stream
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.media import ResolvedMedia, sha256_file

_VIDEO_ORDINAL = 0
"""프로브가 고르는 비디오 순번 — FCP 내보내기는 비디오 스트림 하나다(지원 부분집합)."""

# 프록시 폴백 사유의 안정 코드 — review.json 에 그대로 실리므로 여기서 얼어 있다.
_PROXY_UNPROVEN = "proxy_unproven"
_PROXY_DURATION_MISMATCH = "proxy_frame_duration_mismatch"
_PROXY_COUNT_MISMATCH = "proxy_frame_count_mismatch"


class FrameSamplingError(RuntimeError):
    """프레임 샘플이 나오기 전에 닫힌 상황 — 메시지가 프레임·좌표를 말한다."""


@dataclass(frozen=True, slots=True)
class FrameCorrespondence:
    """원본/프록시 대응의 증명 결과.

    `proxy`는 프로브에 성공한 프록시의 시계다 — 호환 검사에서 밀려도 남는다
    (무엇이 어긋났는지는 `review`가 말한다). 프로브 자체가 실패했거나
    `ResolvedMedia`가 프록시를 싣지 않았으면 None이다. `proxy_usable`이 참이라는
    것은 둘 다 균일 CFR로 증명됐고 정확 프레임 길이·프레임 수가 같다는 뜻이다.
    """

    original: VideoStreamInfo
    proxy: VideoStreamInfo | None
    proxy_usable: bool
    review: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrameSampleRequest:
    """프레임 한 장 요청.

    P6가 소스 격자 인덱스 n으로 `source_time = n × frame_duration`를 만든다 —
    n은 디코드 영상 순번이 아니고 `asset.start`는 여기 오지 않는다.
    """

    frame_id: str
    source_id: str
    source_time: Fraction


@dataclass(frozen=True, slots=True)
class FrameSample:
    """뽑힌 프레임 한 장 — 어디서(by) 몇 번째(index)로 뽑았는지가 다 남는다.

    `original_frame_index`는 원본 매핑의 정본이고 `selected_frame_index`는 실제로
    뽑은 스트림 안의 같은 인덱스다(프록시는 같은 인덱스로 대응된다).
    `selected_presentation_time`은 **고른 스트림**의 실측 원점에 인덱스를 얹은
    PTS다. `review`는 이 장이 프록시 대신 원본에서 나온 이유다 — 프록시를 쓴
    장과 프록시가 애초에 없던 장은 빈 튜플이다.
    """

    frame_id: str
    source_id: str
    source_time: Fraction
    image_path: Path
    image_sha256: str
    used_proxy: bool
    original_frame_index: int
    selected_frame_index: int
    selected_presentation_time: Fraction
    review: tuple[str, ...]


def establish_correspondence(media: ResolvedMedia) -> FrameCorrespondence:
    """원본을 먼저 증명하고, 프록시는 호환일 때만 대응으로 인정한다.

    원본 증명 실패는 `FrameSamplingError`로 닫힌다 — 프록시가 멀쩡해도 원본이
    증명되지 않으면 이 미디어의 프레임 시계는 없다. 프록시 증명 실패(읽기·
    메타데이터 누락·비균일 타임스탬프 전부)는 폴백이다: 원본은 이미 증명됐다.
    """
    original = _probe_original(media)
    if not media.used_proxy:
        return FrameCorrespondence(original=original, proxy=None, proxy_usable=False, review=())
    try:
        proxy = probe_video_stream(media.image_source, _VIDEO_ORDINAL)
    except RuntimeError as exc:
        log.debug(f"프록시 그리드 증명 실패 — 원본으로 폴백한다: {media.image_source} ({exc})")
        return FrameCorrespondence(
            original=original, proxy=None, proxy_usable=False, review=(_PROXY_UNPROVEN,)
        )
    review = _proxy_incompatibility(original, proxy)
    return FrameCorrespondence(
        original=original, proxy=proxy, proxy_usable=not review, review=review
    )


def sample_frames(
    media: ResolvedMedia,
    requests: Sequence[FrameSampleRequest],
    directory: Path,
    cfg: ProjectConfig,
) -> tuple[FrameSample, ...]:
    """요청받은 시각들을 프록시 우선으로 이미지로 만든다 — 프레임 아이디마다 한 장.

    검사 순서가 계약이다: 요청의 신원·타입 → 원본 증명 → 시각 매핑 → 추출.
    어느 단계에서 닫히든 추출은 한 번도 일어나지 않는다.
    """
    _require_requests(media, requests)
    correspondence = establish_correspondence(media)
    selection = _select_source(media, correspondence)
    planned = _plan_samples(requests, correspondence.original)
    return _extract_samples(planned, selection, directory, cfg)


# ─────────────────────────────────────────────────────────────
#  대응 — 원본 증명과 프록시 호환
# ─────────────────────────────────────────────────────────────


def _probe_original(media: ResolvedMedia) -> VideoStreamInfo:
    """원본 증명 — 실패는 이 미디어의 분석 오류다. 폴백이 구할 수 없다."""
    try:
        return probe_video_stream(media.original, _VIDEO_ORDINAL)
    except RuntimeError as exc:
        raise FrameSamplingError(
            f"원본 비디오 그리드 증명에 실패했다 — 프록시 폴백은 이걸 구하지 못한다: "
            f"{media.original} ({exc})"
        ) from exc


def _proxy_incompatibility(original: VideoStreamInfo, proxy: VideoStreamInfo) -> tuple[str, ...]:
    """프록시가 원본과 같은 CFR 격자를 공유하는지 본다 — 오프셋 추측은 없다."""
    reasons: list[str] = []
    if proxy.frame_duration != original.frame_duration:
        reasons.append(_PROXY_DURATION_MISMATCH)
    if proxy.frame_count != original.frame_count:
        reasons.append(_PROXY_COUNT_MISMATCH)
    return tuple(reasons)


@dataclass(frozen=True, slots=True)
class _Selection:
    """실제로 뽑을 소스 — 경로·시계·프록시 사용 여부·폴백 사유."""

    path: Path
    stream: VideoStreamInfo
    used_proxy: bool
    review: tuple[str, ...]


def _select_source(media: ResolvedMedia, correspondence: FrameCorrespondence) -> _Selection:
    proxy = correspondence.proxy
    if correspondence.proxy_usable and proxy is not None:
        return _Selection(media.image_source, proxy, True, ())
    return _Selection(media.original, correspondence.original, False, correspondence.review)


# ─────────────────────────────────────────────────────────────
#  계획 — 신원·매핑·중복
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Planned:
    """중복이 정리된 요청 하나 — 원본 인덱스까지 계산이 끝난 상태."""

    frame_id: str
    source_id: str
    source_time: Fraction
    original_frame_index: int


def _require_requests(media: ResolvedMedia, requests: Sequence[FrameSampleRequest]) -> None:
    """요청의 신원·타입을 먼저 본다 — 프로브보다 앞선다(위조는 즉시 닫는다)."""
    for request in requests:
        _require_text(request.frame_id, f"frame_id ({request.frame_id!r})")
        _require_text(request.source_id, f"source_id ({request.source_id!r})")
        if not isinstance(request.source_time, Fraction):
            raise FrameSamplingError(
                f"source_time은 정확한 Fraction이어야 한다 — frame_id {request.frame_id!r},"
                f" 값 {request.source_time!r} (float는 이 계층에 들어오지 못한다)"
            )
        if request.source_id != media.asset_content_id:
            raise FrameSamplingError(
                f"요청 source_id가 애셋 내용 신원과 다르다 — frame_id {request.frame_id!r},"
                f" 요청 {request.source_id!r}, 애셋 {media.asset_content_id!r}"
            )


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FrameSamplingError(f"{label}는 비지 않은 문자열이어야 한다: {value!r}")


def _plan_samples(
    requests: Sequence[FrameSampleRequest], original: VideoStreamInfo
) -> tuple[_Planned, ...]:
    """중복을 정리하고 원본 인덱스를 계산한다 — 처음 본 순서가 곧 결과 순서다.

    같은 frame_id가 같은 시각으로 두 번 오면 한 장으로 접고, 다른 시각으로 오면
    거부한다. 같은 시각의 **다른** frame_id는 각자 결과를 받되 추출을 공유한다.
    """
    seen: dict[str, Fraction] = {}
    planned: list[_Planned] = []
    for request in requests:
        if request.frame_id in seen:
            if seen[request.frame_id] != request.source_time:
                raise FrameSamplingError(
                    f"frame_id {request.frame_id!r}가 서로 다른 시각으로 두 번 왔다: "
                    f"{seen[request.frame_id]}초와 {request.source_time}초"
                    " — 같은 아이디는 같은 시각이어야 한다"
                )
            continue
        seen[request.frame_id] = request.source_time
        planned.append(
            _Planned(
                frame_id=request.frame_id,
                source_id=request.source_id,
                source_time=request.source_time,
                original_frame_index=_original_index(request, original),
            )
        )
    return tuple(planned)


def _original_index(request: FrameSampleRequest, original: VideoStreamInfo) -> int:
    """`(source_time − 원점) / 프레임 길이` — 정확한 정수여야 한다. 반올림은 없다."""
    frames = (request.source_time - original.presentation_origin) / original.frame_duration
    if frames.denominator != 1:
        raise FrameSamplingError(
            f"요청 시각이 원본 프레임 격자 밖이다 — frame_id {request.frame_id!r},"
            f" 시각 {request.source_time}초, 원점 {original.presentation_origin}초,"
            f" 프레임 길이 {original.frame_duration}초"
            f" (프레임 좌표 {frames})"
        )
    index = frames.numerator
    if not 0 <= index < original.frame_count:
        raise FrameSamplingError(
            f"요청 프레임이 원본 범위 밖이다 — frame_id {request.frame_id!r},"
            f" 인덱스 {index}, 범위 [0, {original.frame_count})"
        )
    return index


# ─────────────────────────────────────────────────────────────
#  추출 — 같은 시각은 파일 하나를 공유한다
# ─────────────────────────────────────────────────────────────


def _extract_samples(
    planned: Sequence[_Planned], selection: _Selection, directory: Path, cfg: ProjectConfig
) -> tuple[FrameSample, ...]:
    """계획을 이미지로 바꾼다 — `(source_id, source_time)`마다 추출은 한 번이다."""
    if not planned:
        return ()
    directory.mkdir(parents=True, exist_ok=True)
    artifacts: dict[tuple[str, Fraction], tuple[Path, str]] = {}
    samples: list[FrameSample] = []
    for item in planned:
        key = (item.source_id, item.source_time)
        artifact = artifacts.get(key)
        if artifact is None:
            artifact = _extract_one(item, selection, directory / _image_name(*key), cfg)
            artifacts[key] = artifact
        path, digest = artifact
        samples.append(
            FrameSample(
                frame_id=item.frame_id,
                source_id=item.source_id,
                source_time=item.source_time,
                image_path=path,
                image_sha256=digest,
                used_proxy=selection.used_proxy,
                original_frame_index=item.original_frame_index,
                selected_frame_index=item.original_frame_index,
                selected_presentation_time=(
                    selection.stream.presentation_origin
                    + item.original_frame_index * selection.stream.frame_duration
                ),
                review=selection.review,
            )
        )
    return tuple(samples)


def _extract_one(
    item: _Planned, selection: _Selection, destination: Path, cfg: ProjectConfig
) -> tuple[Path, str]:
    """한 장을 뽑아 해시까지 매긴다 — 추출 실패는 닫는다(부분 성공이 없다)."""
    try:
        path = extract_frame_index(
            selection.path,
            selection.stream.stream_index,
            item.original_frame_index,
            destination,
            long_edge=cfg.vision_long_edge,
        )
    except RuntimeError as exc:
        raise FrameSamplingError(
            f"프레임 추출이 실패했다 — frame_id {item.frame_id!r},"
            f" {selection.path} 프레임 {item.original_frame_index} ({exc})"
        ) from exc
    return path, sha256_file(path)


def _image_name(source_id: str, source_time: Fraction) -> str:
    """이미지 파일 이름 — (source_id, source_time)에서만 온다.

    요청 순서나 실행 시각이 이름에 들어가지 않으므로 같은 요청은 언제나 같은
    이름이고, 같은 시각의 요청들은 파일 하나를 공유한다.
    """
    digest = hashlib.sha256(
        f"{source_id}\0{source_time.numerator}/{source_time.denominator}".encode()
    ).hexdigest()
    return f"frame-{digest[:32]}.jpg"
