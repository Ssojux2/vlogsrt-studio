"""V1 영상 우선 검토의 백엔드 — 읽기 질의·미리보기 준비·리비전 묶음 편집.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §4.3·§10·§12(화면 2)·§14.2.
화면이 아직 없는 단계의 **백엔드 계약**이다. 셋으로 갈라져 있다:

- **질의는 읽기다.** 검토 리비전 위의 조각·시계·간격·등록 ID를 값으로 돌릴 뿐,
  미디어를 만들지도 유료 제공자를 부르지도 않는다. ffmpeg 실행(버전 조사 포함)은
  질의 길에 없다 — 상태는 기록 파일·등록 행·활성 작업 표에서 유도한다.
  원본의 소리 트랙 선택은 질의·준비 모두 원본별 **서수**로 명시적으로 받는다
  — 선택을 안 준 원본은 기본(첫 트랙, 없으면 침묵)이고, 값을 `None`로 준 원본은
  트랙이 있어도 명시적 침묵이다. 트랙 목록은 준비가 조사해 남긴 기록에서 온다.
- **준비만 만든다.** `run_page_preparation`이 원본을 읽어 `previews/visual/`에
  브라우저 MP4(원본 전체)와 조각 가운데 프레임 썸네일을 쓴다. 캐시 키는
  **원본 해시 + 영상/소리 스트림 선택 + 변환 조리법**이고(§14.2), 한 페이지
  (24개)의 **없는 항목만** 순서대로 만든다 — 이미 있는 것은 ffmpeg을 돌리지
  않는다. 원본·package·확정본은 전혀 손대지 않는다.
- **편집은 검토 파사드를 탄다.** 화면이 준 `expected_revision_id`가 지금 헤드와
  어긋나면 `StaleRevisionError`로 거부하고(stale), 길이 0·맞닿지 않는 이웃의
  경계 이동은 빠르게 거부한다. 출판은 파사드의 원자적 트랜잭션 그대로다.
  제안·질의·준비도 같은 리비전 묶음을 받는다 — 머리가 앞서 갔으면 **살아남은
  조각**의 제안도 stale로 거부하고, 새 머리의 저장 토큰을 몰래 발급하지 않는다.

상태는 다섯 값이다(§14.1 — 조용한 빈 성공이 없다):

- `ready` — 파일·기록·등록이 짝을 이룬다.
- `preparing` — 준비 기록이 남았고 활성 작업이 있다.
- `failed` — 마지막 시도의 실패 기록이 있다(다음 준비가 다시 시도한다).
- `stale` — 한 번 만들어졌지만 지금은 못 쓴다(정리 뒤 파일 소실·깨진 짝).
- `missing` — 한 번도 만든 적이 없다.

캐시 키에 ffmpeg 바이너리 판을 넣지 않는다 — 키 재료는 **요청의 동일성**이다
(원본 해시·스트림 서수·조리법 판). 도구가 바뀌어 같은 요청의 결과물이 달라지면
조리법 판을 올려 키를 갈아야 한다(자막 미리보기와 다른 선택 — 이유는 키가
질의에서 **도구 조사 없이** 재계산되어야 하기 때문이다). 도구 판은 기록 파일에
증거로 남는다. 예외 하나(D62): 미리보기를 검토용 크기로 줄인 조리법은 판을 올리지 않았다 —
이미 만든 4K 완성본을 그대로 쓰려고다. 미리보기는 표시용이라 바이트에 기대는 곳이 없고, 새로
만든 기록만 `encoding`에 조리법 이름을 남긴다.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from vlogsrt import log, oscompat, preprocess
from vlogsrt.oscompat import process_alive, publish_exclusive
from vlogsrt.project.raw import read_scope, source_verify
from vlogsrt.project.raw.analysis_config import REVIEW_MEDIA
from vlogsrt.project.raw.config import FOLDER_WORKSPACE
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext
from vlogsrt.project.raw.media_refs import ORIGINAL_ONLY_FINGERPRINT
from vlogsrt.project.raw.models import (
    ACTIVE_JOB_STATUSES,
    VISUAL_PREPARE_JOB_KIND,
    JobRecord,
    MediaRegistration,
    RawWorkspaceError,
)
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    RANGE_MARKS,
    SELECTIONS,
    ReviewEdit,
    ReviewRevision,
    ReviewSegment,
    SourceClock,
    StaleRevisionError,
    boundary_edit,
    chronological,
    is_unreviewed_auto_exclusion,
    merge_edit,
    range_edit,
    range_segment,
    selection_edit,
    split_edit,
)
from vlogsrt.project.raw.workspace import RawWorkspace

__all__ = [
    "REPRESENTATIVE_THUMBS_PREFIX",
    "VISUAL_INVENTORY_RECIPE",
    "VISUAL_PAGE_SIZE",
    "VISUAL_PREVIEW_RECIPE",
    "VISUAL_STATES",
    "VISUAL_THUMB_RECIPE",
    "VisualAudioTrack",
    "VisualClockSummary",
    "VisualNeighbor",
    "VisualRangeMark",
    "VisualReviewError",
    "VisualReviewOverview",
    "VisualSegmentCard",
    "VisualSelectedSegment",
    "VisualSnapProposal",
    "VisualSourcePreview",
    "adjust_boundary",
    "audio_selection_material",
    "boundary_proposal",
    "mark_range",
    "merge_segments",
    "middle_frame_index",
    "preparation_fingerprint",
    "preview_media_key",
    "query_overview",
    "representative_thumbs_fingerprint",
    "representative_thumbs_missing",
    "require_head_revision",
    "run_page_preparation",
    "run_representative_thumbnails",
    "run_source_preparation",
    "set_selection",
    "snap_proposal",
    "snapped_frame_index",
    "source_preparation_fingerprint",
    "source_preparation_missing",
    "split_segment",
    "thumbnail_media_key",
    "validated_audio_selections",
]

VISUAL_PAGE_SIZE = 24
"""한 페이지에 보이는 조각 수 — 준비 작업도 이 페이지의 썸네일만 만든다."""

VISUAL_PREVIEW_ROOT = "previews/visual"
"""영상 검토 미리보기의 자리 — `previews/`는 정리 대상이라 재생성이 원칙이다(§9.2)."""

VISUAL_THUMB_DIRNAME = "thumbs"
"""썸네일의 자리 — 미리보기 MP4와 같은 `previews/visual/` 아래 한 단계다."""

VISUAL_PREVIEW_RECIPE = "visual-review-preview/v1"
"""원본 전체 미리보기의 조리법 판 — 키 재료가 바뀌면 이 값을 올린다.

D62의 검토용 크기(`REVIEW_MEDIA`)로는 올리지 않았다 — 이미 만든 미리보기를 다시 만들지 않는다.
"""

VISUAL_THUMB_RECIPE = "visual-review-thumb/v1"
"""썸네일의 조리법 판 — long_edge·JPEG 설정이 바뀌면 이 값을 올린다."""

VISUAL_INVENTORY_RECIPE = "visual-review-inventory/v1"
"""원본 소리 트랙 목록 기록의 조리법 판 — 준비가 조사해 쓰고 질의가 읽기만 한다."""

VISUAL_THUMB_LONG_EDGE = 480
"""썸네일 긴 변 한도 — 조판 대상이 아니라 목록 표시용 크기다(조리법 판과 짝이다)."""

VISUAL_VIDEO_ORDINAL = 0
"""미리보기가 담는 영상 스트림 — 첫 번째 영상 하나뿐이다(지원 부분집합)."""

VISUAL_CLAIM_DIRNAME = "claims"
"""준비 작업의 맡음 표시 자리 — `previews/visual/claims/<원본 해시>.<작업 번호>.json`(D62)."""

_JOB_ID = re.compile(r"[A-Za-z0-9_-]+")
_LEFTOVER = re.compile(
    r"\.(?P<name>.+)\.(?P<pid>[0-9]+)\.[0-9a-f]{8}\.(?:part|partial|partial\.jpg)"
)
"""끊긴 만들기의 임시 파일 — `.<최종 이름>.<pid>.<토큰>.part`(미리보기)·`.partial.jpg`(썸네일)·
`.partial`(기록). 이름에 박힌 pid로 살아 있는 만들기의 것을 가른다."""

VISUAL_STATES: tuple[str, ...] = ("preparing", "ready", "failed", "stale", "missing")
"""미리보기 항목의 상태 — 위 다섯 값뿐이다(문서 머리글의 정의)."""


class VisualReviewError(RawWorkspaceError):
    """영상 검토 계약의 오류 — 이유를 사람에게 그대로 보여준다."""


# ── 얼어 붙은 기록 — 질의가 돌려주는 값 ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VisualClockSummary:
    """선택 조각의 원본 시계 — 정수 PTS·timebase·프레임 격자를 그대로 실었다(§13.3)."""

    source_id: str
    source_sha256: str
    time_base_num: int
    time_base_den: int
    origin_pts: int
    end_pts: int
    frame_ticks: int

    @property
    def time_base(self) -> Fraction:
        return Fraction(self.time_base_num, self.time_base_den)


@dataclass(frozen=True, slots=True)
class VisualNeighbor:
    """같은 원본의 맞닿은 오른쪽 이웃 — 병합·경계 조정 안내의 재료(§12 화면 2)."""

    segment_id: str
    source_id: str
    start_pts: int
    end_pts: int


@dataclass(frozen=True, slots=True)
class VisualSegmentCard:
    """목록 한 칸 — 클라이언트가 페이지를 다루는 데 필요한 값을 전부 실는다.

    선택·탐색·반복은 화면이 이 값만으로 한다(코디네이터 계약 — API 호출도 생성도
    없다). `clock`은 원본 시계의 격자·유효 범위(프레임 격자 + 원점·끝)이고,
    `preview_seek_seconds`는 잰 미리보기 원점(출력 영상 첫 PTS)을 **더한** 재생
    위치다(0 가정 없음, G3).
    """

    segment_id: str
    source_id: str
    source_order: int
    segment_order: int
    start_pts: int
    end_pts: int
    duration_seconds: str
    selection: str
    recommendation: str
    speech_status: str
    notes: str
    clock: VisualClockSummary
    snapped_frame_index: int
    snapped_pts: int
    snapped_seconds: str
    preview_registered_file_id: str | None
    preview_seek_seconds: str | None
    thumb_state: str
    thumb_registered_file_id: str | None
    thumb_relative_path: str | None
    right_neighbor: VisualNeighbor | None


@dataclass(frozen=True, slots=True)
class VisualSelectedSegment:
    """선택 조각의 자세한 기록 — 시계·간격·미리보기 등록까지 화면이 필요한 전부."""

    segment_id: str
    source_id: str
    source_order: int
    segment_order: int
    start_pts: int
    end_pts: int
    duration_seconds: str
    selection: str
    recommendation: str
    override_recommendation: str | None
    speech_status: str
    notes: str
    clock: VisualClockSummary
    snapped_frame_index: int
    snapped_pts: int
    snapped_seconds: str
    preview_registered_file_id: str | None
    preview_seek_seconds: str | None
    thumb_state: str
    thumb_registered_file_id: str | None
    thumb_relative_path: str | None
    right_neighbor: VisualNeighbor | None


@dataclass(frozen=True, slots=True)
class VisualAudioTrack:
    """원본이 가진 소리 트랙 하나 — 준비가 조사해 남긴 목록 기록에서 온다.

    `ordinal`이 이 계층의 선택 어휘(원본 안에서 몇 번째 트랙)이고
    `stream_index`는 ffmpeg 매핑이 쓰는 전역 번호다. 질의는 이 값을 기록에서만
    읽는다 — 첫 준비 전에는 목록이 비어 있다(조사하지 않고 지어내지 않는다).
    """

    ordinal: int
    stream_index: int
    codec_name: str
    channels: int | None
    sample_rate: int | None


@dataclass(frozen=True, slots=True)
class VisualSourcePreview:
    """원본 하나의 전체 미리보기 상태 — 등록 ID와 측정 기하·시각 원점까지 실는다."""

    source_id: str
    source_sha256: str
    state: str
    audio_ordinal: int | None
    audio_stream_index: int | None
    video_stream_index: int | None
    registered_file_id: str | None
    relative_path: str | None
    duration_seconds: str | None
    browser_origin_seconds: str | None
    width: int | None
    height: int | None
    rotation_degrees: int | None
    remuxed: bool | None
    error: str | None
    audio_origin_seconds: str | None = None
    audio_tracks: tuple[VisualAudioTrack, ...] = ()


@dataclass(frozen=True, slots=True)
class VisualReviewOverview:
    """영상 검토 질의의 결과 — 리비전·페이지·선택·원본 미리보기의 묶음."""

    workspace_id: str
    base_plan_id: str
    revision_id: str
    sequence: int
    stale_sources: tuple[tuple[str, str], ...]
    page: int
    page_count: int
    total_segments: int
    segments: tuple[VisualSegmentCard, ...]
    selected: VisualSelectedSegment | None
    sources: tuple[VisualSourcePreview, ...]
    active_preparation_jobs: tuple[str, ...]
    representatives: tuple[VisualSegmentCard, ...] = ()
    """영상마다 대표 컷 하나 — 쪽과 무관하다(D64). `include_all_sources`일 때만 채운다.

    영상 카드는 이 칸을 그린다. 예전에는 지금 쪽(24컷)에 컷이 없는 영상이 지어낸 「아직 없음」
    카드였다 — 썸네일이 있어도 쪽 밖이면 없다고 보였다. 대표는 그 영상에서 필터에 맞는 첫 컷,
    맞는 컷이 없으면 첫 컷이다(카드를 누르면 그 컷이 골라진다).
    """
    view_segments: tuple[VisualSegmentCard, ...] = ()
    """고른 영상의 컷 가운데 쪽 목록(`segments`)에 없는 것 — 쪽·필터와 무관하다(D64).

    쪽 넘김이 없어 쪽은 늘 1이다. 레일 구간의 썸네일과 화면의 컷 찾기(`findCut`)가 쪽 목록만
    보면 첫 쪽(24컷) 밖 영상의 컷은 썸네일도 범위도 없었다. 고른 영상 하나만 싣는다(모든 영상의
    모든 컷이 아니다 — 값을 작게 둔다). `include_all_sources`일 때만 채운다.
    """
    representative_thumbs: tuple[str | None, ...] = ()
    """영상 카드의 그림 — `representatives`와 같은 차례의 썸네일 등록 ID(W-24 세션 I).

    대표 컷의 썸네일이 그릴 수 있으면(파일과 등록 행이 다 있다) 그것, 아니면 그 영상의 컷 가운데
    필터와 무관하게 처음 그릴 수 있는 것, 하나도 없으면 None이다. 카드가 고르는 컷(선택·필터의
    뜻, D64)은 그대로이고 그림만 이 값이다 — 대표 컷의 썸네일이 지워진 영상이 다른 컷의 썸네일이
    있는데도 「낡음」으로 보였다.
    """


@dataclass(frozen=True, slots=True)
class VisualSnapProposal:
    """저장 전의 읽기 전용 스냅 제안 — 서버가 잰 점과 판정만 돌려준다(쓰기 0).

    화면은 원하는 시각을 주고 이 값을 받아 검토자에게 보여준 뒤, 저장을 원하면
    `head_revision_id`를 `expected_revision_id`로, `snap_pts`를 편집 점으로 쓴다
    (코디네이터 계약 — 제안이 저장에 앞서고, 제안은 아무 것도 바꾸지 않는다).
    `valid`가 거짓이면 `reason`이 길이 0·안 닿은 이웃·격자 밖 중 어느 것인지 말한다.
    원하는 점(`desired_pts`)은 **서브틱 유리수까지 정확히** 받아 그대로 돌려주고
    (`desired_seconds`는 원점 기준 정확 초), 정수 프레임 PTS는 스냅 결과(`snap_pts`)에만
    있다 — 스냅 전에 틱을 내림·반올림하는 일은 없다(§8.3).
    """

    kind: str
    segment_ids: tuple[str, ...]
    desired_pts: Fraction
    desired_seconds: str
    snap_pts: int
    snap_seconds: str
    head_revision_id: str
    valid: bool
    reason: str | None


# ── 순수 계산 — 프레임 스냅과 캐시 키 ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VisualRangeMark:
    """범위 표시를 저장한 결과(D100 §4) — 화면이 다시 그릴 새 머리의 그 원본 조각들.

    `start_pts`·`end_pts`는 저장한 범위(프레임 격자 위, 초로 받았으면 원본 시계로 스냅한 값),
    `marked_segment_id`는 그 범위를 덮는 조각 하나, `segments`는 새 머리에서 그 원본의 조각을
    시간순으로 모두 싣는다. 다음 편집은 `head_revision_id`를 `expected_revision_id`로 쓴다.
    """

    revision: ReviewRevision
    head_revision_id: str
    source_id: str
    start_pts: int
    end_pts: int
    mark: str
    marked_segment_id: str
    segments: tuple[ReviewSegment, ...]


def snapped_frame_index(clock: SourceClock, pts: Fraction | int) -> int:
    """`pts`에 가장 가까운 프레임 번호 — 정확히 한가운데면 **앞장**을 고른다.

    프레임 좌표 `x = (pts − 원점) / 프레임 틱`은 정확한 유리수다. `pts`는 서브틱
    유리수(`Fraction`)까지 받는다 — 절반 틱이 두 프레임의 정확히 한가운데인 격자
    (NTSC 계열)도 정확히 판정한다. 스냅은 `ceil(x − ½)` — 반올림 오차가 없고,
    x가 두 프레임의 정확히 한가운데(k+½)일 때 앞장(k)이 된다. float는 아예 받지
    않는다(§8.3).
    """
    frames = (Fraction(pts) - clock.origin_pts) / clock.frame_ticks
    return math.ceil(frames - Fraction(1, 2))


def middle_frame_index(clock: SourceClock, start_pts: int, end_pts: int) -> int:
    """조각 `[start_pts, end_pts)`의 가운데 프레임 — 스냅 결과를 조각 안으로 묶는다.

    가운데점은 정확한 유리수 `(start + end) / 2`로 잰다 — 틱을 먼저 내림하지
    않는다(정수 프레임으로의 환원은 스냅이 최종 경계에서 한 번만 한다). 스냅
    결과는 계산상 언제나 조각 안이지만, 호출자가 준 범위가 물러난 경우에도
    **존재하는 프레임**만 고른다('가운데 유효 프레임'). 범위가 비었거나 뒤집혔으면
    거부다 — 조각 계약(§6.1)이 이미 막는 모양이다.
    """
    first = (start_pts - clock.origin_pts) // clock.frame_ticks
    after_last = (end_pts - clock.origin_pts) // clock.frame_ticks
    if first >= after_last:
        raise VisualReviewError(
            f"조각 범위가 비었거나 뒤집혔다: [{start_pts}, {end_pts}) — 가운데 프레임이 없다"
        )
    middle = (Fraction(start_pts) + end_pts) / 2
    snapped = snapped_frame_index(clock, middle)
    return min(max(snapped, first), after_last - 1)


def _exact_pts(value: Fraction | int, what: str) -> Fraction:
    """원하는 점의 정확한 PTS — `int`·`Fraction`만 받고 그 밖은 거부한다.

    float는 이진 근사라 서브틱을 지운다(무한·NaN 포함) — 정확한 계약의 재료가
    될 수 없으니 받는 순간 거부다. bool은 파이썬에서 int라 언제나 먼저 걸러낸다.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Fraction)):
        raise VisualReviewError(
            f"{what}은(는) 정확한 정수나 Fraction이어야 한다: {value!r} — "
            "float는 이진 근사라 서브틱을 지운다. 초로 가진 값이면 Fraction으로 바꿔 주세요"
        )
    return Fraction(value)


