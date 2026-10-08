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
from dataclasses import dataclass
from fractions import Fraction

UNDERSTANDING_SCHEMA_VERSION = "raw-understanding/v1"
"""첫 단계 기록(analysis_transcript.json)의 namespace(계획 §13.1·§15.1)."""

DEFAULT_PROMPT_REVISION = "raw-suitability-v2"
"""Luna 안내문의 리비전 — 캐시 지문과 승인에 함께 실린다(§14.2)."""

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
    """3단 등급의 조정값 — 외부 근거 없는 조정 대상(D51). 분석 지문에 넣지 않는다.

    `RawUnderstandingSettings`·`RawWorkflowConfig`에 두면 Luna 요청 sha·리비전 지문·승인
    digest가 바뀌어 저장된 판정 183창을 다시 사게 된다. 등급은 저장된 증거 위의 결정적
    재계산이므로 이 값은 등급 보고서(`tier_report.json`)에만 실린다.

    `black_luma_max`·`blur_min`은 실측 보정값이다(D51): 저장 프레임 415장의 YAVG 최소 36.5,
    blurdetect 3.62–6.47이 전부 Luna 추천·사용 가능 창이었다. 흐림은 결함 표본이 없어
    가를 수 없으므로 `blur_min = 0`(끔)이다.
    """

    tier_version: str = "raw-tier/v1"
    silence_min_seconds: float = 5.0
    silence_rms_dbfs: float = -35.0
    speech_handle_seconds: float = 0.3
    min_span_seconds: float = 1.0
    speech_density_green: float = 0.35
    black_luma_max: float = 20.0
    black_frame_fraction: float = 0.75
    blur_min: float = 0.0
    blur_frame_fraction: float = 0.75


TIER_SETTINGS = RawTierSettings()
"""지금 쓰는 등급 조정값 — 등급 계산은 주입이 없으면 이 하나를 읽는다."""

LUMA_FULL_SCALE = 255.0
"""8비트 루마의 최댓값 — 검은 화면 기준(YAVG)의 값역 상한."""


def validate_tier_settings(settings: RawTierSettings) -> None:
    """값역 검사 — 어긋나면 ValueError로 전부 보고한다. NaN은 비교를 무력화하므로 먼저 본다."""
    problems = tier_settings_problems(settings)
    if problems:
        raise ValueError("등급 설정이 계약을 어긴다: " + "; ".join(problems))


def tier_settings_problems(settings: RawTierSettings) -> list[str]:
    numbers = {
        "무음 길이": settings.silence_min_seconds,
        "무음 RMS": settings.silence_rms_dbfs,
        "발화 여유": settings.speech_handle_seconds,
        "최소 구간": settings.min_span_seconds,
        "발화 밀도": settings.speech_density_green,
        "검은 화면 루마": settings.black_luma_max,
        "검은 화면 비율": settings.black_frame_fraction,
        "흐림 기준": settings.blur_min,
        "흐림 비율": settings.blur_frame_fraction,
    }
    problems = [
        f"{name}은 유한한 실수여야 한다: {value!r}"
        for name, value in numbers.items()
        if isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ]
    if not isinstance(settings.tier_version, str) or not settings.tier_version.strip():
        problems.append(f"등급판 이름이 비었다: {settings.tier_version!r}")
    return problems or _tier_range_problems(settings)


def _tier_range_problems(settings: RawTierSettings) -> list[str]:
    problems: list[str] = []
    if not 0 < settings.min_span_seconds <= settings.silence_min_seconds:
        problems.append("0 < 최소 구간 ≤ 무음 길이여야 한다")
    if not 0 <= 2 * settings.speech_handle_seconds < settings.silence_min_seconds:
        problems.append("발화 여유 양쪽이 무음 길이를 다 먹는다 — 무음 구간이 남지 않는다")
    if not -100 <= settings.silence_rms_dbfs <= 0:
        problems.append(f"무음 RMS는 -100–0 dBFS여야 한다: {settings.silence_rms_dbfs!r}")
    if not 0 < settings.speech_density_green <= 1:
        problems.append(f"발화 밀도는 (0, 1]이어야 한다: {settings.speech_density_green!r}")
    for name, value in (
        ("검은 화면 비율", settings.black_frame_fraction),
        ("흐림 비율", settings.blur_frame_fraction),
    ):
        if not 0 < value <= 1:
            problems.append(f"{name}은 (0, 1]이어야 한다: {value!r}")
    if not 0 <= settings.black_luma_max <= LUMA_FULL_SCALE:
        problems.append(f"검은 화면 루마는 0–255여야 한다: {settings.black_luma_max!r}")
    if settings.blur_min < 0:
        problems.append(f"흐림 기준은 0(끔) 이상이어야 한다: {settings.blur_min!r}")
    return problems


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
