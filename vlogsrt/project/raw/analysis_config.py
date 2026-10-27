"""첫 단계(A4) 의미 분석의 동결 설정 — 창·프레임·청크 한도의 값 원천.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.4·§15.1의 `[understanding]`
값을 코드보다 먼저 못 박는다. 모델 이름은 여기에 없다 — 문구·시간·Luna 역할은
`raw/config.py`의 `RawModelRoles`가 이미 소유하므로 이 설정은 **값으로만** 읽는다
(모델 리터럴은 허용된 설정 경로에만 산다, 불변 규칙).

기본값은 계획의 초기 기준 그대로다 — 창 15초·문맥 2초·창당 8장·긴변 768.
ASR 청크 한도는 엔진의 동결 상한(180초·24MB)을 **천장으로** 다시 못 박는다:
이 설정을 낮추는 것은 회차별 조정이지만 올리는 것은 D26·D28이 닫은 길을
다시 여는 것이므로 검사가 거부한다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from fractions import Fraction
from typing import Any

UNDERSTANDING_SCHEMA_VERSION = "raw-understanding/v1"
"""첫 단계 기록(analysis_transcript.json)의 namespace(계획 §13.1·§15.1)."""

DEFAULT_PROMPT_REVISION = "raw-suitability-v3"
"""Luna 안내문의 리비전 — 캐시 지문과 승인에 함께 실린다(§14.2).

v3(W-18 18b, D58): `recommended`의 엄격한 기준과 schema의 `visual_issues`. 안내문·schema는
코드에 있으므로 리비전을 올려야 v2로 끝난 분석이 `visual_issues` 없이 다시 쓰이지 않는다.
"""

TRANSCRIPTION_IDENTITY_PROMPT_REVISION = "raw-suitability-v2"
"""1단계(전사) 승인 지문이 안내문 리비전 자리에 묶는 고정값(D58 18b).