def preview_media_key(source_sha256: str, audio_ordinal: int | None) -> str:
    """원본 전체 미리보기의 캐시 키 — 원본 해시·스트림 선택·조리법(§14.2).

    소리 선택은 오디오 **서수**(원본 안에서 몇 번째 트랙)다 — 전역 스트림 번호는
    조사 없이는 알 수 없고, 서수가 이 계층의 선택 어휘다. `None`은 침묵(트랙 없음)
    또는 고르지 않음이다.
    """
    selection = "a-" if audio_ordinal is None else f"a{audio_ordinal}"
    material = f"{source_sha256}|v{VISUAL_VIDEO_ORDINAL}|{selection}|{VISUAL_PREVIEW_RECIPE}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def thumbnail_media_key(source_sha256: str, frame_index: int, *, mapped: bool = False) -> str:
    """썸네일의 캐시 키 — 원본 해시·영상 서수·프레임 번호·조리법(§14.2).

    `mapped`(map 시계, D70)면 프레임 번호는 슬롯이고 보이는 패킷은 맞춤 규칙이 고른다 — 규칙 판
    (`preprocess.GRID_CONFORM_TAG`)을 재료에 얹는다. 균일·snap은 예전 재료 그대로다.
    """
    material = (
        f"{source_sha256}|v{VISUAL_VIDEO_ORDINAL}|f{frame_index}"
        f"|{VISUAL_THUMB_RECIPE}|e{VISUAL_THUMB_LONG_EDGE}"
    )
    if mapped:
        material += f"|{preprocess.GRID_CONFORM_TAG}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _thumb_key(workspace: RawWorkspace, source_sha256: str, frame_index: int) -> str:
    """질의(읽기)가 쓰는 썸네일 키 — map 여부는 준비가 잰 조사 기록에서 읽는다(ffprobe 없음).

    기록이 없으면(첫 준비 전·정리 뒤) 맞춤 꼬리 없는 키다 — map 원본의 썸네일은 그 키로 만든
    적이 없으므로(D70 전에는 지원 밖) 없음으로 보이고, 다음 준비가 기록과 함께 다시 잇는다.
    """
    return thumbnail_media_key(
        source_sha256, frame_index, mapped=_inventory_mapped(workspace, source_sha256)
    )