전사는 의미 분석 안내문을 듣지 않는다. 안내문을 고칠 때마다 전사 승인 지문이 흔들리지 않도록
그 자리를 18b 이전 기본값으로 고정한다 — 기본 설정의 지문은 W-14 이전과 바이트까지 같다.
`DEFAULT_PROMPT_REVISION`을 가리키게 하지 마라: 그 이름이 바뀌어도 이 값은 그대로여야 한다.
"""

MAX_ASR_CHUNK_SECONDS = 180.0
"""엔진의 동결 상한(D26·D28). 이 값을 넘는 청크 설정은 만들 수 없다."""

MAX_ASR_CHUNK_BYTES = 24_000_000
"""오디오 API의 25MB 한도 − 안전 여유. 청크 바이트 상한도 같은 선에 둔다."""


@dataclass(frozen=True)
class RawUnderstandingSettings:
    """첫 단계 분석의 조정값 — 크기·시간·개수는 코드의 매직 넘버가 아니라 여기서.

    `window_seconds`·`context_seconds`는 **분석 시간창**의 크기다(§7.4). 실제
    편집 조각의 길이가 아니다 — 조각은 Luna 판정 뒤 결정적 분할이 정한다.
    """

    schema_version: str = UNDERSTANDING_SCHEMA_VERSION
    window_seconds: float = 15.0
    context_seconds: float = 2.0
    max_images_per_window: int = 8
    image_long_edge: int = 768
    prompt_revision: str = DEFAULT_PROMPT_REVISION
    asr_chunk_max_seconds: float = MAX_ASR_CHUNK_SECONDS
    asr_chunk_max_bytes: int = MAX_ASR_CHUNK_BYTES


def validate_understanding_settings(settings: RawUnderstandingSettings) -> None:
    """설정의 값역 검사 — 어긋나면 ValueError로 전부 보고한다.

    NaN은 비교를 무력화하므로 유한성부터 본다(`vision/policy.py`의 재검사와 같은
    이유). bool은 int 서브클래스지만 개수·길이 좌표가 될 수 없다.
    """
    problems = understanding_settings_problems(settings)
    if problems:
        raise ValueError("understanding 설정이 계약을 어긴다: " + "; ".join(problems))


def understanding_settings_problems(settings: RawUnderstandingSettings) -> list[str]:
    """값역 검사의 본체 — 조립 문과 검사 문이 같은 검사를 지나게 한다."""
    problems: list[str] = []
    if settings.schema_version != UNDERSTANDING_SCHEMA_VERSION:
        problems.append(f"understanding schema는 {UNDERSTANDING_SCHEMA_VERSION}여야 한다")
    for name, value, ceiling in (
        ("창 길이", settings.window_seconds, None),
        ("문맥 길이", settings.context_seconds, None),
        ("ASR 청크 시간 상한", settings.asr_chunk_max_seconds, MAX_ASR_CHUNK_SECONDS),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{name}은 실수여야 한다: {value!r}")
            continue
        if not math.isfinite(float(value)):
            problems.append(f"{name}이 유한하지 않다: {value!r}")
        elif float(value) <= 0.0:
            problems.append(f"{name}은 양수여야 한다: {value!r}")
        elif ceiling is not None and float(value) > ceiling:
            problems.append(f"{name}은 동결 상한 {ceiling}초를 넘지 못한다: {value!r}")
    if (
        isinstance(settings.context_seconds, (int, float))
        and isinstance(settings.window_seconds, (int, float))
        and settings.context_seconds > settings.window_seconds
    ):
        problems.append("문맥이 창보다 길 수는 없다")
    for name, value, ceiling in (
        ("창당 이미지 한도", settings.max_images_per_window, None),
        ("이미지 긴변", settings.image_long_edge, None),
        ("ASR 청크 바이트 상한", settings.asr_chunk_max_bytes, MAX_ASR_CHUNK_BYTES),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{name}은 정수여야 한다: {value!r}")
        elif value < 1:
            problems.append(f"{name}은 1 이상이어야 한다: {value!r}")
        elif ceiling is not None and value > ceiling:
            problems.append(f"{name}은 동결 상한 {ceiling}을 넘지 못한다: {value!r}")
    if not isinstance(settings.prompt_revision, str) or not settings.prompt_revision.strip():
        problems.append(
            f"프롬프트 리비전은 비지 않은 문자열이어야 한다: {settings.prompt_revision!r}"
        )
    return problems


@dataclass(frozen=True)
class AsrChunkCutSettings:
    """1단계 청크를 조용한 지점에서 자르는 찾는 폭·칸(D74) — `RawUnderstandingSettings` 밖이다.

    값은 원본 엔진의 `Config.cut_search_seconds`·`cut_quiet_window_ms` **그대로**다(시험이 같음을
    못 박는다 — 엔진을 바꾸면 같이 바꾼다). 조용함의 임계값은 없다 — 폭 안에서 가장 조용한 칸을
    고를 뿐이라 온통 시끄러워도 목표 가까이에서 자르고 아무것도 버리지 않는다(규칙 4). 이해 설정의
    칸이 아니어서 1단계 승인 지문·청크 열쇠의 설정 해시에 들지 않는다 — 자른 자리는 청크 범위로
    열쇠에 실린다."""

    search_seconds: Fraction = Fraction(5)
    hop_ms: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.search_seconds, Fraction) or self.search_seconds < 0:
            raise ValueError(f"찾는 폭은 0 이상의 유리수다: {self.search_seconds!r}")
        if isinstance(self.hop_ms, bool) or not isinstance(self.hop_ms, int) or self.hop_ms < 1:
            raise ValueError(f"칸은 1ms 이상의 정수다: {self.hop_ms!r}")


ASR_CHUNK_CUT = AsrChunkCutSettings()


ANALYSIS_PROXY_VERSION = "analysis-proxy/v1"
"""분석 프록시 조리법의 버전 — 인코더 인자를 바꾸면 올려서 낡은 프록시·프레임 캐시를 버린다."""


@dataclass(frozen=True)
class AnalysisProxyRecipe:
    """프록시 없는 원본을 분석할 때 한 번 만드는 저해상도 프록시의 조리법(2026-10-08 사용자 지시).

    4K HEVC에서 프레임 한 장을 뽑으려면 `select=eq(n,…)`가 스트림 앞에서부터 디코드한다 —
    그 비용을 한 번의 변환으로 바꾼다. 프레임 격자(개수·프레임 길이·원점)는 그대로 두고
    크기만 `scale` 배로 줄인다. Luna로 가는 그림은 여전히 `image_long_edge` 이하다 —
    이 설정은 디코드 속도의 것이지 비용의 것이 아니다. `gop`은 짧을수록 한 장 추출이 싸다.
    외부 근거 없는 조정 대상(`cut_*`처럼)이다.
    """

    version: str = ANALYSIS_PROXY_VERSION
    scale: float = 0.25
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 28
    gop: int = 12
    pix_fmt: str = "yuv420p"

    def tag(self) -> str:
        """프레임 증거가 이 프록시에서 왔음을 지문에 싣는 이름 — 조리법 전체가 담긴다."""
        return (
            f"{self.version}/scale={self.scale:g}/{self.video_codec}"
            f"/{self.preset}/crf{self.crf}/g{self.gop}/{self.pix_fmt}"
        )


ANALYSIS_PROXY = AnalysisProxyRecipe()
"""지금 쓰는 분석 프록시 조리법 — 모든 호출 지점이 이 하나를 읽는다."""


ANALYSIS_CLIP_VERSION = "analysis-clip/v1"
"""분석 클립 조리법의 버전 — 인코더 인자를 바꾸면 올려서 낡은 클립 캐시를 버린다."""


@dataclass(frozen=True)
class AnalysisClipRecipe:
    """Gemini가 창마다 직접 보는 짧은 클립(그림 + 소리)의 조리법(W-30c1, D85 — 사용자 지시
    2026-10-25 「영상을 직접 모델이 보고 평가해도 돼」).

    원본에서 창의 문맥 구간(창 15초 + 앞뒤 문맥 2초, 최대 19초)을 다시 인코딩한다 — 분석 프록시는
    소리가 없어(`-an`) 쓰지 못한다. 긴 변만 `long_edge`로 줄이고(키우지 않는다) 소리는 고른 트랙
    하나를 모노 AAC로 담는다. Gemini는 영상을 초당 한 장으로 표본하므로(video understanding 문서)
    크기를 키워도 판정이 나아지지 않는다 — 요청 전체가 20 MB(인라인 상한) 안에 들도록 작게
    둔다(`max_bytes`, 프레임 여덟 장의 base64 몫을 남긴다).

    **외부 근거 없는 조정 대상**(`cut_*`처럼): 긴 변·crf·비트레이트·채널. `tag()`는 바이트를 정하는
    칸만 싣는다 — 크기 상한·시간 한도는 기다림과 거절의 것이라 캐시 열쇠에 들지 않는다.
    """

    version: str = ANALYSIS_CLIP_VERSION
    long_edge: int = 640
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 30
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 64
    audio_channels: int = 1
    max_bytes: int = 12_000_000
    timeout_floor_seconds: float = 120.0
    timeout_media_ratio: float = 6.0

    def __post_init__(self) -> None:
        for name in ("long_edge", "audio_bitrate_kbps", "audio_channels", "max_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"분석 클립 {name}은 1 이상의 정수다: {value!r}")
        if isinstance(self.crf, bool) or not isinstance(self.crf, int) or not 0 <= self.crf <= 51:
            raise ValueError(f"분석 클립 crf는 0~51의 정수다: {self.crf!r}")
        for name in ("timeout_floor_seconds", "timeout_media_ratio"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"분석 클립 {name}은 유한한 양수다: {value!r}")

    def tag(self) -> str:
        """클립 바이트를 정하는 조리법 전체의 이름 — 클립 캐시 열쇠에 실린다."""
        return (
            f"{self.version}/edge={self.long_edge}/{self.video_codec}/{self.preset}/crf{self.crf}"
            f"/{self.pix_fmt}/{self.audio_codec}/{self.audio_bitrate_kbps}k/ac{self.audio_channels}"
        )

    def timeout(self, seconds: Fraction) -> float:
        """클립 한 번 자르기의 시간 한도 — `max(바닥, 배율 × 클립 길이)`초."""
        return _scaled_timeout(self.timeout_floor_seconds, self.timeout_media_ratio, float(seconds))


ANALYSIS_CLIP = AnalysisClipRecipe()
"""지금 쓰는 분석 클립 조리법 — 모든 호출 지점이 이 하나를 읽는다."""


@dataclass(frozen=True)
class AnalysisProxyLimits:
    """원본을 통째로 디코드하는 ffmpeg 한 번의 시간 한도 — `max(바닥, 배율 × 원본 길이)`초(D59).

    분석 프록시 만들기와, 프레임 출처를 통째로 한 번 디코드하는 일(프레임 묶음·장면 컷)이 각자
    한 쌍을 쓴다. 예전에는 둘 다 길이와 무관하게 1800초였다 — 실측 0016(545초 4K HEVC)과
    0018(1359초)의 분석 프록시 만들기가 분석 셋이 함께 도는 동안(D49) 1800초를 넘겨
    `TimeoutExpired`로 원본 전체의 분석이 실패했다. 이제 한도는 원본 길이를 따라 자라고, 넘기면
    분석 프록시는 원본으로 물러난다(D47의 안전망).

    **외부 근거 없는 조정 대상**(`cut_*`처럼): 바닥 1800초는 예전 상한 그대로라 짧은 원본의 동작이
    바뀌지 않는다. 배율 6은 실패가 확인된 지점(0016: 원본 길이의 3.3배를 넘어도 끝나지 않음)의
    약 1.8배다 — 0016은 3271초, 0018은 8154초까지 기다린다. 조리법(`AnalysisProxyRecipe.tag()`)·
    분석 지문·캐시 열쇠 어디에도 들어가지 않는다 — 한도는 기다리는 시간이지 만들어지는 바이트가
    아니다.
    """

    timeout_floor_seconds: float = 1800.0
    timeout_media_ratio: float = 6.0
    frame_batch_timeout_floor_seconds: float = 1800.0
    frame_batch_timeout_media_ratio: float = 6.0

    def proxy_timeout(self, media_seconds: float | None) -> float:
        """분석 프록시 만들기의 한도 — 길이를 모르면 바닥이다."""
        validate_analysis_proxy_limits(self)
        return _scaled_timeout(self.timeout_floor_seconds, self.timeout_media_ratio, media_seconds)

    def frame_batch_timeout(self, media_seconds: float | None) -> float:
        """프레임 출처를 통째로 디코드하는 한 번(프레임 묶음·장면 컷)의 한도."""
        validate_analysis_proxy_limits(self)
        return _scaled_timeout(
            self.frame_batch_timeout_floor_seconds,
            self.frame_batch_timeout_media_ratio,
            media_seconds,
        )


def _scaled_timeout(floor: float, ratio: float, media_seconds: float | None) -> float:
    if media_seconds is None or not math.isfinite(media_seconds) or media_seconds <= 0:
        return floor
    return max(floor, ratio * media_seconds)


ANALYSIS_PROXY_LIMITS = AnalysisProxyLimits()
"""지금 쓰는 디코드 시간 한도 — 분석 프록시·프레임 묶음·장면 컷이 주입이 없으면 이 하나를 읽는다."""


def validate_analysis_proxy_limits(limits: AnalysisProxyLimits) -> None:
    """값역 검사 — 바닥은 유한한 양수, 배율은 유한한 0 이상. NaN은 비교를 무력화하므로 먼저 본다."""
    problems: list[str] = []
    for name, value, minimum_exclusive in (
        ("프록시 한도 바닥", limits.timeout_floor_seconds, True),
        ("프록시 한도 배율", limits.timeout_media_ratio, False),
        ("프레임 묶음 한도 바닥", limits.frame_batch_timeout_floor_seconds, True),
        ("프레임 묶음 한도 배율", limits.frame_batch_timeout_media_ratio, False),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{name}은 실수여야 한다: {value!r}")
        elif not math.isfinite(float(value)):
            problems.append(f"{name}이 유한하지 않다: {value!r}")
        elif value < 0 or (minimum_exclusive and value == 0):
            problems.append(f"{name}이 범위 밖이다: {value!r}")
    if problems:
        raise ValueError("디코드 시간 한도가 계약을 어긴다: " + "; ".join(problems))


@dataclass(frozen=True)
class ReviewMediaSettings:
    """작업대 검토 보조 미디어(원본 전체 미리보기·썸네일)의 만들기 설정(D62).

    **외부 근거 없는 조정 대상**(`cut_*`처럼). 미리보기는 검토 보조일 뿐이다 — Final Cut 보내기는
    원본을 쓴다. 4K를 그대로 `medium`·crf 20으로 다시 인코딩하던 것이 35초 클립에 부하 아래 약
    5분, 9분 0016에 6.8GB였다. 그래서 긴 변 1920(키우지 않는다)·`veryfast`·crf 23으로 만든다.

    한도는 `AnalysisProxyLimits`와 같은 꼴 `max(바닥, 배율 × 원본 길이)`이다 — 0018(1359초)의
    미리보기가 고정 1800초에 걸려 멈췄다. 바닥 1800초는 예전 상한 그대로, 배율 6은 분석 프록시와
    같다. `process_niceness`는 미리보기·썸네일 ffmpeg의 OS 우선순위(nice)다 — 분석 프록시와
    CPU를 다투지 않게 한다(부하 80–100/10코어 실측). `thumb_seek_margin_seconds`는 썸네일의
    빠른 찾기가 목표 프레임보다 얼마나 앞에서 디코드를 시작하는가다.

    `preview_long_edge_4k`는 4K 원본(화면 긴 변 ≥ `FOLDER_WORKSPACE.proxy_min_long_edge`)의 미리보기
    긴 변이다(D100 §2) — 폴더 작업대의 재생기는 4K를 960×540(소리 있음)으로 본다. 브라우저가 그대로
    재생하는 4K H.264도 복사하지 않고 줄인다. 외부 근거 없는 조정 대상이다(25% 분석 프록시와 같은
    크기).

    어느 값도 미리보기·썸네일 캐시 열쇠·조리법 판(`VISUAL_PREVIEW_RECIPE`)·분석 지문에 들어가지
    않는다 — 이미 만든 미리보기는 그대로 쓰고, 새로 만든 것만 기록에 `encoding`을 남긴다.
    """

    preview_long_edge: int = 1920
    preview_long_edge_4k: int = 960
    preview_x264_preset: str = "veryfast"
    preview_x264_crf: int = 23
    preview_timeout_floor_seconds: float = 1800.0
    preview_timeout_media_ratio: float = 6.0
    process_niceness: int = 10
    thumb_seek_margin_seconds: float = 1.0

    def preview_timeout(self, media_seconds: float | None) -> float:
        """원본 전체 미리보기 한 번의 한도 — 길이를 모르면 바닥이다."""
        validate_review_media_settings(self)
        return _scaled_timeout(
            self.preview_timeout_floor_seconds, self.preview_timeout_media_ratio, media_seconds
        )


REVIEW_MEDIA = ReviewMediaSettings()
"""지금 쓰는 검토 보조 미디어 설정 — 미리보기·썸네일 만들기가 이 하나를 읽는다."""


def validate_review_media_settings(settings: ReviewMediaSettings) -> None:
    """값역 검사 — 긴 변은 2 이상 짝수, crf 0–51, nice 0–20, 한도·여유는 유한한 양수/0 이상."""
    problems: list[str] = []
    for edge in (settings.preview_long_edge, settings.preview_long_edge_4k):
        if isinstance(edge, bool) or not isinstance(edge, int) or edge < 2 or edge % 2:
            problems.append(f"미리보기 긴 변은 2 이상 짝수 정수여야 한다: {edge!r}")
    crf = settings.preview_x264_crf
    if isinstance(crf, bool) or not isinstance(crf, int) or not 0 <= crf <= 51:
        problems.append(f"미리보기 crf는 0–51 정수여야 한다: {crf!r}")
    nice = settings.process_niceness
    if isinstance(nice, bool) or not isinstance(nice, int) or not 0 <= nice <= 20:
        problems.append(f"nice 값은 0–20 정수여야 한다: {nice!r}")
    if not settings.preview_x264_preset or not settings.preview_x264_preset.isalpha():
        problems.append(f"x264 preset 이름이 아니다: {settings.preview_x264_preset!r}")
    for name, value, minimum_exclusive in (
        ("미리보기 한도 바닥", settings.preview_timeout_floor_seconds, True),
        ("미리보기 한도 배율", settings.preview_timeout_media_ratio, False),
        ("썸네일 찾기 여유", settings.thumb_seek_margin_seconds, False),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{name}은 실수여야 한다: {value!r}")
        elif not math.isfinite(float(value)) or value < 0 or (minimum_exclusive and value == 0):
            problems.append(f"{name}이 범위 밖이다: {value!r}")
    if problems:
        raise ValueError("검토 보조 미디어 설정이 계약을 어긴다: " + "; ".join(problems))


PAIR_VIDEO_SAMPLE_SIZE = 1024
"""대응 프레임 표본 한 장의 바이트 수 — `correspondence_samples`의 32×32 루마(Y) 평면 한 장."""

PAIR_VIDEO_MAX_ERROR = 10
"""원본·프록시 대응 표본의 평균 절대 차 상한(0–255 루마). 넘으면 같은 프레임이 아니다.

실측 DJI 4K: 같은 프레임 ≤ 1.3, 한 프레임 어긋남 0.6–16. 이 상한은 '다른 그림'을 막고,
한 프레임 이동은 `_shift_rejection`이 잡는다.
"""

PAIR_SHIFT_MARGIN = 0.1
"""이웃 프레임이 제자리보다 이만큼 더 가까우면 시간이 밀린 짝으로 본다."""

PAIR_AUDIO_MIN_CORRELATION = 0.98
"""원본·프록시 대응 소리 표본의 최소 상관 — 못 미치면 같은 소리가 아니다."""

PAIR_AUDIO_TAIL_SECONDS = Fraction(1, 4)
"""소리 표본을 뽑는 마지막 지점이 끝에서 떨어지는 거리 — 표본이 파일 밖으로 나가지 않게."""

PCM_FULL_SCALE = 32768
"""16비트 PCM의 풀스케일 — dBFS를 표본값으로 바꾸는 기준(분석 PCM은 s16le)."""


@dataclass(frozen=True)
class RawAutoCleanupSettings:
    """자동 정리의 내부 무음 판정 값 — `cut_*`·`repair_*`처럼 외부 근거 없는 조정 대상(D45).

    `silence_peak_dbfs`는 표본값으로 `int(32768 * 10 ** (dbfs / 20))`다 — 기본 -50 dBFS는
    103(≈ 103.6의 내림)이라 예전 리터럴과 같은 판정이다. 이 설정은 분석 입력 지문에
    들어가지 않는다: 이미 저장된 `auto_decisions`는 그대로 쓰이고, 값은 새로 계산하는
    판정과 옛 기록의 재계산에만 닿는다.
    """

    silence_seconds: float = 2.0
    speech_handle_seconds: float = 0.3
    silence_peak_dbfs: float = -50.0

    def peak_sample_limit(self) -> int:
        return int(PCM_FULL_SCALE * 10 ** (self.silence_peak_dbfs / 20))


def validate_auto_cleanup_settings(settings: RawAutoCleanupSettings) -> None:
    """값역 검사 — NaN은 비교를 무력화하므로 유한성부터 본다."""
    problems: list[str] = []
    for name, value in (
        ("공백 기준", settings.silence_seconds),
        ("발화 여유", settings.speech_handle_seconds),
        ("무음 피크", settings.silence_peak_dbfs),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{name}은 실수여야 한다: {value!r}")
        elif not math.isfinite(float(value)):
            problems.append(f"{name}이 유한하지 않다: {value!r}")
    if not problems:
        if settings.silence_seconds <= 0:
            problems.append(f"공백 기준은 양수여야 한다: {settings.silence_seconds!r}")
        if settings.speech_handle_seconds < 0:
            problems.append(f"발화 여유는 0 이상이어야 한다: {settings.speech_handle_seconds!r}")
        if 2 * settings.speech_handle_seconds >= settings.silence_seconds:
            problems.append("발화 여유 양쪽이 공백 기준을 다 먹는다 — 지울 구간이 남지 않는다")
        if settings.silence_peak_dbfs > 0:
            problems.append(f"무음 피크는 0 dBFS 이하여야 한다: {settings.silence_peak_dbfs!r}")
    if problems:
        raise ValueError("자동 정리 설정이 계약을 어긴다: " + "; ".join(problems))


@dataclass(frozen=True)
class RawTierSettings:
    """3단 등급의 조정값 — 외부 근거 없는 조정 대상(D51·D52). 분석 지문에 넣지 않는다.

    `RawUnderstandingSettings`·`RawWorkflowConfig`에 두면 Luna 요청 sha·리비전 지문·승인
    digest가 바뀌어 저장된 판정 183창을 다시 사게 된다. 등급은 저장된 분석 문서(AI 판정 +
    생성 자막) 위의 결정적 재계산이라 읽을 때마다 다시 계산하고 어디에도 보관하지 않는다 —
    이 값은 검토 조각의 `등급판` 표지(`tier_version`)로만 남는다.

    로컬 측정 값(공백 RMS·검은 화면 루마·흐림)은 사용자 지시로 지웠다(D52) — 측정 계층이
    없어졌으므로 기준도 없다. 앞의 넷은 D51 그대로다. 값이나 `tiering.py`의 규칙을 바꾸면
    `tier_version`을 올린다 — 옮기기는 이 표지로만 낡은 검토 머리를 안다(20a = v3 ·
    20b-final = v4, D60 · W-25 a = v5, D65).

    추천 기준 강화(D60, 외부 근거 없는 조정 대상 — 바꾸면 D60에 근거를 남긴다):

    - `same_text_run_min`: 접은 글자가 같은 발화가 이만큼 잇따르면 그 줄들을 밀도에서 뺀다(같은 말
      되풀이 — whisper 메아리). 19e 「확인 필요」와 같은 값·같은 접기지만 따로 둔다(표시 기준을
      바꿔도 등급이 움직이지 않게).
    - `slow_filler_chars_per_second`·`slow_filler_min_seconds`: 이 길이 이상이면서 글자(NFC 뒤 문자·
      숫자 범주 낱자)가 초당 이보다 적은 발화는 느린 추임새로 보고 밀도에서 뺀다. 0이면 끈다.
    - `recommended_min_seconds`: 이보다 짧은 녹색 구간·검토 조각은 추천하지 않는다(보통까지). 0이면
      끈다.

    자막이 확인된 영상(D65, 외부 근거 없는 조정 대상 — 바꾸면 D65에 근거를 남긴다):

    - `subtitle_verified_suspect_share`: 지금 자막 리비전의 시각 확인이 초록인 영상에서
      「확인 필요」(D59 19e) 표시가 붙은 원본 자막 줄의 몫이 이 값 **이하**면 「자막 확인됨」이다 —
      그 영상의 `usable` × 발화 많음 칸이 추천이 된다. 0이면 의심 줄이 하나도 없어야 하고, 1이면
      초록 판정만 본다.

    손셀피 브이로그의 추천 넓히기(W-29c, D79, `raw-tier/v6` — 사용자 결정 「초록을 충분히 늘려줘」,
    외부 근거 없는 조정 대상):

    - `usable_speech_green`: AI가 `usable`이라 한 화면 × 발화 많음 → 추천(「자막 확인됨」이 없어도).
    - `scenery_green`: AI가 `recommended`라 한 화면 × 자막 공백(풍경) → 추천(D51을 바꾼다).
    - `good_sparse_green`: AI가 `recommended`라 한 화면 × 발화 적음 → 추천.
    - `visual_issue_caps`: 녹색을 보통으로 내리는 화면 문제(D60 20b-final)를 이 종류만으로 좁힌다 —
      손에 든 셀피에 늘 붙는 가림·흔들림·흐림은 빼고 어두움·과노출만 남긴다(사용자 결정).
    - `segment_green_share`: 검토 조각은 빨강이 없고 녹색 구간이 조각 길이의 이 몫 **이상**이면
      추천이다(예전엔 전부 녹색이어야 했다). 3초 상한은 그대로 걸린다. 1이면 예전 규칙이다.

    라이브러리 전체 맥락 평가(W-39, D99, `raw-tier/v7` — 사용자 결정 「등급에 반영 · 라이브러리
    전체」, 외부 근거 없는 조정 대상):

    - `context_lower_verdicts`·`context_lower_min_confidence`: 맥락 평가가 이 판정을 이 확신
      이상으로 준 녹색 구간은 보통으로 내린다(「추천 제한: 맥락: …」).
    - `context_raise_verdicts`·`context_raise_min_confidence`·`context_raise_rules`: 이 판정을
      이 확신 이상으로 준 노랑 구간은, 규칙 칸이 이 목록에 있고 화면이 `recommended`·`usable`이며
      판정 보류·추천 제한이 없을 때만 추천으로 올린다(좁게 — 올린 추천은 D93으로 켜져 나간다).
    - 평가가 없거나 낡았으면 v6과 같은 등급이다.
    """

    tier_version: str = "raw-tier/v7"
    silence_min_seconds: float = 5.0
    speech_handle_seconds: float = 0.3
    min_span_seconds: float = 1.0
    speech_density_green: float = 0.35
    same_text_run_min: int = 3
    slow_filler_chars_per_second: float = 1.5
    slow_filler_min_seconds: float = 2.0
    recommended_min_seconds: float = 3.0
    subtitle_verified_suspect_share: float = 0.10
    usable_speech_green: bool = True
    scenery_green: bool = True
    good_sparse_green: bool = True
    visual_issue_caps: tuple[str, ...] = ("dark", "overexposed")
    segment_green_share: float = 0.5
    context_lower_verdicts: tuple[str, ...] = ("redundant", "off_topic")
    context_lower_min_confidence: str = "medium"
    context_raise_verdicts: tuple[str, ...] = ("core",)
    context_raise_min_confidence: str = "high"
    context_raise_rules: tuple[str, ...] = ("speech-sparse", "scenery-no-speech")


TIER_SETTINGS = RawTierSettings()
"""지금 쓰는 등급 조정값 — 등급 계산은 주입이 없으면 이 하나를 읽는다."""


def validate_tier_settings(settings: RawTierSettings) -> None:
    """값역 검사 — 어긋나면 ValueError로 전부 보고한다. NaN은 비교를 무력화하므로 먼저 본다."""
    problems = tier_settings_problems(settings)
    if problems:
        raise ValueError("등급 설정이 계약을 어긴다: " + "; ".join(problems))


def tier_settings_problems(settings: RawTierSettings) -> list[str]:
    numbers = {
        "공백 길이": settings.silence_min_seconds,
        "발화 여유": settings.speech_handle_seconds,
        "최소 구간": settings.min_span_seconds,
        "발화 밀도": settings.speech_density_green,
        "느린 추임새 속도": settings.slow_filler_chars_per_second,
        "느린 추임새 길이": settings.slow_filler_min_seconds,
        "추천 최소 길이": settings.recommended_min_seconds,
        "자막 확인 의심 줄 몫": settings.subtitle_verified_suspect_share,
    }
    problems = [
        f"{name}은 유한한 실수여야 한다: {value!r}"
        for name, value in numbers.items()
        if isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ]
    run = settings.same_text_run_min
    if isinstance(run, bool) or not isinstance(run, int):
        problems.append(f"같은 말 런 길이는 정수여야 한다: {run!r}")
    if not isinstance(settings.tier_version, str) or not settings.tier_version.strip():
        problems.append(f"등급판 이름이 비었다: {settings.tier_version!r}")
    problems += _tier_v6_problems(settings)
    problems += _tier_context_problems(settings)
    return problems or _tier_range_problems(settings)


@dataclass(frozen=True)
class ContextReviewSettings:
    """라이브러리 맥락 평가(W-39, D99)의 입력 예산과 안내문 판 — 외부 근거 없는 조정 대상.

    원본마다 자막 줄·창 요약을 이 수까지만 싣고, 줄 글자·요약 글자를 이 길이에서 자른다 — 줄이고
    자른 수는 입력 기록이 센다(조용히 줄이지 않는다). 모두 평가 열쇠에 실린다(바꾸면 지난 평가는
    낡는다). 판정(`context_*`)은 등급 설정(`RawTierSettings`)이 정한다."""

    prompt_revision: str = "context-review/v1"
    max_lines_per_source: int = 400
    max_line_chars: int = 80
    max_windows_per_source: int = 80
    max_summary_chars: int = 200
    max_output_tokens: int = 32000
    """응답 한도(생각 포함) — 구간 수백 개의 판정이 들어가게 넉넉히(Gemini 동기 상한 65536 안)."""
    quote_chars_per_token: int = 1
    """견적의 입력 토큰 추정 — 글자 하나 = 토큰 하나(한국어 JSON에 보수적인 쪽, 실제는 더 적다)."""

    def __post_init__(self) -> None:
        for name in (
            "max_lines_per_source",
            "max_line_chars",
            "max_windows_per_source",
            "max_summary_chars",
            "max_output_tokens",
            "quote_chars_per_token",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}는 1 이상의 정수여야 한다: {value!r}")
        if not self.prompt_revision.strip():
            raise ValueError("맥락 평가 안내문 판이 비었다")


CONTEXT_REVIEW = ContextReviewSettings()
"""지금 쓰는 맥락 평가 예산 — `context_review.build_input`의 기본값이다."""

CONTEXT_VERDICTS = ("core", "supporting", "redundant", "off_topic")
"""라이브러리 맥락 평가(W-39, D99)의 판정 — 핵심 · 보조 · 중복 · 맥락 밖."""
CONTEXT_CONFIDENCES = ("low", "medium", "high")
"""맥락 판정의 확신 — 낮은 것부터(차례가 곧 크기다)."""


def _tier_context_problems(settings: RawTierSettings) -> list[str]:
    problems: list[str] = []
    for name, verdicts in (
        ("내리는 맥락 판정", settings.context_lower_verdicts),
        ("올리는 맥락 판정", settings.context_raise_verdicts),
    ):
        if not isinstance(verdicts, tuple) or any(v not in CONTEXT_VERDICTS for v in verdicts):
            problems.append(f"{name}은 {CONTEXT_VERDICTS} 가운데의 튜플이어야 한다: {verdicts!r}")
    if set(settings.context_lower_verdicts) & set(settings.context_raise_verdicts):
        problems.append("같은 맥락 판정이 내리기와 올리기에 함께 있다")
    for name, level in (
        ("내리기 확신", settings.context_lower_min_confidence),
        ("올리기 확신", settings.context_raise_min_confidence),
    ):
        if level not in CONTEXT_CONFIDENCES:
            problems.append(f"{name}은 {CONTEXT_CONFIDENCES} 가운데 하나여야 한다: {level!r}")
    rules = settings.context_raise_rules
    if not isinstance(rules, tuple) or not all(isinstance(rule, str) for rule in rules):
        problems.append(f"올리기 규칙 칸은 글자의 튜플이어야 한다: {rules!r}")
    return problems


VISUAL_ISSUE_KINDS = ("blur", "shake", "dark", "overexposed", "obstructed")
"""AI 판정의 화면 문제 종류(D58 18b) — `tiering.VISUAL_ISSUE_LABELS`의 열쇠와 같다(시험이
잠근다)."""


def _tier_v6_problems(settings: RawTierSettings) -> list[str]:
    problems = [
        f"{name}은 참·거짓이어야 한다: {value!r}"
        for name, value in (
            ("usable 발화 추천", settings.usable_speech_green),
            ("풍경 추천", settings.scenery_green),
            ("발화 적은 좋은 화면 추천", settings.good_sparse_green),
        )
        if not isinstance(value, bool)
    ]
    caps = settings.visual_issue_caps
    if not isinstance(caps, tuple) or not all(isinstance(kind, str) for kind in caps):
        return [*problems, f"추천을 막는 화면 문제는 글자의 튜플이어야 한다: {caps!r}"]
    unknown = [kind for kind in caps if kind not in VISUAL_ISSUE_KINDS]
    if unknown:
        problems.append(f"추천을 막는 화면 문제 종류를 모른다: {unknown!r}")
    share = settings.segment_green_share
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 1:
        problems.append(f"조각 녹색 몫은 (0, 1]이어야 한다: {share!r}")
    return problems


def _tier_range_problems(settings: RawTierSettings) -> list[str]:
    problems: list[str] = []
    if not 0 < settings.min_span_seconds <= settings.silence_min_seconds:
        problems.append("0 < 최소 구간 ≤ 공백 길이여야 한다")
    if not 0 <= 2 * settings.speech_handle_seconds < settings.silence_min_seconds:
        problems.append("발화 여유 양쪽이 공백 길이를 다 먹는다 — 무음 구간이 남지 않는다")
    if not 0 < settings.speech_density_green <= 1:
        problems.append(f"발화 밀도는 (0, 1]이어야 한다: {settings.speech_density_green!r}")
    if settings.same_text_run_min < 2:
        problems.append(f"같은 말 런은 2줄 이상이어야 한다: {settings.same_text_run_min!r}")
    if settings.slow_filler_chars_per_second < 0 or settings.slow_filler_min_seconds <= 0:
        problems.append("느린 추임새 속도는 0 이상, 길이는 양수여야 한다")
    if settings.recommended_min_seconds < 0:
        problems.append(f"추천 최소 길이는 0 이상이어야 한다: {settings.recommended_min_seconds!r}")
    if not 0 <= settings.subtitle_verified_suspect_share <= 1:
        share = settings.subtitle_verified_suspect_share
        problems.append(f"자막 확인 의심 줄 몫은 [0, 1]이어야 한다: {share!r}")
    return problems


EDITABLE_TIER_KEYS: tuple[str, ...] = (
    "recommended_min_seconds",
    "speech_density_green",
    "silence_min_seconds",
    "usable_speech_green",
    "scenery_green",
    "good_sparse_green",
    "segment_green_share",
    "visual_issue_caps",
)
"""작업대가 보이는 추천 기준(W-42, D100 §3) — 최소 길이 · 밀도 · 공백 길이 · 녹색 칸 셋 · 녹색 몫 ·
화면 문제. 나머지 칸도 작업공간 덮기로 바꿀 수 있지만 화면에는 없다."""

CONTEXT_RAISE_RULE_CHOICES = ("speech-sparse", "scenery-no-speech")
"""맥락 평가가 올릴 수 있는 노랑 칸(D99) — 덮기는 이 안에서 좁히기만 한다(넓히지 않는다)."""

_TIER_OVERRIDE_FIXED = frozenset({"tier_version"})
"""덮을 수 없는 칸 — 등급판은 코드의 규칙이다(작업공간 기준은 digest로 따로 낡음 열쇠에 실린다)."""

_TIER_TUPLE_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "visual_issue_caps": VISUAL_ISSUE_KINDS,
    "context_lower_verdicts": CONTEXT_VERDICTS,
    "context_raise_verdicts": CONTEXT_VERDICTS,
    "context_raise_rules": CONTEXT_RAISE_RULE_CHOICES,
}


def tier_settings_from_overrides(overrides: Mapping[str, object]) -> RawTierSettings:
    """작업공간 덮기(JSON 값) → 등급 설정(W-42, D100 §3). 기본값 위에 얹고 검사한다.

    모르는 칸·등급판·틀린 꼴(참·거짓 자리의 수, 실수 자리의 글 등)·목록 밖 낱말은
    ValueError다. 실수 칸의 정수는 실수로 바꾼다(같은 값이 같은 digest를 낸다). 값역은
    `validate_tier_settings`가 본다.
    """
    if not isinstance(overrides, Mapping):
        raise ValueError(f"추천 기준 덮기는 칸 → 값의 객체여야 한다: {overrides!r}")
    names = {field.name for field in fields(RawTierSettings)}
    problems: list[str] = []
    values: dict[str, Any] = {}
    for key, value in overrides.items():
        if key in _TIER_OVERRIDE_FIXED or key not in names:
            problems.append(f"덮을 수 없는 추천 기준 칸이다: {key!r}")
            continue
        coerced, problem = _tier_override_value(key, value)
        if problem is not None:
            problems.append(problem)
        else:
            values[key] = coerced
    if problems:
        raise ValueError("추천 기준 덮기가 계약을 어긴다: " + "; ".join(problems))
    settings = replace(TIER_SETTINGS, **values)
    validate_tier_settings(settings)
    return settings


def _tier_override_value(key: str, value: object) -> tuple[object, str | None]:
    """칸 하나의 JSON 값 → 설정 값 — 꼴은 기본값의 꼴을 따른다. (값, 문제)."""
    default = getattr(TIER_SETTINGS, key)
    wrong = f"{key}의 값 꼴이 틀렸다: {value!r}"
    if isinstance(default, bool):
        return (value, None) if isinstance(value, bool) else (None, wrong)
    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), None
        return None, wrong
    if isinstance(default, int):
        whole = isinstance(value, int) and not isinstance(value, bool)
        return (value, None) if whole else (None, wrong)
    if isinstance(default, str):
        return (value, None) if isinstance(value, str) else (None, wrong)
    return _tier_override_words(key, value)


def _tier_override_words(key: str, value: object) -> tuple[object, str | None]:
    """낱말 목록 칸 — 알려진 낱말만, 겹치지 않게."""
    if not isinstance(value, (list, tuple)) or not all(isinstance(word, str) for word in value):
        return None, f"{key}는 낱말 목록이어야 한다: {value!r}"
    if len(set(value)) != len(value):
        return None, f"{key}에 같은 낱말이 두 번 있다: {value!r}"
    vocabulary = _TIER_TUPLE_VOCABULARIES[key]
    unknown = [word for word in value if word not in vocabulary]
    if unknown:
        return None, f"{key}의 낱말을 모른다 {unknown!r} — {vocabulary} 가운데서 고른다"
    return tuple(value), None


def tier_settings_overrides(settings: RawTierSettings) -> dict[str, object]:
    """등급 설정 → 기본값과 다른 칸만의 JSON 덮기(`tier_settings_from_overrides`의 거꾸로)."""
    found: dict[str, object] = {}
    for field in fields(RawTierSettings):
        if field.name in _TIER_OVERRIDE_FIXED:
            continue
        value = getattr(settings, field.name)
        if value != getattr(TIER_SETTINGS, field.name):
            found[field.name] = list(value) if isinstance(value, tuple) else value
    return found


def tier_settings_values(settings: RawTierSettings) -> dict[str, object]:
    """등급 설정의 모든 칸(등급판 빼고)을 JSON 값으로 — 화면이 지금 값과 기본값을 함께 그린다."""
    return {
        field.name: list(value) if isinstance(value, tuple) else value
        for field in fields(RawTierSettings)
        if field.name not in _TIER_OVERRIDE_FIXED
        for value in (getattr(settings, field.name),)
    }


@dataclass(frozen=True)
class RawAnalysisConcurrency:
    """라이브러리 일괄 분석이 한 번에 돌리는 영상 수 — 외부 근거 없는 조정 대상(D49).

    1단계(분석 프록시·전사·프레임)는 CPU를 쓰고 실측 영상당 149–298초, 2단계(Luna)는 네트워크를
    기다리며 50–173초다. 셋이면 한 영상의 Luna 대기 동안 다른 영상의 1단계가 돌아 두 단계가
    겹치고, 4K 디코드 메모리와 분당 요청 한도에 여유가 남는다. 1이면 예전 직렬과 바이트까지 같다.
    `RawWorkflowConfig`에 두지 않는다 — 거기 들어가면 저장된 견적의 workflow·digest가 바뀐다.
    동시에 도는 수만 바꾸므로 영상마다의 승인·호출 수(비용)는 이 값과 무관하다.
    """

    parallel_analysis_limit: int = 3


ANALYSIS_CONCURRENCY = RawAnalysisConcurrency()
"""지금 쓰는 동시 분석 한도 — 작업대는 주입이 없으면 이 하나를 읽는다."""


def validate_analysis_concurrency(settings: RawAnalysisConcurrency) -> None:
    """값역 검사 — bool은 int 서브클래스지만 개수가 될 수 없다."""
    value = settings.parallel_analysis_limit
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"동시 분석 한도는 정수여야 한다: {value!r}")
    if value < 1:
        raise ValueError(f"동시 분석 한도는 1 이상이어야 한다: {value!r}")


@dataclass(frozen=True)
class WorkspaceDbSettings:
    """작업공간 DB(WAL)의 옆 파일 크기 상한과 체크포인트 대기 — 외부 근거 없는 조정 대상(D63 H).

    `journal_size_limit_bytes`는 연결마다 거는 `PRAGMA journal_size_limit`이다. SQLite 기본값
    -1(무제한)에서는 `-wal`이 가장 컸던 때의 크기로 남는다 — 실제 작업공간의 `-wal`이 3.4 MB로
    줄지 않았다(체크포인트는 내용을 옮길 뿐 파일을 줄이지 않는다). 상한을 걸면 체크포인트가 WAL을
    처음으로 되돌릴 때마다 파일을 이 크기로 자른다. 1 MiB(4 KiB 쪽 256장)는 관찰한 3.4 MB의 3분의
    1 아래이면서, 작업 상태·진행 한 번 쓰기(몇 쪽)를 수백 번 담아 작은 쓰기마다 파일을 다시
    늘리지 않는다. 쓰기가 몰리는 동안에는 자동 체크포인트(1000쪽)까지 잠깐 넘을 수 있다.

    `checkpoint_busy_timeout_ms`는 닻을 닫으며 거는 `wal_checkpoint(TRUNCATE)`의 대기다. 연결의
    `busy_timeout`(10초)을 그대로 쓰면 종료(atexit)가 닻 수 × 10초까지 막힐 수 있다. 0.5초는 작업
    상태 한 번 커밋(밀리초)을 기다리기에 넉넉하고, 닻 상한 8개에도 종료를 4초 안에 묶는다. 못 끝내면
    경고만 남기고 닫는다 — 내용은 `-wal`에 그대로 있고 다음 체크포인트가 옮긴다.

    어느 값도 분석 지문·캐시 열쇠·승인에 들어가지 않는다 — DB 파일 관리일 뿐 결과를 바꾸지 않는다.
    """

    journal_size_limit_bytes: int = 1_048_576
    checkpoint_busy_timeout_ms: int = 500


WORKSPACE_DB = WorkspaceDbSettings()
"""지금 쓰는 작업공간 DB 설정 — 모든 작업공간 연결과 닻이 이 하나를 읽는다."""


def validate_workspace_db_settings(settings: WorkspaceDbSettings) -> None:
    """값역 검사 — PRAGMA 문장에 그대로 실리므로 bool이 아닌 정수만, 상한은 0 이상·대기는 양수."""
    problems: list[str] = []
    for name, value, minimum in (
        ("journal_size_limit_bytes", settings.journal_size_limit_bytes, 0),
        ("checkpoint_busy_timeout_ms", settings.checkpoint_busy_timeout_ms, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{name}은 정수여야 한다: {value!r}")
        elif value < minimum:
            problems.append(f"{name}이 범위 밖이다: {value!r}")
    if problems:
        raise ValueError("작업공간 DB 설정이 계약을 어긴다: " + "; ".join(problems))