def preparation_fingerprint(base_plan_id: str, page: int, selection_material: str = "") -> str:
    """준비 작업의 지문 — 같은 (계획, 페이지, 소리 선택)의 활성 작업은 하나로 묶는다(§14.1).

    소리 선택 재료가 다르면 지문이 다르다 — 서수 0을 준비하다가 서수 1 요청이
    같은 작업에 재결합하는 일이 없고, 각자의 캐시(키도 서수를 묶는다)를 채운다.
    """
    material = f"{VISUAL_PREVIEW_RECIPE}|{base_plan_id}|page:{page}|audio:{selection_material}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def source_preparation_fingerprint(
    base_plan_id: str, source_id: str, selection_material: str = ""
) -> str:
    """영상 하나 준비(D64)의 지문 — 같은 (계획, 영상, 소리 선택)의 활성 작업은 하나로 묶는다."""
    material = (
        f"{VISUAL_PREVIEW_RECIPE}|{base_plan_id}|source:{source_id}|audio:{selection_material}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def audio_selection_material(audio_selections: Mapping[str, int | None] | None) -> str:
    """지문에 박는 소리 선택 재료 — 원본별 서수를 정렬해 한 문장으로(멱등)."""
    if not audio_selections:
        return ""
    parts = [
        f"{source_id}={'a-' if ordinal is None else f'a{ordinal}'}"
        for source_id, ordinal in sorted(audio_selections.items())
    ]
    return "|".join(parts)


def validated_audio_selections(
    audio_selections: Mapping[str, int | None] | None,
) -> dict[str, int | None]:
    """공개 소리 선택의 검사 — 키는 원본 ID, 값은 서수(int ≥ 0) 또는 명시적 침묵 None.

    형이 틀린 선택은 제출 순간에 거부한다(작업을 만들지 않는다). 서수가 원본의
    트랙 수 안에 있는지는 조사가 필요하니 준비 작업이 잰 뒤 실패로 닫는다 —
    어느 쪽도 조용히 첫 트랙으로 환원하지 않는다.
    """
    if audio_selections is None:
        return {}
    validated: dict[str, int | None] = {}
    for source_id, ordinal in audio_selections.items():
        if not isinstance(source_id, str) or not source_id.strip():
            raise VisualReviewError(f"소리 선택의 키는 원본 ID 문자열이어야 한다: {source_id!r}")
        if ordinal is None:
            validated[source_id] = None
            continue
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise VisualReviewError(
                f"원본 {source_id!r}의 소리 서수가 잘못됐다: {ordinal!r} — "
                "0 이상의 정수 또는 명시적 침묵 None이어야 한다"
            )
        validated[source_id] = ordinal
    return validated


def _now() -> str:
    # 마이크로초까지 찍는다 — 초 해상도면 같은 초 안에 만든 두 기록(트록 전환 등)의
    # '가장 최근' 판정이 동률이 되어 선택 질의가 비결정적으로 갈린다(V2 작업대가
    # 이 최근 기록 표시에 기대므로 계약상 결정적이어야 한다).
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _page_of(position: int) -> int:
    """0起点 위치의 페이지 번호(1부터) — 페이지 크기는 얼어 있다."""
    return position // VISUAL_PAGE_SIZE + 1


# ── 기록 파일 — 원자적 쓰기와 관대한 읽기 ─────────────────────────────────────


def _write_record(target: Path, payload: dict[str, object]) -> None:
    """기록 JSON을 원자적으로 쓴다 — 임시 이름을 만들고 바꾼다(반쪽 기록이 안 남는다)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.partial")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    oscompat.replace(temp, target)


def _read_record(target: Path) -> dict[str, object] | None:
    """기록 JSON을 읽는다 — 없거나 깨졌으면 None(상태 유도가 조용히 넘어간다)."""
    if not target.is_file():
        return None
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _record_str(record: dict[str, object] | None, key: str) -> str | None:
    value = None if record is None else record.get(key)
    return value if isinstance(value, str) else None


def _record_int(record: dict[str, object] | None, key: str) -> int | None:
    value = None if record is None else record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _preview_relative(key: str) -> Path:
    return Path(f"{VISUAL_PREVIEW_ROOT}/{key}.mp4")


def _preview_record_relative(key: str) -> Path:
    return Path(f"{VISUAL_PREVIEW_ROOT}/{key}.json")


def _thumb_relative(key: str) -> Path:
    return Path(f"{VISUAL_PREVIEW_ROOT}/{VISUAL_THUMB_DIRNAME}/{key}.jpg")


def _thumb_failure_relative(key: str) -> Path:
    return Path(f"{VISUAL_PREVIEW_ROOT}/{VISUAL_THUMB_DIRNAME}/{key}.json")


def _inventory_relative(source_sha256: str) -> Path:
    return Path(f"{VISUAL_PREVIEW_ROOT}/inventory/{source_sha256}.json")


def _write_inventory(
    workspace: RawWorkspace,
    source_id: str,
    source_sha256: str,
    audio: Sequence[preprocess.RawAudioTrack],
    grid_conform: str | None = None,
) -> None:
    """원본의 소리 트랙 목록 기록 — 준비가 조사할 때마다 남긴다(질의는 읽기만).

    map 원본(D70)이면 맞춤 규칙 판(`grid_conform`)도 남긴다 — 질의가 ffprobe 없이 썸네일 키를
    맞춘다(`_thumb_key`). 균일·snap 원본은 칸이 없다.

    이 목록이 화면의 선택기를 채운다. 질의가 ffprobe을 돌리지 않으므로 목록은
    **준비가 잰 조사**에서만 온다 — 첫 준비 전에는 비어 있는 게 정직한 값이다.
    `previews/`는 정리 대상이므로 이 기록도 지워질 수 있고, 다음 준비가 다시 쓴다.
    """
    record: dict[str, object] = {
        "schema": VISUAL_INVENTORY_RECIPE,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "audio": [
            {
                "ordinal": ordinal,
                "stream_index": track.stream_index,
                "codec_name": track.codec_name,
                "channels": track.channels,
                "sample_rate": track.sample_rate,
            }
            for ordinal, track in enumerate(audio)
        ],
        "built_at": _now(),
    }
    if grid_conform is not None:
        record["grid_conform"] = grid_conform
    _write_record(workspace.root / _inventory_relative(source_sha256), record)


def _inventory_mapped(workspace: RawWorkspace, source_sha256: str) -> bool:
    """조사 기록이 지금 판의 map 원본이라고 말하는가 — 다른 판·없음은 거짓."""
    record = read_scope.memo(
        ("visual_review.inventory", str(workspace.root), source_sha256),
        lambda: _read_record(workspace.root / _inventory_relative(source_sha256)),
    )
    return _record_str(record, "grid_conform") == preprocess.GRID_CONFORM_TAG


def _read_inventory(workspace: RawWorkspace, source_sha256: str) -> tuple[VisualAudioTrack, ...]:
    """저장된 소리 트랙 목록 — 없거나 믿을 수 없으면 빈 묶음(지어내지 않는다)."""
    record = _read_record(workspace.root / _inventory_relative(source_sha256))
    if record is None or record.get("schema") != VISUAL_INVENTORY_RECIPE:
        return ()
    raw = record.get("audio")
    if not isinstance(raw, list):
        return ()
    tracks: list[VisualAudioTrack] = []
    for ordinal, item in enumerate(raw):
        if not isinstance(item, dict):
            return ()
        stream_index = item.get("stream_index")
        if isinstance(stream_index, bool) or not isinstance(stream_index, int):
            return ()
        codec = item.get("codec_name")
        tracks.append(
            VisualAudioTrack(
                ordinal=ordinal,
                stream_index=stream_index,
                codec_name=codec if isinstance(codec, str) else "",
                channels=_record_int(item, "channels"),
                sample_rate=_record_int(item, "sample_rate"),
            )
        )
    return tuple(tracks)


# ── 맡음 표시 — 준비 작업이 지금 어느 원본을 맡았는가(D62) ───────────────────────


def _claim_dir(workspace: RawWorkspace) -> Path:
    return workspace.root / VISUAL_PREVIEW_ROOT / VISUAL_CLAIM_DIRNAME


def _write_claims(
    workspace: RawWorkspace, job_id: str, sources: set[tuple[str, str]]
) -> tuple[Path, ...]:
    """이 작업이 이 페이지의 원본을 맡았다고 적는다 — 원본 검증(9GB 해시) 전에 「준비 중」이 보인다.

    기록 파일(`<키>.json`)은 원본을 검증하고 스트림을 고른 **뒤에야** 키를 알아 쓸 수 있다. 그
    사이 화면이 「미리보기 아직 없음」을 말했다(실측). 맡음 표시는 원본 해시(검토 시계의 기록값)와
    작업 번호만으로 쓰고, 질의는 그 작업이 지금 활성일 때만 믿는다. 작업이 끝나면 지운다.
    """
    if not _JOB_ID.fullmatch(job_id):
        log.warn(f"작업 번호가 파일 이름에 맞지 않아 맡음 표시를 쓰지 않습니다: {job_id!r}")
        return ()
    written: list[Path] = []
    for source_id, source_sha256 in sorted(sources):
        target = _claim_dir(workspace) / f"{source_sha256}.{job_id}.json"
        _write_record(
            target,
            {"source_id": source_id, "job_id": job_id, "pid": os.getpid(), "claimed_at": _now()},
        )
        written.append(target)
    return tuple(written)


def _drop_claims(claims: Sequence[Path]) -> None:
    for claim in claims:
        try:
            claim.unlink(missing_ok=True)
        except OSError as error:
            log.warn(f"맡음 표시를 지우지 못했습니다(작업이 끝나 무시됨): {claim.name} ({error})")


def _claimed_sources(workspace: RawWorkspace, active: Sequence[JobRecord]) -> frozenset[str]:
    """지금 활성인 작업이 맡은 원본 해시 — 끝난·끊긴 작업의 표시는 아무것도 말하지 않는다."""
    directory = _claim_dir(workspace)
    active_ids = {job.job_id for job in active}
    if not active_ids or not directory.is_dir():
        return frozenset()
    claimed: set[str] = set()
    for claim in directory.glob("*.json"):
        sha, _, job_id = claim.name.removesuffix(".json").partition(".")
        if job_id in active_ids:
            claimed.add(sha)
    return frozenset(claimed)


# ── 끊긴 만들기의 잔재 — 서버 시작·준비 시작에 치운다(D62) ────────────────────────


def sweep_preview_leftovers(workspace: RawWorkspace, active: Sequence[JobRecord]) -> int:
    """죽은 프로세스가 남긴 임시 파일·맡음 표시를 지우고 끊긴 'preparing' 기록을 닫는다.

    지우는 것은 이름에 박힌 pid가 이 프로세스가 아니고 살아 있지도 않은 임시 파일뿐이다 — 게시된
    최종 파일(`<키>.mp4`·`<키>.jpg`·`<키>.json`)은 이름 꼴이 달라 절대 고르지 않는다
    (실측: 10/7–10/11의 `.part` 6.1GB). 기록은 지우지 않고 상태만 'interrupted'로 닫는다 —
    질의가 「준비 중」 대신 낡음을 말해 「미리보기 다시 준비」가 보인다. 답은 지운 바이트 수다.
    """
    root = workspace.root / VISUAL_PREVIEW_ROOT
    if not root.is_dir():
        return 0
    freed = 0
    for directory in (root, root / VISUAL_THUMB_DIRNAME, root / "inventory", _claim_dir(workspace)):
        freed += _sweep_directory(directory)
    closed = _close_orphaned_records(root, active)
    if freed or closed:
        log.info(
            f"미리보기 잔재 정리: 끊긴 만들기의 임시 파일 {freed:,}B를 지우고"
            f" 끊긴 준비 기록 {closed}개를 닫았습니다 ({root})"
        )
    return freed


def _pid_alive(pid: int) -> bool:
    """그 pid가 지금 살아 있는가 — 이 프로세스이거나 물을 권한이 없는(남의) 프로세스면 산 것.

    Windows의 `os.kill`은 묻지 않고 끝내므로 `oscompat.process_alive`가 플랫폼마다 묻는다(D100 §1).
    """
    if pid == os.getpid():
        return True
    return process_alive(pid)


def _dead_owner(entry: Path) -> bool:
    """이 임시 파일·맡음 표시를 남긴 프로세스가 죽었는가 — 꼴이 아니면 False(건드리지 않는다)."""
    if entry.parent.name == VISUAL_CLAIM_DIRNAME and entry.suffix == ".json":
        pid = _record_int(_read_record(entry), "pid")
        return pid is not None and not _pid_alive(pid)
    match = _LEFTOVER.fullmatch(entry.name)
    return match is not None and not _pid_alive(int(match.group("pid")))


def _sweep_directory(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    freed = 0
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file() or not _dead_owner(entry):
            continue
        try:
            size = entry.stat().st_size
            entry.unlink()
        except FileNotFoundError:
            continue  # 다른 정리가 먼저 지웠다
        except OSError as error:
            log.warn(f"미리보기 잔재를 지우지 못해 그대로 둡니다: {entry} ({error})")
            continue
        freed += size
    return freed


def _close_orphaned_records(root: Path, active: Sequence[JobRecord]) -> int:
    """만드는 쪽이 사라진 'preparing' 기록을 'interrupted'로 닫는다 — 닫은 수.

    pid가 있으면 그 프로세스가 죽었을 때, pid가 없는 옛 기록은 활성 준비 작업이 하나도 없을 때
    (서버 시작이 바로 그때다) 사라진 것이다.
    """
    preparing_active = any(job.kind == VISUAL_PREPARE_JOB_KIND for job in active)
    closed = 0
    for record_path in sorted(root.glob("*.json")):
        record = _read_record(record_path)
        if record is None or record.get("state") != "preparing":
            continue
        pid = _record_int(record, "pid")
        if (pid is not None and _pid_alive(pid)) or (pid is None and preparing_active):
            continue
        _write_record(record_path, {**record, "state": "interrupted", "interrupted_at": _now()})
        closed += 1
    return closed


def _registration_for(workspace: RawWorkspace, relative: Path) -> MediaRegistration | None:
    """이 상대 경로의 등록 행 — 미리보기 종류만 훑는다(질의는 생성을 하지 않는다)."""
    wanted = relative.as_posix()
    return next(
        (row for row in workspace.list_media("preview") if row.relative_path == wanted), None
    )


def _visual_traces_purged(workspace: RawWorkspace) -> bool:
    """영상 검토 미리보기의 흔적이 정리됐는가 — 등록 행은 남았는데 파일이 없다(§9.2).

    질의는 원본마다 이것을 묻는다 — 읽기 범위(그리기 한 번) 안에서는 등록 파일을 한 번만 본다(D63).
    """
    key = ("visual_traces_purged", str(workspace.root))
    return read_scope.memo(key, lambda: _read_traces_purged(workspace))


def _read_traces_purged(workspace: RawWorkspace) -> bool:
    return any(
        not (workspace.root / Path(row.relative_path)).is_file()
        for row in workspace.list_media("preview")
        if row.relative_path.startswith(f"{VISUAL_PREVIEW_ROOT}/")
    )


# ── 상태 유도 — 질의가 만드는 다섯 값 ─────────────────────────────────────────


def _thumb_state(
    workspace: RawWorkspace, key: str, fingerprint_active: bool, purged: bool
) -> tuple[str, MediaRegistration | None]:
    """썸네일 하나의 상태와 등록 행 — 파일·실패 기록·활성 작업·정리 흔적에서 유도한다."""
    relative = _thumb_relative(key)
    registration = _registration_for(workspace, relative)
    if (workspace.root / relative).is_file():
        return "ready", registration
    failure = _read_record(workspace.root / _thumb_failure_relative(key))
    if failure is not None and failure.get("state") == "failed":
        return "failed", registration
    if fingerprint_active:
        return "preparing", registration
    if registration is not None or purged:
        return "stale", registration
    return "missing", registration


def _record_mp4_exists(workspace: RawWorkspace, key: str, record: dict[str, object]) -> bool:
    """기록이 가리키는 재생 파일이 지금 있는가 — 직접 등록(프록시)이면 등록 ID로 본다."""
    direct_file_id = record.get("direct_file_id")
    if isinstance(direct_file_id, str):
        try:
            workspace.resolve_media_file(direct_file_id)
        except RawWorkspaceError:
            return False
        return True
    return (workspace.root / _preview_relative(key)).is_file()


def _derive_record_state(
    workspace: RawWorkspace, key: str, record: dict[str, object], active: Sequence[JobRecord]
) -> str:
    """기록 하나의 상태 — 선언한 상태를 파일·활성 작업이 뒷받침할 때만 믿는다."""
    declared = _record_str(record, "state")
    if declared == "ready" and _record_mp4_exists(workspace, key, record):
        return "ready"
    if declared == "failed":
        return "failed"
    if declared == "preparing" and _preparing_backed(record, active):
        return "preparing"
    return "stale"


def _preparing_backed(record: dict[str, object], active: Sequence[JobRecord]) -> bool:
    """'preparing' 기록을 지금 도는 작업이 받치는가 — 기록을 쓴 **그 작업**이어야 한다(D62).

    끊긴 만들기의 기록은 영원히 'preparing'으로 남는다(실측 10/7–10/11). 아무 준비 작업이나
    받침으로 치면 다른 영상의 준비가 도는 동안 그것들이 「준비 중」으로 보였다. 작업 번호가 없는
    옛 기록만 예전 규칙(준비 작업 하나라도)이다 — 서버 시작의 정리가 그것을 'interrupted'로 닫는다.
    """
    owner = record.get("job_id")
    if isinstance(owner, str):
        return any(job.job_id == owner for job in active)
    return any(job.kind == VISUAL_PREPARE_JOB_KIND for job in active)


def _source_candidates(
    workspace: RawWorkspace,
    source_sha256: str,
    selected_audio: tuple[int | None, ...] | None,
) -> list[tuple[str, dict[str, object]]]:
    """이 원본(과 소리 선택)의 미리보기 기록 전부 — 최근(`built_at`) 순."""
    directory = workspace.root / VISUAL_PREVIEW_ROOT
    if not directory.is_dir():
        return []
    candidates: list[tuple[str, dict[str, object]]] = []
    for record_path in sorted(directory.glob("*.json")):
        record = _read_record(record_path)
        if record is None or record.get("source_sha256") != source_sha256:
            continue
        if record.get("schema") != VISUAL_PREVIEW_RECIPE:
            continue
        key = _record_str(record, "key")
        if key is None:
            continue
        if (
            selected_audio is not None
            and _record_int(record, "audio_ordinal") not in selected_audio
        ):
            continue
        candidates.append((key, record))
    candidates.sort(key=lambda item: _record_str(item[1], "built_at") or "", reverse=True)
    return candidates


def _latest_source_record(
    workspace: RawWorkspace,
    source_sha256: str,
    selected_audio: tuple[int | None, ...] | None = None,
    active: Sequence[JobRecord] = (),
) -> tuple[str, dict[str, object]] | None:
    """이 원본의 원본 전체 미리보기 기록 — (키, 기록) 또는 None.

    `selected_audio`가 None이면(선택을 안 준 질의) 가장 최근(`built_at`) 기록이
    지금의 선택이다. 짝이 들어오면 **그 서수와 정확히 같은** 기록만 본다 — 방금
    다른 서수를 만들었다 해도 그것을 바꿔치기하지 않는다(선택은 캐시 짝을 묶는다).
    같은 후보 안에서는 **재생 가능한 완성본이 언제나 이긴다** — 다른 키로 다시
    시작한 인코딩(진행 중·실패·끊김)이 이미 멀쩡한 미리보기를 가리면 플레이어가
    아무것도 못 받는다(실측 증상). 완성본이 하나도 없을 때만 가장 최근 기록의
    상태(preparing·failed·stale)가 그대로 보인다.
    """
    candidates = _source_candidates(workspace, source_sha256, selected_audio)
    if not candidates:
        return None
    return next(
        (c for c in candidates if _derive_record_state(workspace, *c, active) == "ready"),
        candidates[0],
    )


def _source_preview_state(
    workspace: RawWorkspace,
    source_id: str,
    source_sha256: str,
    active: Sequence[JobRecord],
    selected_audio: tuple[int | None, ...] | None = None,
    claimed: bool = False,
) -> VisualSourcePreview:
    """원본 하나의 미리보기 상태 — 기록 주도로 유도한다(질의는 조사하지 않는다).

    `selected_audio`는 이 원본에 대해 질의가 **명시적으로** 고른 서수다 — 그 서수의
    기록만 보고, 없으면 `missing`(그 선택은 한 번도 만들지 않았다)이다. 생략하면
    가장 최근 기록(V1 기본 질의)을 본다. `claimed`는 지금 활성인 준비 작업이 이 원본을
    맡았다는 표시다 — 재생 가능한 완성본이 없으면 「준비 중」이다(기록이 아직 없어도, D62).
    """
    audio_tracks = _read_inventory(workspace, source_sha256)
    found = _latest_source_record(workspace, source_sha256, selected_audio, active)
    if found is None:
        if selected_audio is not None:
            state = "missing"  # 이 선택은 한 번도 만들지 않았다 — 다른 선택의 흔적과 무관
            chosen = selected_audio[0]
        else:
            state = "stale" if _visual_traces_purged(workspace) else "missing"
            chosen = None
        if claimed:
            state = "preparing"  # 작업이 원본 검증 중이라 기록이 아직 없다(D62)
        return VisualSourcePreview(
            source_id=source_id,
            source_sha256=source_sha256,
            state=state,
            audio_ordinal=chosen,
            audio_stream_index=None,
            video_stream_index=None,
            registered_file_id=None,
            relative_path=None,
            duration_seconds=None,
            browser_origin_seconds=None,
            width=None,
            height=None,
            rotation_degrees=None,
            remuxed=None,
            error=None,
            audio_origin_seconds=None,
            audio_tracks=audio_tracks,
        )
    key, record = found
    relative = _preview_relative(key)
    registration = _registration_for(workspace, relative)
    mp4_exists = _record_mp4_exists(workspace, key, record)
    direct_file_id = record.get("direct_file_id")
    state = _derive_record_state(workspace, key, record, active)
    if claimed and state != "ready":
        state = "preparing"  # 지난 시도가 낡았거나 실패했어도 지금 작업이 다시 만들고 있다
    remuxed = record.get("remuxed")
    # 매핑 원점은 증거(video_origin_seconds)에서 다시 잓는다 — F1까지의 기록이
    # browser_origin_seconds를 소리 원점 뺄셈으로 적어 늦은 트랙·침묵 선택에서
    # 0을 거짓으로 남겼다(A3 실측). 질의는 ffprobe도 쓰기도 없이 회복한다 —
    # 매핑 값만으론 미리보기 파일·등록 ID를 다시 만들지 않는다.
    return VisualSourcePreview(
        source_id=source_id,
        source_sha256=source_sha256,
        state=state,
        audio_ordinal=_record_int(record, "audio_ordinal"),
        audio_stream_index=_record_int(record, "audio_stream_index"),
        video_stream_index=_record_int(record, "video_stream_index"),
        registered_file_id=(
            str(direct_file_id)
            if direct_file_id and mp4_exists
            else registration.file_id
            if registration is not None
            else None
        ),
        relative_path=relative.as_posix(),
        duration_seconds=_record_str(record, "duration"),
        browser_origin_seconds=_record_str(record, "video_origin_seconds"),
        width=_record_int(record, "width"),
        height=_record_int(record, "height"),
        rotation_degrees=_record_int(record, "rotation_degrees"),
        remuxed=remuxed if isinstance(remuxed, bool) else None,
        error=_record_str(record, "error"),
        audio_origin_seconds=_record_str(record, "audio_origin_seconds"),
        audio_tracks=audio_tracks,
    )


# ── 질의 — 읽기만 한다 ─────────────────────────────────────────────────────────


def query_overview(
    workspace: RawWorkspace,
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str | None = None,
    selected_segment_id: str | None = None,
    page: int = 1,
    active_jobs: Sequence[JobRecord] = (),
    expected_revision_id: str | None = None,
    audio_selections: Mapping[str, int | None] | None = None,
    include_all_sources: bool = False,
    selection_filter: str = "all",
) -> VisualReviewOverview:
    """검토 헤드 위의 영상 검토 질의 — 미디어 생성·제공자 호출이 전혀 없다.

    페이지는 1부터다. 선택 조각을 주지 않으면 요청 페이지의 첫 조각이 선택이다.
    원본 미리보기 상태는 그 페이지(과 선택 조각)가 닿는 원본만 돌려준다 — 질의는
    목록을 계산하고, 채우는 것은 준비 작업의 몫이다.

    `expected_revision_id`를 주면 지금 머리와 다를 때 `StaleRevisionError`로 거부한다
    (화면이 본 것이 낡았다는 것을 값으로 아는 길). 생략하면 언제나 최신 머리 —
    명시적 새로고침의 길은 그대로 열려 있다. `audio_selections`는 원본별 **명시적**
    소리 서수다 — 준 원본은 그 선택의 기록만 보고, 생략한 원본은 가장 최근 기록을
    본다.
    """
    _require_page(page)
    loaded = facade.load_head(base_plan_id)
    revision = loaded.revision
    require_head_revision(revision, expected_revision_id)
    ordered = chronological(revision)
    active = tuple(
        job
        for job in active_jobs
        if job.kind == VISUAL_PREPARE_JOB_KIND and job.status in ACTIVE_JOB_STATUSES
    )
    fingerprints = {job.input_fingerprint for job in active}
    purged = _visual_traces_purged(workspace)
    claimed = _claimed_sources(workspace, active)
    if selection_filter not in ("all", "included", "excluded", "review"):
        raise VisualReviewError("알 수 없는 컷 필터입니다")
    # 「제외 제안」 보기(키는 옛 이름 review) — 결정을 기다리는 제외 제안만이다(W-17 C, D57).
    # 옛 「검토 대기」는 '검토 필요' 권고·전사 실패 컷도 실었다 — 보류는 결정할 것이 아니다.
    matching = [
        index
        for index, segment in enumerate(ordered)
        if selection_filter == "all"
        or segment.selection == selection_filter
        or (selection_filter == "review" and is_unreviewed_auto_exclusion(segment))
    ]
    page_count = max(1, math.ceil(len(matching) / VISUAL_PAGE_SIZE))
    start = (page - 1) * VISUAL_PAGE_SIZE
    positions = matching[start : start + VISUAL_PAGE_SIZE]
    page_segments = tuple(ordered[index] for index in positions)
    if (
        selected_segment_id
        and any(s.segment_id == selected_segment_id for s in ordered)
        and not any(ordered[index].segment_id == selected_segment_id for index in matching)
    ):
        selected_segment_id = None
    chosen_id = selected_segment_id or (page_segments[0].segment_id if page_segments else None)
    chosen_position = None
    if chosen_id is not None:
        chosen_position = next(
            (index for index, segment in enumerate(ordered) if segment.segment_id == chosen_id),
            None,
        )
        if chosen_position is None:
            raise VisualReviewError(
                f"리비전 {revision.revision_id!r}에 선택 조각 {chosen_id!r}이(가) 없다"
            )
    touched = {segment.source_id for segment in (ordered if include_all_sources else page_segments)}
    if chosen_position is not None:
        touched.add(ordered[chosen_position].source_id)
    selections = validated_audio_selections(audio_selections)
    chosen_filters = {
        source_id: (choice,) for source_id, choice in selections.items() if source_id in touched
    }
    previews = {
        source_id: _source_preview_state(
            workspace,
            source_id,
            _required_clock(revision, source_id).source_sha256,
            active,
            chosen_filters.get(source_id),
            claimed=_required_clock(revision, source_id).source_sha256 in claimed,
        )
        for source_id in sorted(touched)
    }
    cards = tuple(
        _segment_card(
            workspace, revision, ordered, position, fingerprints, purged, previews, claimed
        )
        for position in positions
    )
    selected = None
    if chosen_position is not None:
        selected = _selected_segment(
            workspace, revision, ordered, chosen_position, fingerprints, purged, previews, claimed
        )

    def cards_at(wanted: Sequence[int]) -> tuple[VisualSegmentCard, ...]:
        return tuple(
            _segment_card(
                workspace, revision, ordered, position, fingerprints, purged, previews, claimed
            )
            for position in wanted
        )

    representatives: tuple[VisualSegmentCard, ...] = ()
    view_segments: tuple[VisualSegmentCard, ...] = ()
    representative_thumbs: tuple[str | None, ...] = ()
    if include_all_sources:
        representatives = cards_at(_representative_positions(ordered, matching))
        view_segments = cards_at(_view_positions(ordered, chosen_position, positions))
        representative_thumbs = tuple(
            _card_thumb(workspace, revision, ordered, card) for card in representatives
        )
    return VisualReviewOverview(
        workspace_id=facade.project_id,
        base_plan_id=revision.base_plan_id,
        revision_id=revision.revision_id,
        sequence=revision.sequence,
        stale_sources=loaded.stale_sources,
        page=page,
        page_count=page_count,
        total_segments=len(matching),
        segments=cards,
        selected=selected,
        sources=tuple(previews[source_id] for source_id in sorted(previews)),
        active_preparation_jobs=tuple(job.job_id for job in active),
        representatives=representatives,
        view_segments=view_segments,
        representative_thumbs=representative_thumbs,
    )


def _view_positions(
    ordered: Sequence[ReviewSegment], chosen_position: int | None, page_positions: Sequence[int]
) -> list[int]:
    """고른 영상의 컷 가운데 쪽 목록에 없는 것의 위치 — 필터와 무관하다(레일은 거르지 않는다)."""
    if chosen_position is None:
        return []
    source_id = ordered[chosen_position].source_id
    shown = set(page_positions)
    return [
        position
        for position, segment in enumerate(ordered)
        if segment.source_id == source_id and position not in shown
    ]


def _representative_positions(
    ordered: Sequence[ReviewSegment], matching: Sequence[int]
) -> list[int]:
    """영상마다 대표 컷의 위치 — 필터에 맞는 첫 컷, 없으면 첫 컷. 영상 순서(촬영 시각 순) 그대로."""
    first: dict[str, int] = {}
    for position, segment in enumerate(ordered):
        first.setdefault(segment.source_id, position)
    chosen: dict[str, int] = {}
    for position in matching:
        chosen.setdefault(ordered[position].source_id, position)
    return sorted(chosen.get(source_id, position) for source_id, position in first.items())


def _drawable_thumb_id(workspace: RawWorkspace, key: str) -> str | None:
    """화면이 그릴 수 있는 썸네일의 등록 ID — 파일과 등록 행이 다 있어야 한다(`/media/<id>`).

    파일만 있고 등록이 없는 것(`ready`인데 주소가 없다)은 그릴 수 없다 — 카드가 그림 대신
    「준비됨」 글자를 보였다(W-24 세션 I).
    """
    relative = _thumb_relative(key)
    if not (workspace.root / relative).is_file():
        return None
    registration = _registration_for(workspace, relative)
    return registration.file_id if registration is not None else None


def _card_thumb(
    workspace: RawWorkspace,
    revision: ReviewRevision,
    ordered: Sequence[ReviewSegment],
    card: VisualSegmentCard,
) -> str | None:
    """영상 카드의 그림 — 대표 컷의 것, 없으면 그 영상의 컷 차례로 처음 그릴 수 있는 것."""
    if card.thumb_state == "ready" and card.thumb_registered_file_id is not None:
        return card.thumb_registered_file_id
    clock = _required_clock(revision, card.source_id)
    for segment in ordered:
        if segment.source_id != card.source_id:
            continue
        frame_index, _, _ = _snapped_fields(clock, segment.start_pts, segment.end_pts)
        key = _thumb_key(workspace, clock.source_sha256, frame_index)
        found = _drawable_thumb_id(workspace, key)
        if found is not None:
            return found
    return None


def _require_page(page: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise VisualReviewError(f"페이지는 1 이상의 정수여야 한다: {page!r}")


def require_head_revision(head: ReviewRevision, expected_revision_id: str | None) -> None:
    """머리가 호출자가 본 리비전인가 — 아니면 stale로 거부한다(0손상, 새 토큰 발급 없음).

    질의·제안·준비가 같은 묶음 규칙이다: 머리가 앞서 갔으면 **살아남은 조각**의
    요청도 거부한다 — 옛 사건에 새 머리의 저장 토큰을 몰래 붙이는 일이 없다.
    `None`은 묶음 없음(명시적 최신 새로고침)이다.
    """
    if expected_revision_id is None:
        return
    if head.revision_id != expected_revision_id:
        raise StaleRevisionError(
            f"호출이 묶인 검토 리비전 {expected_revision_id!r}이(가) 지금 머리"
            f" {head.revision_id!r}와 다르다 — 화면을 새로 고침해서 최신 리비전으로"
            " 다시 받으세요"
        )


def _snapped_fields(clock: SourceClock, start_pts: int, end_pts: int) -> tuple[int, int, str]:
    """가운데 프레임의 (번호, 원본 PTS, 원점 기준 초) — 미리보기 seek의 재료다.

    원본 전체 미리보기는 `-ss` 없이 만들어 원점 이동 하나로 원본 시각과 대응한다.
    그래서 `snapped_seconds`는 원점 기준 초이고, 화면은 이 값을 그대로 재생 위치로
    쓴다(§10.2와 같은 좌표 규칙).
    """
    frame_index = middle_frame_index(clock, start_pts, end_pts)
    snapped_pts = clock.origin_pts + frame_index * clock.frame_ticks
    return frame_index, snapped_pts, str((snapped_pts - clock.origin_pts) * clock.time_base)


def _required_clock(revision: ReviewRevision, source_id: str) -> SourceClock:
    clock = revision.clock_for(source_id)
    if clock is None:
        raise VisualReviewError(f"원본 {source_id!r}의 시계가 리비전에 없다")
    return clock


def _right_neighbor(
    ordered: Sequence[ReviewSegment], segment: ReviewSegment
) -> VisualNeighbor | None:
    """같은 원본의 맞닿은 오른쪽 이웃 — 시간순 다음이 닿지 않으면 없다(§12)."""
    for position, candidate in enumerate(ordered):
        if candidate.segment_id != segment.segment_id:
            continue
        if position + 1 >= len(ordered):
            return None
        nxt = ordered[position + 1]
        if nxt.source_id == segment.source_id and nxt.start_pts == segment.end_pts:
            return VisualNeighbor(
                segment_id=nxt.segment_id,
                source_id=nxt.source_id,
                start_pts=nxt.start_pts,
                end_pts=nxt.end_pts,
            )
        return None
    raise VisualReviewError(f"조각 {segment.segment_id!r}을 시간순 목록에서 못 찾았다")


def _segment_card(
    workspace: RawWorkspace,
    revision: ReviewRevision,
    ordered: Sequence[ReviewSegment],
    position: int,
    fingerprints: frozenset[str] | set[str],
    purged: bool,
    previews: Mapping[str, VisualSourcePreview],
    claimed: frozenset[str] = frozenset(),
) -> VisualSegmentCard:
    """목록 한 칸 — 조각 값과 미리보기 상태를 묶는다(생성은 하지 않는다)."""
    segment = ordered[position]
    clock = _required_clock(revision, segment.source_id)
    frame_index, snapped_pts, seconds = _snapped_fields(clock, segment.start_pts, segment.end_pts)
    key = _thumb_key(workspace, clock.source_sha256, frame_index)
    fingerprint_active = (
        preparation_fingerprint(revision.base_plan_id, _page_of(position)) in fingerprints
        or clock.source_sha256 in claimed  # 원본 미리보기 작업이 맡은 원본의 썸네일(D62)
    )
    state, registration = _thumb_state(workspace, key, fingerprint_active, purged)
    preview = previews.get(segment.source_id)
    return VisualSegmentCard(
        segment_id=segment.segment_id,
        source_id=segment.source_id,
        source_order=segment.source_order,
        segment_order=segment.segment_order,
        start_pts=segment.start_pts,
        end_pts=segment.end_pts,
        duration_seconds=str((segment.end_pts - segment.start_pts) * clock.time_base),
        selection=segment.selection,
        recommendation=segment.recommendation,
        speech_status=segment.speech_status,
        notes=segment.notes,
        clock=_clock_summary(clock),
        snapped_frame_index=frame_index,
        snapped_pts=snapped_pts,
        snapped_seconds=seconds,
        preview_registered_file_id=(
            preview.registered_file_id if preview is not None and preview.state == "ready" else None
        ),
        preview_seek_seconds=_seek_seconds(seconds, preview),
        thumb_state=state,
        thumb_registered_file_id=registration.file_id if registration is not None else None,
        thumb_relative_path=_thumb_relative(key).as_posix(),
        right_neighbor=_right_neighbor(ordered, segment),
    )


def _clock_summary(clock: SourceClock) -> VisualClockSummary:
    """시계 값 요약 — 격자·원점·끝을 그대로 옮긴다(화면의 국소 스냅 재료)."""
    return VisualClockSummary(
        source_id=clock.source_id,
        source_sha256=clock.source_sha256,
        time_base_num=clock.time_base_num,
        time_base_den=clock.time_base_den,
        origin_pts=clock.origin_pts,
        end_pts=clock.end_pts,
        frame_ticks=clock.frame_ticks,
    )


def _seek_seconds(snapped_seconds: str, preview: VisualSourcePreview | None) -> str | None:
    """**출력 영상 첫 PTS를 더한** 재생 위치 — 원점을 0이라고 가정하지 않는다(G3).

    원본 전체 미리보기는 `-ss` 없이 만들지만 ffmpeg이 입력의 선언 시작(format
    start_time)만큼 전체를 당겨 출력 타임라인을 다시 놓는다(-copyts 아님). 브라우저는
    그 출력 컨테이너 시각표 그대로 재생하므로 원본 국소 시각 s의 프레임은 브라우저
    시각 `browser_origin_seconds`(출력 영상 첫 패킷 PTS) + s에 나타난다 — 프레임
    대응이 오라클이다(A3 실측: 선행 소리 원본의 트랙 0/1/침묵 세 선택 모두 출력
    영상 원점이 같고 디코드 대응 mse 0). 영상이 가장 이른 스트림이면 원점이 0이라
    매핑은 항등식 — 지금까지 통과한 전부다. 측정이 없으면(None) 화면은 미리보기가
    준비되지 않은 것으로 다룬다 — 지어내지 않는다.
    """
    if preview is None or preview.browser_origin_seconds is None:
        return None
    return str(Fraction(snapped_seconds) + Fraction(preview.browser_origin_seconds))


def _selected_segment(
    workspace: RawWorkspace,
    revision: ReviewRevision,
    ordered: Sequence[ReviewSegment],
    position: int,
    fingerprints: frozenset[str] | set[str],
    purged: bool,
    previews: Mapping[str, VisualSourcePreview],
    claimed: frozenset[str] = frozenset(),
) -> VisualSelectedSegment:
    """선택 조각의 자세한 기록 — 카드에 덮기 등급을 더한 모양이다."""
    card = _segment_card(
        workspace, revision, ordered, position, fingerprints, purged, previews, claimed
    )
    segment = ordered[position]
    return VisualSelectedSegment(
        segment_id=card.segment_id,
        source_id=card.source_id,
        source_order=card.source_order,
        segment_order=card.segment_order,
        start_pts=card.start_pts,
        end_pts=card.end_pts,
        duration_seconds=card.duration_seconds,
        selection=card.selection,
        recommendation=card.recommendation,
        override_recommendation=segment.override_recommendation,
        speech_status=card.speech_status,
        notes=card.notes,
        clock=card.clock,
        snapped_frame_index=card.snapped_frame_index,
        snapped_pts=card.snapped_pts,
        snapped_seconds=card.snapped_seconds,
        preview_registered_file_id=card.preview_registered_file_id,
        preview_seek_seconds=card.preview_seek_seconds,
        thumb_state=card.thumb_state,
        thumb_registered_file_id=card.thumb_registered_file_id,
        thumb_relative_path=card.thumb_relative_path,
        right_neighbor=card.right_neighbor,
    )


# ── 준비 — 한 페이지의 없는 항목만 순서대로 만든다 ─────────────────────────────


@dataclass(frozen=True, slots=True)
class _SourceSelection:
    """준비가 조사해 정한 스트림 선택 — 서수가 키, 전역 번호가 ffmpeg의 매핑이다."""

    source_id: str
    source_sha256: str
    path: Path
    video_stream_index: int
    audio_ordinal: int | None
    audio_stream_index: int | None
    job_id: str | None = None
    """이 준비를 도는 작업 — 'preparing' 기록이 그 작업에 묶인다(D62)."""
    slot_packets: tuple[int, ...] = ()
    """디코드할 파일이 map 시계(D70)면 슬롯 → 패킷 순번 — 썸네일 번호는 슬롯이다."""


def run_page_preparation(
    context: JobContext,
    workspace: RawWorkspace,
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str | None = None,
    page: int = 1,
    audio_selections: Mapping[str, int | None] | None = None,
    expected_revision_id: str | None = None,
    asset_ids: tuple[str, ...] = (),
) -> None:
    """요청 페이지의 미리보기를 채운다 — 원본 전체 MP4와 썸네일, 없는 것만 순서대로.

    썸네일 키가 내용(원본 해시·프레임 번호)이므로 경계 편집 뒤에도 같은 가운데
    프레임은 다시 만들지 않는다. 원본은 준비가 시작되기 전에 한 번 더 검증한다
    (크기·SHA-256 — 계획·검토와 같은 계약). 항목 하나가 실패하면 그 기록을 남기고
    계속하되, 작업의 결과는 실패로 닫는다 — 조용한 빈 성공이 없다(§14.1).

    `audio_selections`는 원본별 **명시적** 소리 선택이다 — 키에 없는 원본은 기본
    (첫 트랙, 없으면 침묵), 값 `None`은 트랙이 있어도 명시적 침묵, 서수는 그
    트랙만 담는다. 범위 밖 서수는 조사 뒤 곧바로 실패한다(첫 트랙으로 조용히
    환원하지 않는다). `expected_revision_id`를 주면 작업이 **실행되는 순간**에도
    머리가 그 리비전인지 다시 본다 — 그새 앞서 갔으면 실패로 닫지, 새 기하를
    몰래 다시 해석하지 않는다.
    """
    _require_page(page)
    revision = facade.load_head(base_plan_id).revision
    require_head_revision(revision, expected_revision_id)
    ordered = chronological(revision)
    start = (page - 1) * VISUAL_PAGE_SIZE
    page_segments = ordered[start : start + VISUAL_PAGE_SIZE]
    if asset_ids:
        page_segments = tuple(s for s in page_segments if s.source_id in asset_ids)
    _run_preparation(
        context, workspace, revision, page_segments, validated_audio_selections(audio_selections)
    )


def pages_with_sources(revision: ReviewRevision, asset_ids: tuple[str, ...]) -> tuple[int, ...]:
    """쪽 준비(`run_page_preparation`)가 그 원본들의 조각을 하나라도 맡는 쪽 번호 — 읽기만 한다.

    `asset_ids`가 비면 모든 쪽이다. 나머지 쪽의 준비는 조각이 없어 아무것도 만들지 않는다 —
    원본 미리보기가 그런 쪽마다 빈 준비 작업을 걸어, 영상 하나를 고를 때마다 작업이 쪽 수만큼
    쌓였다(W-24 세션 J).
    """
    ordered = chronological(revision)
    page_count = (len(ordered) + VISUAL_PAGE_SIZE - 1) // VISUAL_PAGE_SIZE
    wanted = set(asset_ids)
    return tuple(
        page
        for page in range(1, page_count + 1)
        if not wanted
        or any(
            segment.source_id in wanted
            for segment in ordered[(page - 1) * VISUAL_PAGE_SIZE : page * VISUAL_PAGE_SIZE]
        )
    )


def run_source_preparation(
    context: JobContext,
    workspace: RawWorkspace,
    facade: ReviewWorkspaceFacade,
    *,
    source_id: str,
    base_plan_id: str | None = None,
    audio_selections: Mapping[str, int | None] | None = None,
    expected_revision_id: str | None = None,
) -> None:
    """영상 하나의 미리보기를 채운다 — 쪽과 무관하게 그 영상의 원본 전체 MP4와 모든 컷 썸네일(D64).

    작업대 「미리보기 다시 준비」가 고른 영상에 이것을 건다. 예전에는 쪽 준비
    (`run_page_preparation`)가 필터 없는 컷 목록의 지금 쪽을 채웠다 — 고른 영상이 그 쪽 밖이면
    아무것도 하지 않고 「시작했습니다」라고 답했다. 없는 것만 만드는 규칙·실패 기록·소리 선택은
    쪽 준비와 같다.
    """
    revision = facade.load_head(base_plan_id).revision
    require_head_revision(revision, expected_revision_id)
    segments = tuple(s for s in chronological(revision) if s.source_id == source_id)
    if not segments:
        raise VisualReviewError(f"검토 머리에 원본 {source_id!r}의 컷이 없다")
    _run_preparation(
        context, workspace, revision, segments, validated_audio_selections(audio_selections)
    )


def source_preparation_missing(
    workspace: RawWorkspace,
    facade: ReviewWorkspaceFacade,
    *,
    source_id: str,
    base_plan_id: str | None = None,
    audio_selections: Mapping[str, int | None] | None = None,
) -> int:
    """영상 하나에서 준비가 만들 것의 수 — 원본 전체 미리보기(완성본이 없으면 1) + 파일 없는 썸네일.

    읽기만 한다. 0이면 「미리보기 다시 준비」가 할 일이 없다 — 작업을 걸지 않고 그렇게 답한다(D64).
    """
    revision = facade.load_head(base_plan_id).revision
    clock = _required_clock(revision, source_id)
    selections = validated_audio_selections(audio_selections)
    chosen = (selections[source_id],) if source_id in selections else None
    preview = _source_preview_state(workspace, source_id, clock.source_sha256, (), chosen)
    missing = 0 if preview.state == "ready" else 1
    for segment in chronological(revision):
        if segment.source_id != source_id:
            continue
        frame_index, _, _ = _snapped_fields(clock, segment.start_pts, segment.end_pts)
        key = _thumb_key(workspace, clock.source_sha256, frame_index)
        missing += not (workspace.root / _thumb_relative(key)).is_file()
    return missing


REPRESENTATIVE_THUMBS_PREFIX = "representative-thumbs:"
"""영상마다 첫 컷 썸네일 작업(W-24 세션 I)의 지문 머리 — 쪽·영상 준비의 지문(16진 해시)과 갈린다."""


def _first_cuts(revision: ReviewRevision) -> dict[str, ReviewSegment]:
    """영상마다 첫 컷(촬영 시각 순) — 필터 없는 목록의 대표 컷이다."""
    first: dict[str, ReviewSegment] = {}
    for segment in chronological(revision):
        first.setdefault(segment.source_id, segment)
    return first


def representative_thumbs_missing(
    workspace: RawWorkspace, revision: ReviewRevision
) -> tuple[tuple[str, str], ...]:
    """첫 컷의 썸네일을 그릴 수 없는 영상 — (원본, 썸네일 키), 촬영 시각 순. 읽기만 한다.

    파일은 있는데 등록이 없는 것도 고른다 — 준비가 ffmpeg 없이 등록만 채운다(`_prepare_thumb`).
    """
    missing: list[tuple[str, str]] = []
    for source_id, segment in _first_cuts(revision).items():
        clock = _required_clock(revision, source_id)
        frame_index, _, _ = _snapped_fields(clock, segment.start_pts, segment.end_pts)
        key = _thumb_key(workspace, clock.source_sha256, frame_index)
        if _drawable_thumb_id(workspace, key) is None:
            missing.append((source_id, key))
    return tuple(missing)


def representative_thumbs_fingerprint(keys: Sequence[str]) -> str:
    """첫 컷 썸네일 작업의 지문 — 같은 썸네일 묶음의 활성 작업은 하나로 묶는다."""
    material = f"{VISUAL_THUMB_RECIPE}|" + ",".join(sorted(keys))
    return REPRESENTATIVE_THUMBS_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def run_representative_thumbnails(
    context: JobContext,
    workspace: RawWorkspace,
    revision: ReviewRevision,
    missing: Sequence[tuple[str, str]],
    attempted: Callable[[str], None],
) -> None:
    """고른 영상마다 첫 컷의 썸네일 하나만 — 원본 전체 미리보기는 만들지 않는다(W-24 세션 I).

    `missing`은 `representative_thumbs_missing`의 (원본, 썸네일 키)다. 하나를 시도하고 나면(성공·
    실패·뜻밖의 오류 모두) `attempted(키)`를 부른다 — 서비스가 그 키를 이번 실행에서 다시 걸지
    않는다. 취소되면 남은 키까지 모두 적는다 — 사람이 취소한 작업을 다음 고리가 곧바로 다시 걸지
    않게. 「라이브러리 닫기」의 취소는 서비스가 세대를 올려 이 기록을 버리므로 다시 열면 다시 건다.

    라이브러리를 열면 모든 영상 카드에 그림이 있어야 한다. 썸네일은 영상을 고를 때·「미리보기
    다시 준비」·자동 정리만 만들어서, 고르지 않은 영상(실측 0024)은 그림이 없었다. 원본마다
    검증·조사 한 번에 프레임 하나, 하나씩 차례로, 낮은 OS 우선순위로(`_prepare_thumb`). 키·조리법·
    바이트는 다른 준비와 같다. 하나가 실패해도 다음 영상으로 가고, 실패가 있으면 작업을 실패로
    닫는다 — 조용한 빈 성공이 없다.
    """
    first = _first_cuts(revision)
    context.set_total(len(missing))
    failures: list[str] = []
    seek_plans: dict[str, preprocess.FrameSeekPlan | None] = {}
    try:
        for done, (source_id, key) in enumerate(missing, start=1):
            context.check_cancel()
            segment = first.get(source_id)
            if segment is not None:
                attempt = functools.partial(
                    _first_cut_thumb, context, workspace, revision, segment, failures, seek_plans
                )
                _tried_once(key, attempted, attempt)
            context.set_progress(done)
    except JobCancelledError:
        for _, key in missing:
            attempted(key)
        raise
    if failures:
        raise VisualReviewError(
            f"첫 컷 썸네일 실패 {len(failures)}건(기록에 남았다): " + "; ".join(failures[:5])
        )


def _tried_once(key: str, attempted: Callable[[str], None], attempt: Callable[[], None]) -> None:
    """시도 하나 — 어떻게 멈추든(성공·실패·오류·취소) 적는다(취소는 부르는 쪽이 남은 키까지)."""
    try:
        attempt()
    finally:
        attempted(key)


def _first_cut_thumb(
    context: JobContext,
    workspace: RawWorkspace,
    revision: ReviewRevision,
    segment: ReviewSegment,
    failures: list[str],
    seek_plans: dict[str, preprocess.FrameSeekPlan | None],
) -> None:
    """영상 하나의 첫 컷 썸네일 — 원본을 못 열면 실패로 적고 다음 영상으로 간다."""
    try:
        selection = _chosen_source(workspace, revision, segment.source_id, {}, context)
    except (RawWorkspaceError, RuntimeError, OSError) as error:
        failures.append(f"원본 {segment.source_id!r}: {error}")
        return
    _prepare_thumb(workspace, revision, segment, selection, failures, seek_plans)


def _run_preparation(
    context: JobContext,
    workspace: RawWorkspace,
    revision: ReviewRevision,
    segments: Sequence[ReviewSegment],
    choices: Mapping[str, int | None],
) -> None:
    """준비 본문 — 맡은 원본을 표시하고 조각을 순서대로 채운다. 실패가 있으면 실패로 닫는다."""
    if not segments:
        return
    sweep_preview_leftovers(workspace, ())
    claims = _write_claims(
        workspace,
        context.job_id,
        {
            (seg.source_id, _required_clock(revision, seg.source_id).source_sha256)
            for seg in segments
        },
    )
    try:
        failures = _prepare_segments(context, workspace, revision, segments, choices)
    finally:
        _drop_claims(claims)
    if failures:
        raise VisualReviewError(
            f"미리보기 준비 실패 {len(failures)}건(원본 전체·썸네일 각각 기록에 남았다): "
            + "; ".join(failures[:5])
        )


def _prepare_segments(
    context: JobContext,
    workspace: RawWorkspace,
    revision: ReviewRevision,
    page_segments: Sequence[ReviewSegment],
    choices: Mapping[str, int | None],
) -> list[str]:
    """페이지 조각을 순서대로 — 원본마다 검증 한 번, 조각마다 썸네일, 그 뒤에 원본 전체 미리보기.

    썸네일이 먼저다(W-24 세션 I). 예전에는 원본마다 첫 썸네일 앞에서 원본 전체 인코딩을 했다 —
    실측 0024(33컷)는 그 인코딩이 매번 한도·서버 멈춤에 끊겨 썸네일이 하나도 없었다. 썸네일은
    미리보기 파일이 아니라 원본(또는 프록시, `_select_source`가 고른 것)에서 바로 뽑으므로 순서만
    바뀌고 바이트·키는 같다. 진행 눈금은 썸네일 수 + 원본 수다. 실패 목록이 답.
    """
    selections: dict[str, _SourceSelection] = {}
    seek_plans: dict[str, preprocess.FrameSeekPlan | None] = {}
    failures: list[str] = []
    sources = {segment.source_id for segment in page_segments}
    context.set_total(len(page_segments) + len(sources))
    done = 0
    for segment in page_segments:
        context.check_cancel()
        selection = selections.get(segment.source_id)
        if selection is None:
            selection = _chosen_source(workspace, revision, segment.source_id, choices, context)
            selections[segment.source_id] = selection
        _prepare_thumb(workspace, revision, segment, selection, failures, seek_plans)
        done += 1
        context.set_progress(done)
    for selection in selections.values():
        context.check_cancel()
        _ensure_full_source_preview(workspace, selection, failures)
        done += 1
        context.set_progress(done)
    return failures


def _chosen_source(
    workspace: RawWorkspace,
    revision: ReviewRevision,
    source_id: str,
    choices: Mapping[str, int | None],
    context: JobContext,
) -> _SourceSelection:
    """준비의 원본 선택 — 명시적 소리 선택이 있으면 그 서수, 없으면 기본(첫 트랙)."""
    explicit = source_id in choices
    return _select_source(
        workspace,
        revision,
        source_id,
        explicit=explicit,
        ordinal=choices[source_id] if explicit else None,
        job_id=context.job_id,
    )


def _select_source(
    workspace: RawWorkspace,
    revision: ReviewRevision,
    source_id: str,
    *,
    explicit: bool,
    ordinal: int | None,
    job_id: str | None = None,
) -> _SourceSelection:
    """원본의 검증·조사·스트림 선택 — 명시적 선택이 없으면 첫 트랙(없으면 침묵).

    소리가 여러 트랙이면 **고른 하나**만 미리보기에 들어간다 — 브라우저가 컨테이너의
    트랙을 못 고르기 때문이다(자막 미리보기 F2와 같은 이유). 명시적 선택에서
    서수가 트랙 수 밖이면 그 자리에서 실패한다(조용한 첫 트랙 환원 금지). 조사한
    트랙 목록은 기록으로 남겨 질의의 선택기를 채운다.
    """
    clock = _required_clock(revision, source_id)
    asset = workspace.source_asset(source_id)
    path = workspace.source_media_path(asset)
    _verify_source_unchanged(workspace, asset, path)
    from vlogsrt.project.raw.media_refs import resolve

    path = resolve(workspace, asset, "preview")
    probe = preprocess.probe_raw_media_memo(path)  # 원본을 방금 확인했다 — 기억(W-45)
    slots = _decode_slots(workspace, clock.source_sha256, path, probe)
    tag = preprocess.GRID_CONFORM_TAG if slots else None
    _write_inventory(workspace, source_id, clock.source_sha256, probe.audio, tag)
    if not explicit:
        audio_ordinal: int | None = 0 if probe.audio else None
        chosen = probe.audio[0] if probe.audio else None
    elif ordinal is None:
        audio_ordinal = None  # 명시적 침묵 — 트랙이 있어도 영상만 간다
        chosen = None
    elif ordinal >= len(probe.audio):
        raise VisualReviewError(
            f"원본 {source_id!r}({asset.source_name})에 소리 서수 {ordinal}이(가) 없다 — "
            f"트랙은 {len(probe.audio)}개다(0..{max(0, len(probe.audio) - 1)}). "
            "조용한 환원 대신 실패로 알린다"
        )
    else:
        audio_ordinal = ordinal
        chosen = probe.audio[ordinal]
    return _SourceSelection(
        source_id=source_id,
        source_sha256=clock.source_sha256,
        path=path,
        video_stream_index=probe.video.stream_index,
        audio_ordinal=audio_ordinal,
        audio_stream_index=chosen.stream_index if chosen is not None else None,
        job_id=job_id,
        slot_packets=slots,
    )


GRID_MARKER_DIRNAME = "cache/grid-conform"
"""원본 내용마다 맞춤 갈래의 표지(D70) — 계획이 잰 시계에서 남긴다. 내용 해시와 규칙 판에 묶여 늘
참이고, 지워지면(캐시 정리) 다음 준비가 한 번 다시 잰다."""
GRID_MARKER_SCHEMA = "grid-conform-marker/v1"


def _grid_marker(root: Path, source_sha256: str) -> Path:
    return root / GRID_MARKER_DIRNAME / f"{source_sha256}.json"


def note_grid_conform(root: Path, source_sha256: str, clock: preprocess.RawVideoClock) -> None:
    """원본 시계가 map인지 남긴다 — 준비가 map이 아닌 원본의 패킷을 다시 훑지 않게(D70).

    격자를 증명하지 못한 시계는 남기지 않는다(검토 머리에 들지 못한다). 같은 표지면 쓰지 않는다.
    """
    if not clock.uniform_grid:
        return
    payload: dict[str, object] = {
        "schema": GRID_MARKER_SCHEMA,
        "rule": preprocess.GRID_CONFORM_TAG,
        "source_sha256": source_sha256,
        "mapped": clock.conform_kind == "map",
    }
    target = _grid_marker(root, source_sha256)
    if _read_record(target) != payload:
        _write_record(target, payload)


def marked_mapped(root: Path, source_sha256: str) -> bool:
    """지금 규칙 판의 표지가 「map」인가 — 표지가 없거나 다른 판이면 거짓(작업대 레일, W-46b).

    레일의 「내보낼 부분」 띠는 map 원본을 비운다(초안 렌더가 통째로 뺀다, D68). 표지가 없으면
    아직 재지 않은 원본이다 — 거짓으로 읽고(균일·snap과 같은 띠), 렌더가 그때 잰 시계로 정한다.
    """
    record = _read_record(_grid_marker(root, source_sha256))
    return (
        record is not None
        and record.get("schema") == GRID_MARKER_SCHEMA
        and record.get("rule") == preprocess.GRID_CONFORM_TAG
        and record.get("mapped") is True
    )


def _marked_unmapped(root: Path, source_sha256: str) -> bool:
    """지금 규칙 판의 표지가 「map 아님」인가 — 표지가 없거나 다른 판이면 거짓(다시 잰다)."""
    record = _read_record(_grid_marker(root, source_sha256))
    return (
        record is not None
        and record.get("schema") == GRID_MARKER_SCHEMA
        and record.get("rule") == preprocess.GRID_CONFORM_TAG
        and record.get("mapped") is False
    )


def _decode_slots(
    workspace: RawWorkspace, source_sha256: str, path: Path, probe: preprocess.RawMediaProbe
) -> tuple[int, ...]:
    """디코드할 파일의 map 대응(D70) — 균일·snap은 빈 튜플이고 패킷을 훑지 않는다.

    계획이 남긴 표지(`note_grid_conform`)가 「map 아님」이면 그대로 돌아간다 — 균일·snap 원본의
    준비는 D70 앞과 같은 ffprobe만 돈다. map이거나 표지가 없으면(D70 앞에 세운 머리) 이 파일의
    시계를 재고 표지를 남긴다. 디코드할 파일은 원본이거나 같은 대응으로 검증된 프록시다
    (`media_refs._lattice_rejection`).
    """
    if _marked_unmapped(workspace.root, source_sha256):
        return ()
    clock = preprocess.probe_extraction_clock(path, probe=probe)
    note_grid_conform(workspace.root, source_sha256, clock)
    return clock.slot_packets if clock.uniform_grid else ()


def _verify_source_unchanged(workspace: RawWorkspace, asset: object, path: Path) -> None:
    """ready 원본의 재검증 — 크기를 지금 다시 읽고, 내용은 stat이 같으면 기록을 믿는다(D62).

    서비스의 `_verified_asset`·검토 파사드의 `_verify_source_unchanged`와 같은 문장을
    여기 다시 쓴다 — 이 모듈은 서비스를 import하지 않아야 순환이 없다(W1 계약). 미리보기는
    표시용 재생본이라 카드를 누를 때마다 9GB를 다시 해시하지 않는다(`source_verify`) — stat이
    지난 완전 검증과 다르면 예전대로 완전 해시하고, 다르면 같은 문장으로 거부한다.
    """
    status = getattr(asset, "status", None)
    recorded_sha = getattr(asset, "content_sha256", None)
    recorded_size = getattr(asset, "size_bytes", None)
    if status != "ready" or recorded_sha is None:
        raise VisualReviewError(
            f"원본이 검증 준비 상태가 아니다: {getattr(asset, 'source_relative_path', '?')}"
            f" ({status})"
        )
    probed = path.stat()
    if probed.st_size != recorded_size:
        raise VisualReviewError(
            f"원본 크기가 가져오기 때와 다르다: {path} ({recorded_size} → {probed.st_size}B)"
        )
    if source_verify.source_digest(workspace, asset, path, trust_stat=True) != recorded_sha:
        raise VisualReviewError(f"원본 내용이 가져오기 때와 다르다: {path} — 다시 가져오세요")


def _seek_plan_for(
    selection: _SourceSelection, plans: dict[str, preprocess.FrameSeekPlan | None]
) -> preprocess.FrameSeekPlan | None:
    """이 원본의 빠른 프레임 찾기 증명 — 원본마다 한 번만 잰다(디먹스만, D62)."""
    if selection.source_id not in plans:
        margin = Fraction(REVIEW_MEDIA.thumb_seek_margin_seconds)
        plans[selection.source_id] = preprocess.probe_frame_seek_plan(
            selection.path, selection.video_stream_index, margin_seconds=margin
        )
    return plans[selection.source_id]


def _prepare_thumb(
    workspace: RawWorkspace,
    revision: ReviewRevision,
    segment: ReviewSegment,
    selection: _SourceSelection,
    failures: list[str],
    seek_plans: dict[str, preprocess.FrameSeekPlan | None] | None = None,
) -> None:
    """조각 하나의 썸네일 — 이미 있으면 ffmpeg을 돌리지 않고 돌아간다.

    만들 때는 빠른 프레임 찾기를 쓴다(증명이 없으면 처음부터 디코드 — 바이트는 같다, D62)
    그리고 낮은 OS 우선순위로 돈다 — 분석과 CPU를 다투지 않는다.
    """
    clock = _required_clock(revision, segment.source_id)
    frame_index, _, _ = _snapped_fields(clock, segment.start_pts, segment.end_pts)
    mapped = bool(selection.slot_packets)
    key = thumbnail_media_key(selection.source_sha256, frame_index, mapped=mapped)
    relative = _thumb_relative(key)
    target = workspace.root / relative
    if target.is_file():
        _ensure_registered(workspace, target, relative, rebuilt=False)
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(
            f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.partial.jpg"
        )
        # 「지금 도는 작업」이 일반 이름(프레임 추출) 대신 사람이 기다리는 단계를 말한다(D61).
        with preprocess.process_step("썸네일 추출"):
            seek = _seek_plan_for(selection, {} if seek_plans is None else seek_plans)
            preprocess.extract_frame_index(
                selection.path,
                selection.video_stream_index,
                frame_index,
                staged,
                long_edge=VISUAL_THUMB_LONG_EDGE,
                seek=seek,
                priority=REVIEW_MEDIA.process_niceness,
                slot_packets=selection.slot_packets,
            )
        _publish(staged, target)
    except RuntimeError as error:
        _write_record(
            workspace.root / _thumb_failure_relative(key),
            _thumb_record_payload(selection, key, frame_index, "failed", str(error)),
        )
        failures.append(f"썸네일 {segment.segment_id!r} (프레임 {frame_index}): {error}")
        return
    (workspace.root / _thumb_failure_relative(key)).unlink(missing_ok=True)
    _ensure_registered(workspace, target, relative, rebuilt=True)


def _thumb_record_payload(
    selection: _SourceSelection, key: str, frame_index: int, state: str, error: str
) -> dict[str, object]:
    """썸네일 실패 기록의 본문 — 성공 기록은 파일 그 자체다(상태 유도 참조)."""
    return {
        "schema": VISUAL_THUMB_RECIPE,
        "key": key,
        "source_id": selection.source_id,
        "source_sha256": selection.source_sha256,
        "frame_index": frame_index,
        "long_edge": VISUAL_THUMB_LONG_EDGE,
        "state": state,
        "error": error,
        "built_at": _now(),
    }


def _publish(staged: Path, target: Path) -> None:
    """임시 파일의 원자 게시 — 경합에서 이기지 못하면 이긴 쪽 파일을 그대로 쓴다."""
    try:
        publish_exclusive(staged, target)
    except FileExistsError:
        return  # 동시 준비가 같은 키를 먼저 완성했다 — 그 파일이 곧 결과다
    finally:
        staged.unlink(missing_ok=True)


def _legacy_preview_key(selection: _SourceSelection) -> str:
    """지문이 키에 들어가기 전(69fc88c)의 원본 전체 미리보기 키 — 원본 해시·서수만.

    그 시절 키는 `preview_media_key(source_sha256, audio_ordinal)`이었고, 입력은 언제나
    자산의 해시 검증된 바이트 그 자체였다. 조리법(`VISUAL_PREVIEW_RECIPE`)은 그대로이니
    그 키의 완성본은 지금의 정본 지문(`original-only/v1`) 키와 **같은 입력의 같은 결과**다.
    """
    return preview_media_key(selection.source_sha256, selection.audio_ordinal)


def _alias_legacy_preview(
    workspace: RawWorkspace, selection: _SourceSelection, media_fingerprint: str, target: Path
) -> None:
    """옛 키의 완성본을 정본 키 이름으로 잇는다 — 입력이 원본 바이트 그대로일 때만.

    라이브러리 가져오기가 모든 클립에 미디어 연결 기록을 남기며 키가 바뀌어, 이미
    만든 4K 미리보기가 있는데도 다시 인코딩을 시작했다(실측 7건). 지문이 정본이면
    입력이 자산의 바이트 그 자체라 옛 완성본이 곧 정본의 결과다 — 프록시(비정본
    지문)는 다른 입력이니 절대 잇지 않는다. 게시는 기존 게시와 같은 link 한 번이고,
    경합에서 지면(이미 있다) 이긴 쪽 파일이 곧 결과다.
    """
    if media_fingerprint != ORIGINAL_ONLY_FINGERPRINT:
        return
    legacy = workspace.root / _preview_relative(_legacy_preview_key(selection))
    if not legacy.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        publish_exclusive(legacy, target)
    except FileExistsError:
        return  # 동시 준비가 같은 키를 먼저 게시했다 — 그 파일이 곧 결과다


def _ensure_full_source_preview(
    workspace: RawWorkspace, selection: _SourceSelection, failures: list[str]
) -> None:
    """원본 전체의 브라우저 MP4 — 최종 이름의 파일은 임시 파일의 원자 게시로만 생긴다.

    그러므로 그 이름에 있는 MP4는 언제나 검증을 통과한 완성본이다 — 다시 만들거나
    지우지 않고, 기록이 없으면 **잰 값으로 기록을 보강**한다(§14.2의 캐시 재사용
    검사). 파일이 없을 때만 만든다: 스테이징 안전장치·link 게시는 추출 경계가
    가진다. 게시 경합에서 지면(이미 있다) 이긴 쪽 파일을 그대로 쓴다 — 어느 순간에도
    최종 이름의 파일이 지워지는 일이 없으므로 준비끼리 서로를 깨뜨릴 수 없다.
    """
    from vlogsrt.project.raw import media_refs

    media_fingerprint = media_refs.fingerprint(workspace, selection.source_id)
    key = preview_media_key(selection.source_sha256 + media_fingerprint, selection.audio_ordinal)
    relative = _preview_relative(key)
    record_path = workspace.root / _preview_record_relative(key)
    target = workspace.root / relative
    record = _read_record(record_path)
    refs = media_refs.load(workspace, selection.source_id)
    probe = preprocess.probe_raw_media_memo(selection.path)  # `_select_source`가 확인했다
    if (
        refs
        and refs["validation"]["status"] in ("verified", "proxy_only")
        and selection.path == Path(refs["proxy"]["path"])
        and selection.path.suffix.lower() in (".mp4", ".m4v")
        and probe.video.codec_name == "h264"
        and len(probe.audio) <= 1
        and all(a.codec_name == "aac" for a in probe.audio)
        and (not probe.audio or selection.audio_ordinal == 0)
    ):
        direct = media_refs.register_direct(workspace, selection.source_id, selection.path)
        payload = _source_record_payload(
            selection, key, "ready", None, _measure_preview(selection.path)
        )
        payload.update(direct_file_id=direct, media_fingerprint=media_fingerprint)
        _write_record(record_path, payload)
        return
    if not target.is_file():
        _alias_legacy_preview(workspace, selection, media_fingerprint, target)
    if target.is_file():
        if record is None or record.get("state") != "ready":
            _write_record(
                record_path,
                _source_record_payload(selection, key, "ready", None, _measure_preview(target)),
            )
        _ensure_registered(workspace, target, relative, rebuilt=False)
        return
    _write_record(record_path, _source_record_payload(selection, key, "preparing", None, None))
    try:
        # 로컬 미디어 동시 한도(D100 §2) — 분석 프록시와 같은 자리를 다툰다.
        with media_refs.local_media_slot():
            extraction = preprocess.extract_visual_review_preview(
                selection.path,
                target,
                audio_stream_index=selection.audio_stream_index,
                encoding=_preview_encoding(probe.video),
                # 한도는 원본 길이를 따라 자란다 — 0018(1359초)이 고정 1800초에 걸렸다(D62).
                timeout_seconds=REVIEW_MEDIA.preview_timeout(float(probe.duration)),
                priority=REVIEW_MEDIA.process_niceness,
            )
        built = True
    except RuntimeError as error:
        if "게시 경로가 이미 있다" not in str(error):
            _write_record(
                record_path, _source_record_payload(selection, key, "failed", str(error), None)
            )
            failures.append(f"원본 전체 미리보기 {selection.source_id!r}: {error}")
            return
        built = False  # 동시 준비가 같은 키를 먼저 게시했다 — 그 파일이 곧 결과다
        if not target.is_file():
            _write_record(
                record_path,
                _source_record_payload(selection, key, "failed", str(error), None),
            )
            failures.append(f"원본 전체 미리보기 {selection.source_id!r}: {error}")
            return
        extraction = _measure_preview(target)
    except OSError as error:
        _write_record(
            record_path, _source_record_payload(selection, key, "failed", str(error), None)
        )
        failures.append(f"원본 전체 미리보기 {selection.source_id!r}: {error}")
        return
    _write_record(record_path, _source_record_payload(selection, key, "ready", None, extraction))
    _ensure_registered(workspace, target, relative, rebuilt=built)


def _preview_encoding(video: preprocess.RawVideoMedia) -> preprocess.PreviewEncoding:
    """새로 만드는 미리보기의 조리법 — 설정에서 온다. 캐시 열쇠에는 들어가지 않는다(D62).

    4K 원본(D100 §2)은 긴 변 `preview_long_edge_4k`로, 브라우저가 그대로 재생하는 H.264여도 줄인다.
    열쇠에 긴 변이 없으므로 이미 만든 4K 미리보기(1920 또는 복사본)는 그대로 다시 쓴다.
    """
    uhd = FOLDER_WORKSPACE.is_uhd(video.width, video.height)
    return preprocess.PreviewEncoding(
        long_edge=REVIEW_MEDIA.preview_long_edge_4k if uhd else REVIEW_MEDIA.preview_long_edge,
        preset=REVIEW_MEDIA.preview_x264_preset,
        crf=REVIEW_MEDIA.preview_x264_crf,
        shrink_copyable=uhd,
        # 하드웨어 인코더가 먼저(W-45, D100 §6) — 고른 이름은 기록의 `encoding`에만 남는다.
        video_codecs=REVIEW_MEDIA.preview_video_codecs,
        hardware_bits_per_pixel=REVIEW_MEDIA.preview_hardware_bits_per_pixel,
        threads=FOLDER_WORKSPACE.local_media.encode_threads(os.cpu_count()),
    )


def _measure_preview(target: Path) -> preprocess.VisualPreviewExtraction:
    """이미 게시된 미리보기의 측정 — 기록 보강용, 값을 지어내지 않는다(전부 잰 값)."""
    probe = preprocess.probe_raw_media(target)
    clock = preprocess.probe_extraction_clock(target, probe=probe)
    audio = probe.audio[0] if probe.audio else None
    return preprocess.VisualPreviewExtraction(
        path=target,
        remuxed=False,
        video_codec=probe.video.codec_name,
        audio_codec=audio.codec_name if audio is not None else None,
        audio_stream_count=len(probe.audio),
        duration=probe.duration,
        video_origin=clock.presentation_origin,
        audio_origin=audio.start_time if audio is not None else None,
        width=probe.video.width,
        height=probe.video.height,
        rotation_degrees=probe.video.rotation_degrees,
        source_video_codec=probe.video.codec_name,
        source_audio_codec=audio.codec_name if audio is not None else None,
        ffmpeg_version=preprocess.ffmpeg_provenance().splitlines()[0],
        warnings=(),
    )


def _browser_origin(extraction: preprocess.VisualPreviewExtraction) -> Fraction:
    """브라우저 시간표의 원점 이동 — **출력 영상 첫 패킷 PTS** 그 자체(가산).

    미리보기 인코딩은 -copyts가 아니어서 ffmpeg이 입력의 선언 시작만큼 전체
    스트림을 함께 당긴다. 그 이동은 소리 선택과 무관한 컨테이너 사실이다(실측:
    영상 2초·소리0 1.372초·소리1 2.186초 원본에서 트랙 0/1/침묵 세 미리보기 모두
    출력 영상 원점 0.627969초, 디코드 대응 mse 0). 소리 원점 뺄셈 공식은 늦은
    트랙·침묵에서 0을 거짓으로 내놓아 폐기했다(A3 재현). 영상이 가장 이른
    스트림이면 0 — 지금까지 통과한 항등 매핑 전부다. `audio_origin`은 증거로
    기록에만 남는다.
    """
    return extraction.video_origin


def _source_record_payload(
    selection: _SourceSelection,
    key: str,
    state: str,
    error: str | None,
    extraction: preprocess.VisualPreviewExtraction | None,
) -> dict[str, object]:
    """원본 미리보기 기록의 본문 — 측정값은 추출 결과에서만 온다."""
    payload: dict[str, object] = {
        "schema": VISUAL_PREVIEW_RECIPE,
        "key": key,
        "source_id": selection.source_id,
        "source_sha256": selection.source_sha256,
        "video_stream_index": selection.video_stream_index,
        "audio_ordinal": selection.audio_ordinal,
        "audio_stream_index": selection.audio_stream_index,
        "state": state,
        "error": error,
        "built_at": _now(),
    }
    if state == "preparing":
        # 만드는 쪽의 신원 — 작업 번호로 받침을, pid로 서버 시작 정리를 가른다(D62).
        payload.update(job_id=selection.job_id, pid=os.getpid())
    if extraction is not None:
        payload.update(
            {
                "encoding": extraction.encoding,
                "duration": str(extraction.duration),
                "video_origin_seconds": str(extraction.video_origin),
                "audio_origin_seconds": (
                    str(extraction.audio_origin) if extraction.audio_origin is not None else None
                ),
                "browser_origin_seconds": str(_browser_origin(extraction)),
                "video_codec": extraction.video_codec,
                "audio_codec": extraction.audio_codec,
                "width": extraction.width,
                "height": extraction.height,
                "rotation_degrees": extraction.rotation_degrees,
                "remuxed": extraction.remuxed,
                "source_video_codec": extraction.source_video_codec,
                "source_audio_codec": extraction.source_audio_codec,
                "ffmpeg_version": extraction.ffmpeg_version,
                "warnings": list(extraction.warnings),
            }
        )
    return payload


def _ensure_registered(
    workspace: RawWorkspace, target: Path, relative: Path, *, rebuilt: bool
) -> MediaRegistration:
    """미리보기의 등록 행 — 같은 경로의 행을 재사용하고, 다시 만들었으면 지문을 다시 박는다.

    정리(purge)는 파일만 지우고 행은 남긴다. 다시 만든 파일은 지문(size·mtime)이
    어긋나므로 같은 경로의 행에 `refresh_media_fingerprint`로 새 지문을 박는다 —
    낡은 file_id를 그대로 다시 쓰고(제공 안전망은 그대로), 어긋난 지문의 문은
    닫힌다(자막 미리보기와 같은 회복 길). `rebuilt=False`는 짝이 이미 온전할 때의
    등록 보강(행만 없는 경우)이다 — 지문을 건드리지 않는다.
    """
    existing = _registration_for(workspace, relative)
    if existing is None:
        return workspace.register_media_file(target, "preview")
    if not rebuilt:
        return existing
    workspace.refresh_media_fingerprint(existing.file_id)
    probed = target.lstat()
    return MediaRegistration(
        file_id=existing.file_id,
        kind=existing.kind,
        relative_path=existing.relative_path,
        status=existing.status,
        size_bytes=probed.st_size,
        registered_at=existing.registered_at,
    )


# ── 편집 제안 — 저장보다 앞서고, 아무 것도 쓰지 않는다 ────────────────────────


def _proposal(
    kind: str,
    segment_ids: tuple[str, ...],
    head: ReviewRevision,
    clock: SourceClock,
    desired_pts: Fraction,
    valid: bool,
    reason: str | None,
) -> VisualSnapProposal:
    """제안 값 조립 — 스냅은 늘 서버가 잰 점(동률 앞장)이고 원하는 점은 정확히 보존한다."""
    snap = clock.origin_pts + snapped_frame_index(clock, desired_pts) * clock.frame_ticks
    return VisualSnapProposal(
        kind=kind,
        segment_ids=segment_ids,
        desired_pts=desired_pts,
        desired_seconds=str((desired_pts - clock.origin_pts) * clock.time_base),
        snap_pts=snap,
        snap_seconds=str((snap - clock.origin_pts) * clock.time_base),
        head_revision_id=head.revision_id,
        valid=valid,
        reason=reason,
    )


def snap_proposal(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str | None = None,
    expected_revision_id: str,
    segment_id: str,
    desired_pts: Fraction | int,
) -> VisualSnapProposal:
    """분할 점의 읽기 전용 제안 — 화면이 본 리비전에 묶이고 스냅·길이 0 판정만 한다.

    `expected_revision_id`는 화면이 지금 보여주는 리비전이다 — 머리가 앞서 갔으면
    조각이 그대로 살아남았더라도 `StaleRevisionError`로 거부한다. 제안이 옛 사건에
    새 머리의 저장 토큰을 몰래 발급하는 일은 없다(유효 저장 토큰은 언제나 새로
    고침한 리비전의 제안에서만 나온다). `desired_pts`는 서브틱 유리수까지 정확히
    받는다(int·Fraction; float·무한·NaN은 거부) — 정수 프레임 PTS는 스냅 결과뿐.
    """
    head = facade.load_head(base_plan_id).revision
    require_head_revision(head, expected_revision_id)
    segment = _segment_of(head, segment_id)
    clock = _required_clock(head, segment.source_id)
    desired = _exact_pts(desired_pts, "원하는 분할 점")
    snap = clock.origin_pts + snapped_frame_index(clock, desired) * clock.frame_ticks
    if not segment.start_pts < snap < segment.end_pts:
        return _proposal(
            "split",
            (segment_id,),
            head,
            clock,
            desired,
            False,
            f"스냅한 점 {snap}이(가) 조각 [{segment.start_pts}, {segment.end_pts}) 안쪽"
            " 엄격히 안이 아니다 — 길이 0인 조각은 만들지 않는다",
        )
    return _proposal("split", (segment_id,), head, clock, desired, True, None)


def boundary_proposal(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str | None = None,
    expected_revision_id: str,
    left_segment_id: str,
    right_segment_id: str,
    desired_pts: Fraction | int,
) -> VisualSnapProposal:
    """경계 이동의 읽기 전용 제안 — 맞닿음·길이 0 판정까지, 본 리비전 묶음으로 미리 본다."""
    head = facade.load_head(base_plan_id).revision
    require_head_revision(head, expected_revision_id)
    left = _segment_of(head, left_segment_id)
    right = _segment_of(head, right_segment_id)
    clock = _required_clock(head, left.source_id)
    desired = _exact_pts(desired_pts, "원하는 경계 점")
    if left.source_id != right.source_id:
        return _proposal(
            "boundary",
            (left_segment_id, right_segment_id),
            head,
            clock,
            desired,
            False,
            "경계 조정은 같은 원본 안에서만 가능하다",
        )
    if left.end_pts != right.start_pts:
        return _proposal(
            "boundary",
            (left_segment_id, right_segment_id),
            head,
            clock,
            desired,
            False,
            f"두 조각이 맞닿지 않는다({left.end_pts} != {right.start_pts})"
            " — 건너뛴 이웃은 못 잇는다",
        )
    snap = clock.origin_pts + snapped_frame_index(clock, desired) * clock.frame_ticks
    if not left.start_pts < snap < right.end_pts:
        return _proposal(
            "boundary",
            (left_segment_id, right_segment_id),
            head,
            clock,
            desired,
            False,
            f"스냅한 경계 {snap}이(가) 합친 내부 [{left.start_pts}, {right.end_pts}) 엄격히"
            " 안이 아니다 — 어느 한쪽이 길이 0이 되는 이동은 만들지 않는다",
        )
    return _proposal(
        "boundary", (left_segment_id, right_segment_id), head, clock, desired, True, None
    )


# ── 편집 — 화면이 준 리비전에 묶인다 ──────────────────────────────────────────


def _head_or_stale(
    facade: ReviewWorkspaceFacade, base_plan_id: str | None, expected_revision_id: str
) -> ReviewRevision:
    """지금 헤드가 화면이 본 리비전인가 — 아니면 stale로 거부한다(0손상)."""
    head = facade.load_head(base_plan_id).revision
    require_head_revision(head, expected_revision_id)
    return head


def _segment_of(revision: ReviewRevision, segment_id: str) -> ReviewSegment:
    for segment in revision.segments:
        if segment.segment_id == segment_id:
            return segment
    raise VisualReviewError(f"리비전 {revision.revision_id!r}에 조각 {segment_id!r}가 없다")


def _require_frame_point(clock: SourceClock, pts: int, what: str) -> None:
    if not clock.is_frame_boundary(pts):
        raise VisualReviewError(f"{what} {pts}는 원본 {clock.source_id!r}의 프레임 격자 밖이다")


def split_segment(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str,
    expected_revision_id: str,
    segment_id: str,
    split_pts: int,
) -> ReviewRevision:
    """조각을 둘로 나눈다 — 길이 0이 되는 자리는 빠르게 거부하고 파사드가 출판한다."""
    head = _head_or_stale(facade, base_plan_id, expected_revision_id)
    segment = _segment_of(head, segment_id)
    if not segment.start_pts < split_pts < segment.end_pts:
        raise VisualReviewError(
            f"분할 점 {split_pts}가 조각 {segment_id!r} 안쪽 엄격히 안이 아니다"
            f" [{segment.start_pts}, {segment.end_pts}) — 길이 0인 조각은 만들지 않는다"
        )
    _require_frame_point(_required_clock(head, segment.source_id), split_pts, "분할 점")
    edit: ReviewEdit = split_edit(expected_revision_id, segment_id, split_pts)
    return facade.apply_edit(base_plan_id, edit)


def adjust_boundary(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str,
    expected_revision_id: str,
    left_segment_id: str,
    right_segment_id: str,
    boundary_pts: int,
) -> ReviewRevision:
    """맞닿은 두 조각의 경계를 옮긴다 — 안 닿은 짝과 길이 0이 되는 이동은 거부다."""
    head = _head_or_stale(facade, base_plan_id, expected_revision_id)
    left = _segment_of(head, left_segment_id)
    right = _segment_of(head, right_segment_id)
    if left.source_id != right.source_id:
        raise VisualReviewError(
            f"경계 조정은 같은 원본 안에서만 가능하다: {left_segment_id!r} vs {right_segment_id!r}"
        )
    if left.end_pts != right.start_pts:
        raise VisualReviewError(
            f"경계 조정은 맞닿은 조각에만 가능하다: {left_segment_id!r}의 끝 {left.end_pts}와"
            f" {right_segment_id!r}의 시작 {right.start_pts}가 다르다 — 건너뛴 이웃은 못 잇는다"
        )
    if not left.start_pts < boundary_pts < right.end_pts:
        raise VisualReviewError(
            f"새 경계 {boundary_pts}는 두 조각의 합친 내부 엄격히 안이어야 한다"
            f" [{left.start_pts}, {right.end_pts}) — 어느 한쪽이 길이 0이 되는 이동은"
            " 만들지 않는다"
        )
    _require_frame_point(_required_clock(head, left.source_id), boundary_pts, "새 경계")
    edit: ReviewEdit = boundary_edit(
        expected_revision_id, left_segment_id, right_segment_id, boundary_pts
    )
    return facade.apply_edit(base_plan_id, edit)


def merge_segments(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str,
    expected_revision_id: str,
    first_segment_id: str,
    second_segment_id: str,
) -> ReviewRevision:
    """같은 원본의 맞닿은 두 조각을 합친다 — 안 닿은 짝은 거부다(검토 도메인 규칙)."""
    head = _head_or_stale(facade, base_plan_id, expected_revision_id)
    first = _segment_of(head, first_segment_id)
    second = _segment_of(head, second_segment_id)
    if first.source_id != second.source_id or first.end_pts != second.start_pts:
        raise VisualReviewError(
            f"병합은 같은 원본의 맞닿은 조각에만 가능하다: {first_segment_id!r} ·"
            f" {second_segment_id!r}"
        )
    edit: ReviewEdit = merge_edit(expected_revision_id, first_segment_id, second_segment_id)
    return facade.apply_edit(base_plan_id, edit)


def _range_point(
    clock: SourceClock, pts: int | None, seconds: Fraction | int | None, what: str
) -> int:
    """범위 끝 하나의 PTS — PTS는 격자 위여야 하고, 초(원점 기준)는 원본 시계로 스냅한다.

    스냅은 분할 제안과 같다(`snapped_frame_index` — 가장 가까운 프레임, 정확히 한가운데면 앞장).
    둘 다 주거나 둘 다 없으면 거부다. float는 받지 않는다(`_exact_pts`).
    """
    if (pts is None) == (seconds is None):
        raise VisualReviewError(f"{what}은(는) PTS나 초 가운데 하나만 주어야 한다")
    if pts is not None:
        if isinstance(pts, bool) or not isinstance(pts, int):
            raise VisualReviewError(f"{what} PTS는 정수여야 한다: {pts!r}")
        _require_frame_point(clock, pts, what)
        return pts
    assert seconds is not None
    desired = clock.origin_pts + _exact_pts(seconds, what) / clock.time_base
    return clock.origin_pts + snapped_frame_index(clock, desired) * clock.frame_ticks


def mark_range(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str | None,
    expected_revision_id: str,
    source_id: str,
    mark: str,
    start_pts: int | None = None,
    end_pts: int | None = None,
    start_seconds: Fraction | int | None = None,
    end_seconds: Fraction | int | None = None,
) -> VisualRangeMark:
    """원본 하나의 `[시작, 끝)`을 추천·보통·비추천으로 표시하거나 지운다 — 리비전 하나(D100 §4)."""
    head = _head_or_stale(facade, base_plan_id, expected_revision_id)
    clock = _required_clock(head, source_id)
    if mark not in RANGE_MARKS:
        raise VisualReviewError(f"알 수 없는 범위 표시 {mark!r} — {RANGE_MARKS} 가운데 하나다")
    start = _range_point(clock, start_pts, start_seconds, "범위 시작")
    end = _range_point(clock, end_pts, end_seconds, "범위 끝")
    if not clock.origin_pts <= start < end <= clock.end_pts:
        raise VisualReviewError(
            f"범위 [{start}, {end})가 비었거나 뒤집혔거나 원본 {source_id!r}"
            f" [{clock.origin_pts}, {clock.end_pts}] 밖이다"
        )
    edited = facade.apply_edit(head.base_plan_id, range_edit(head, source_id, start, end, mark))
    return VisualRangeMark(
        revision=edited,
        head_revision_id=edited.revision_id,
        source_id=source_id,
        start_pts=start,
        end_pts=end,
        mark=mark,
        marked_segment_id=range_segment(edited, source_id, start).segment_id,
        segments=tuple(s for s in chronological(edited) if s.source_id == source_id),
    )


def set_selection(
    facade: ReviewWorkspaceFacade,
    *,
    base_plan_id: str,
    expected_revision_id: str,
    segment_id: str,
    selection: str,
) -> ReviewRevision:
    """사용/제외 표시를 바꾼다 — 조각은 지워지지 않고 표시만 바뀐다(§6.4)."""
    _head_or_stale(facade, base_plan_id, expected_revision_id)
    if selection not in SELECTIONS:
        raise VisualReviewError(f"알 수 없는 선택 {selection!r}")
    edit: ReviewEdit = selection_edit(expected_revision_id, segment_id, selection)
    return facade.apply_edit(base_plan_id, edit)
