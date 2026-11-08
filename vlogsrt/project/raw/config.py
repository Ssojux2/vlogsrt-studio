"""원본 영상 브라우저 v2의 동결 설정 — 모델 역할·기능 등록표·수명주기·요금.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11·§15.1의 설정 계약을 코드보다
먼저 세운다(A0). A1 이후의 서비스·어댑터는 이 값을 **값으로만** 읽는다 — 여기서
네트워크 호출·ffmpeg 실행·파일 쓰기를 하지 않는다.

모델 리터럴은 이 파일에만 있을 수 있다(`scripts/check_invariants.py` §7 —
gpt-6-luna·claude-*는 정확히 이 경로, whisper-1·gpt-transcribe는 config 경로 셋 중 하나).
2026-10-02 공식 문서 확인값이며 계정 접근·유료 호출은 시험하지 않았다(§11.9). Claude
의미 분석 프로파일은 공식 문서 확인값이다 — Opus 5.5는 2026-10-09(D54), Sonnet 5.5는
2026-10-11(D58).
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from types import MappingProxyType
from typing import TypeVar, cast
from urllib.parse import urlsplit

_T = TypeVar("_T")

WORKFLOW_SCHEMA_VERSION = "raw-workspace/v1"
"""새 워크플로의 namespace(계획 §13). FCP ProjectRevision을 재사용하지 않는다."""

CAPTION_SCHEMA_VERSION = "raw-caption/v2"
"""최종 자막 기록의 schema. 문구·시간 출처가 갈린 v2 계약이다(계획 §11.5)."""

CAPTION_PROFILE_LATEST_OPENAI_SRT = "latest_openai_srt"
"""기본 프리셋 — 최신 문구 모델 + 검증된 시간 골격 + 앱 조립(계획 §11.0)."""

TIMING_PROVIDER_WHISPER_VERBOSE_JSON = "whisper_verbose_json"
"""시간 제공자 어댑터. whisper-1은 과도기 제공자다(계획 §11.10)."""

TEXT_RESPONSE_FORMAT_JSON = "json"
"""문구 경로의 유일한 확인 응답 형식 — `text`만 오고 시각은 오지 않는다(§11.4)."""

TIMING_RESPONSE_FORMAT_VERBOSE_JSON = "verbose_json"
"""시간 경로의 응답 형식 — 세그먼트 골격의 원천이다(§11.4)."""

TIMESTAMP_GRANULARITIES_SEGMENT = ("segment",)
"""기본은 세그먼트 시간이다. `word`는 세그먼트와 함께일 때만 허용한다(§11.3, D73)."""

STAGE1_TIMESTAMP_GRANULARITIES = ("segment", "word")
"""1단계 내부 전사가 묻는 입자(W-25c, D73) — 단어 시각은 **제안·보고서 재료로만** 남는다.

자막의 시각과 경계는 여전히 `segments[]`에서만 온다(규칙 1). whisper-1은 같은 값에 단어 시각을
함께 준다(지연만 는다). 저장된 작업공간 설정의 칸이 아니다 — 칸을 바꾸면 이미 만든 작업공간은
옛 값을 그대로 읽고, 청크 열쇠가 설정 전체를 해시하므로 이미 산 전사가 모두 낡는다. 그래서
`stage1_timing_view`가 요청에만 얹고 열쇠는 저장된 설정으로 잰다."""

TIMESTAMP_GRANULARITIES_ALLOWED = (TIMESTAMP_GRANULARITIES_SEGMENT, STAGE1_TIMESTAMP_GRANULARITIES)
"""whisper 계약이 받는 입자 조합 — 세그먼트만, 또는 세그먼트와 단어. 단어만은 골격이 없다."""

TIMING_PROVIDER_GEMINI_WORD_INFO = "gemini_transcribe_word_info"
"""시간 제공자 어댑터 — Gemini 3.5 Transcribe의 단어 시각(W-31c, D92)."""
TIMING_RESPONSE_FORMAT_WORD_INFO = "word_info"
"""Gemini 시간 경로의 응답 형식 — 단어 `start_offset`·`end_offset`이 줄 경계의 원천이다(D92)."""
TIMESTAMP_GRANULARITIES_WORD = ("word",)
"""Gemini 엔진의 입자 — 단어 시각만 있고, 줄은 단어 경계에서 묶는다(`word_cues`, D92)."""
TIMING_PROVIDER_CONTRACTS: Mapping[str, tuple[str, tuple[tuple[str, ...], ...]]] = MappingProxyType(
    {
        TIMING_PROVIDER_WHISPER_VERBOSE_JSON: (
            TIMING_RESPONSE_FORMAT_VERBOSE_JSON,
            TIMESTAMP_GRANULARITIES_ALLOWED,
        ),
        TIMING_PROVIDER_GEMINI_WORD_INFO: (
            TIMING_RESPONSE_FORMAT_WORD_INFO,
            (TIMESTAMP_GRANULARITIES_WORD,),
        ),
    }
)
"""시간 제공자 → (응답 형식, 받는 입자 조합) — 이 짝 밖의 계약은 동결 검사가 거부한다."""

CONFIRMED_REGISTRY_REVISION = "openai-caption-2026-10-02-v1"
"""2026-10-02 공식 문서로 확인한 등록표 리비전 — 아래 확정값들의 열쇠다."""

CONFIRMED_TIMING_SHUTDOWN_DATE = date(2027, 2, 26)
"""공지상 whisper-1 종료일(2026-08-26 공지). 종료 시각은 가정하지 않는다(§11.10)."""


@dataclass(frozen=True)
class RawModelRoles:
    """역할별 모델 — 문구·시간·의미 분석은 서로 다른 역할이다(계획 §3.2·§11.0).

    문구 모델은 시각 없는 문자열만, 시간 모델은 세그먼트 골격만, Luna 모델은
    프레임·전사 증거의 의미 해석만 맡는다. 한 모델이 두 역할을 겹치는 순간
    "시각 없는 문구에 가짜 시각"이 생긴다 — 검증이 그 조합을 막는다.

    의미 분석(`luna_model` — 칸 이름은 저장 형식이라 그대로다)의 기본은 Gemini 3.8 Flash다(D87 —
    사용자 지시 2026-10-25 「주로 Flash를 이용해서 처리」, 그 전에는 Claude Sonnet 5.5, D58). 새
    작업공간은 이 값으로 만들어지고, 기존 작업공간은 열 때 한 번 이 값으로 옮긴다
    (`RawWorkspaceService.adopt_default_analysis_model`) — 그 뒤 사람이 고른 모델은 그대로다.
    """

    text_model: str = "gpt-transcribe"
    timing_model: str = "whisper-1"
    luna_model: str = "gemini-3.8-flash"


@dataclass(frozen=True)
class RawModelRegistry:
    """모델 기능·수명주기 등록표 스냅샷(계획 §11.9·§11.10).

    2026-10-02 공식 문서 기준 확인값이다. 종료 공지(2026-08-26)는 whisper-1의
    지원 종료를 예고하고 종료일을 2027-02-26으로 명시한다 — 이 날짜의 정확한
    시각은 가정하지 않고, 배포 전 공지를 다시 확인한다.
    """

    revision: str = "openai-caption-2026-10-02-v1"
    docs_checked_at: date = date(2026, 10, 2)
    account_access_status: str = "unverified"
    integration_test_status: str = "not_run"
    timing_deprecation_notice_date: date = date(2026, 8, 26)
    timing_shutdown_date: date = date(2027, 2, 26)
    block_online_at_shutdown_date: bool = True
    source_urls: tuple[str, ...] = (
        "https://developers.openai.com/api/docs/models/gpt-transcribe",
        "https://developers.openai.com/api/docs/guides/transcription",
        "https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create",
        "https://developers.openai.com/api/docs/deprecations",
        "https://developers.openai.com/api/docs/models/whisper-1",
    )

    def blocks_online_timing(self, on: date) -> bool:
        """§11.10 — 종료 예정일에 도달하면 신규 온라인 Whisper 작업을 보수적으로 차단.

        과거 유효 결과 열기·오프라인 fixture 재생·수동 수정은 막지 않는다. 대체
        타이밍 모델은 별도 검증 전 자동 도입하지 않는다 — 이 검사는 "조용한
        fallback 금지"의 데이터 쪽 반쪽일 뿐이다.
        """
        return self.block_online_at_shutdown_date and on >= self.timing_shutdown_date


@dataclass(frozen=True)
class RawPricingSnapshot:
    """2026-10-02 공식 모델 페이지 기준 요금 스냅샷(계획 §11.8).

    분당 문자열이 아니라 Decimal로 저장한다 — 부동소수점 오차를 비용 산술에
    끌어들이지 않는다. 확인되지 않은 가격은 0원으로 계산하지 않는다(검증 참조).
    """

    checked_at: date = date(2026, 10, 2)
    currency: str = "USD"
    text_per_minute: Decimal = Decimal("0.0045")
    timing_per_minute: Decimal = Decimal("0.006")

    @property
    def both_paths_per_minute(self) -> Decimal:
        """문구·시간 두 경로를 각각 한 번씩 돌리는 기본 전사비(계획 §11.8).

        산술 예시일 뿐 실행 비용 상한이 아니다 — 실집행은 청크·복구·재시도
        예약량을 합산한다.
        """
        return self.text_per_minute + self.timing_per_minute


@dataclass(frozen=True)
class RawCaptionContract:
    """최종 자막 요청 계약 — 문구/시간 응답 형식과 금지 조건(계획 §11.4).

    문구 경로는 JSON `text`만, 시간 경로는 verbose_json 세그먼트만. 기능 등록표가
    허용하지 않는 조합(timestamp_granularities·srt·vtt를 문구 모델에 붙이는 등)은
    호출 전에 차단한다 — A7-1이 이 값을 진짜 요청에 얹는다.
    """

    schema_version: str = CAPTION_SCHEMA_VERSION
    profile: str = CAPTION_PROFILE_LATEST_OPENAI_SRT
    timing_provider: str = TIMING_PROVIDER_WHISPER_VERBOSE_JSON
    languages: tuple[str, ...] = ("ko",)
    keywords: tuple[str, ...] = ()
    text_response_format: str = TEXT_RESPONSE_FORMAT_JSON
    timing_response_format: str = TIMING_RESPONSE_FORMAT_VERBOSE_JSON
    timestamp_granularities: tuple[str, ...] = TIMESTAMP_GRANULARITIES_SEGMENT
    allow_silent_model_fallback: bool = False
    allow_guessed_timestamps: bool = False


@dataclass(frozen=True)
class TimingResponseTolerance:
    """시간 응답 해석의 관용 — 청크 끝을 살짝 넘은 세그먼트를 청크 끝으로 닫는 폭(D55).

    **외부 근거 없음.** 실측(2026-10-09, 원본 18개·청크 38개) whisper-1 넘침은 1.0초
    (181.0/180.0) · 0.068초(39.0/38.932) · 0.06초(180.06/180.0)였다 — 예전엔 그 하나가
    청크 전체를 버려 4/38 청크가 조용히 비었다(규칙 9). 2.0초는 실측 최대에 1.0초 여유다.
    이 폭 밖의 넘침은 여전히 소유하지 않은 시각이라 거부한다. 닫는 것은 시각을 지어내는
    것이 아니라 오디오가 끝난 자리로 자르는 것뿐이다(D55 — 규칙 1·3의 좁은 예외).

    `RawWorkflowConfig`의 칸이 **아니다** — 내부 전사 캐시 열쇠가 그 사전을 통째로
    해시하므로, 칸이 되면 이미 받은 전사가 모두 낡는다(D47의 교훈).
    """

    chunk_end_overrun_seconds: float = 2.0


TIMING_RESPONSE_TOLERANCE = TimingResponseTolerance()


SOURCE_TEXT_PROMPT = "한국어 여행 브이로그. 반말·구어체·추임새를 들린 그대로 적어 주세요."
"""1단계 글자 경로(gpt-transcribe)에만 가는 고정 문맥(D59 19c) — 앞 조각 글자는 싣지 않는다.

whisper에는 가지 않는다(규칙 6·D16 — whisper는 이 글을 직전 자막으로 읽고 이어 쓴다). 두 번째
귀에게는 문체를 알려 반말·추임새를 표준어로 고쳐 쓰지 않게 하는 것이 목적이다. **외부 근거 없는
조정 대상**이다 — 바꾸면 글자 캐시 열쇠가 바뀌어 그 글자를 다시 산다(whisper는 그대로다)."""


@dataclass(frozen=True)
class SourceTextSettings:
    """1단계 글자 경로의 동결 설정(D59 19c) — `RawWorkflowConfig`의 칸이 **아니다**.

    설정에 칸을 더하면 엄격한 왕복(`_config_from_mapping`)이 기존 작업공간을 못 열고, 내부
    whisper 청크 열쇠가 `asdict(RawWorkflowConfig)`를 통째로 해시하므로 이미 산 전사가 모두
    낡는다(D47·D55의 교훈). 그래서 글자 경로의 모델·언어·문맥은 여기 있고, 글자 캐시 열쇠
    (`source_text.text_chunk_key`)에만 실린다. 모델은 확인된 문구 역할 모델이다 — 다른 값은
    `source_text_problems`가 거부한다(조용한 모델 교체 없음).
    """

    schema: str = "raw-source-asr-text/v1"
    model: str = RawModelRoles().text_model
    languages: tuple[str, ...] = ("ko",)
    prompt: str = SOURCE_TEXT_PROMPT


SOURCE_TEXT_SETTINGS = SourceTextSettings()

TEXT_MODEL_LABELS = {RawModelRoles().text_model: "GPT-Transcribe"}
"""견적 문구에 쓰는 두 번째 귀(문구 역할) 모델의 이름(D59 19d) — 모델 이름은 견적에서만
보인다."""


def text_model_label(model: str) -> str:
    """견적의 「다른 귀(…)」 괄호 안 이름 — Gemini 전사 엔진(W-35b)은 그 프로파일의 이름, 등록된
    이름이 없으면 모델 이름 그대로."""
    profile = GEMINI_TRANSCRIBE_PROFILES.get(model)
    if profile is not None:
        return profile.label
    return TEXT_MODEL_LABELS.get(model, model)


def source_text_problems(settings: SourceTextSettings) -> list[str]:
    """글자 경로 설정의 자기 일관성 — 확인된 문구 모델·비지 않은 언어·제어문자 없는 문맥."""
    problems: list[str] = []
    if settings.schema != SourceTextSettings().schema:
        problems.append(f"글자 경로 schema는 {SourceTextSettings().schema}여야 한다")
    if settings.model != RawModelRoles().text_model:
        problems.append(
            f"글자 경로 모델은 확인된 문구 모델({RawModelRoles().text_model})이어야 한다"
        )
    if not settings.languages:
        problems.append("글자 경로의 예상 언어가 비었다")
    problems += hint_problems("글자 경로 예상 언어", settings.languages)
    if not settings.prompt.strip():
        problems.append("글자 경로 문맥이 비었다")
    elif any(unicodedata.category(char).startswith("C") for char in settings.prompt):
        problems.append("글자 경로 문맥에 줄바꿈·제어문자가 있다")
    return problems


@dataclass(frozen=True)
class SubtitleSuspectSettings:
    """원본 자막 「확인 필요」 표시의 기준(D59 19e) — **표시만** 한다. 외부 근거 없는 조정 대상.

    `RawWorkflowConfig`·`RawUnderstandingSettings`·`RawTierSettings`의 칸이 **아니고** 어느 지문·
    캐시 열쇠·승인·견적에도 실리지 않는다 — 바꿔도 아무것도 다시 사지 않는다. 표시가 자막을 지우거나
    숨기거나 고치지 않으므로(규칙 4·9) 값이 틀려도 잃는 것은 사람의 눈길뿐이다.

    - `repeat_run_min`: 접은 글자(공백·부호·대소문자)가 같은 큐가 이만큼 **이어지면** 표시한다.
      2는 「네」「네」 같은 맞장구까지 걸어 3이다(실측 18개 원본: 2개짜리 런 6개, 3개 이상 11개 —
      「개좋아」 35·11, 「이곳은 정말 아름다운 곳입니다」 18, 「Cut」 13·7, 「水が少し」 5 …).
    - `boilerplate_phrases`: whisper가 조각 끝 무음에서 지어내곤 하는 영상 끝 인사말. 접은 글자가 큐
      안에 들어 있으면 표시한다(「다음 영상에서 만나요!」도 걸린다).
    - `allowed_letter_scripts`: 글자(유니코드 L 범주)의 이름에 이 낱말 중 하나가 있어야 한다 —
      한글·라틴 밖의 글자(키릴 `усhod`, 일본어 `水が少し`)를 짚는다. 숫자·부호·기호는 보지 않고,
      수식 글자(Lm — `ʼ`·`ˈ`)도 보지 않으며, NFKC로 허용 낱말의 글자가 되는 호환 글자(`º`·`ª`·`ℓ`)는
      받는다.
    - `allowed_letters`: 이름이 허용 낱말에 없고 NFKC로도 되지 않지만 받는 글자 — 단위의 마이크로
      기호(`µ`, U+00B5 — NFKC가 그리스 뮤로 접는다). 그리스 뮤(U+03BC) 자체는 여전히 다른 문자다.
    - `small_vocabulary_run_min`·`small_vocabulary_max_kinds`(W-29a, D77): 접은 글자가 이만큼 **적은
      종류**(2종 이상, 종류마다 두 번 이상, 한 종류가 절반 이하)뿐인 줄이 이만큼 이어지면 표시한다 —
      whisper가 「Be safe/Be happy/Be cool」을 18줄 번갈아 지어냈다(같은 말 하나의 런은
      `repeat_run_min`의 몫). 6줄은 맞장구 몇 마디를 넘는다.
    - `symbols_only_min_chars`(W-29a, D77): 글자 없이 부호·기호만 이만큼 이어진 줄을 표시한다 —
      실측 「¡¡¡¡…」 6.7초. 말 사이 쉼으로 흔한 「…」·「...」·「?!」(1–3자)는 걸리지 않게 바닥이
      `SYMBOLS_ONLY_FLOOR`(4)다. 기본 6이면 「......」(6자)는 걸린다.
    - `low_confidence_avg_logprob`·`low_confidence_no_speech_prob`(W-25f, D76): whisper 세그먼트의
      평균 로그 확률이 이보다 **낮거나** 무음 확률이 이 **이상**이면 「전사 확신이 낮습니다」다.
      whisper 레퍼런스 구현의 `logprob_threshold`(−1.0)·`no_speech_threshold`(0.6) 그대로다.
    """

    repeat_run_min: int = 3
    boilerplate_phrases: tuple[str, ...] = (
        "시청해 주셔서 감사합니다",
        "영상 봐주셔서 감사합니다",
        "다음 영상에서 만나요",
        "구독과 좋아요 부탁드립니다",
        # W-29a(D77) — 실측 reelog 환각(소음 위 whisper): 끝인사·광고 고지·인사 문구
        "다음에 만나요",
        "찾아뵐게요",
        "Thanks for watching",
        "수고하셨습니다",
        "유료광고를 포함",
        "맛있게 잘 먹었습니다",
    )
    allowed_letter_scripts: tuple[str, ...] = ("HANGUL", "LATIN")
    allowed_letters: tuple[str, ...] = ("\u00b5",)
    low_confidence_avg_logprob: float = -1.0
    low_confidence_no_speech_prob: float = 0.6
    small_vocabulary_run_min: int = 6
    small_vocabulary_max_kinds: int = 3
    symbols_only_min_chars: int = 6
    gemini_heard_coverage_floor: float = 0.5
    """줄 글자 가운데 Gemini가 들은 말(D88)에서 찾은 몫이 이보다 작으면 「Gemini가 다르게 들음」 —
    외부 근거 없는 조정 대상(다른 귀 조각 게이트 `retext_chunk_similarity_floor`와 같은 0.5에서
    시작)."""
    gemini_heard_min_chars: int = 2
    """비교하는 줄의 최소 글자 수(공백·부호 뺀) — 한 글자 추임새(「네」·「와」)는 몫이 흔들려 짚지
    않는다."""


SUBTITLE_SUSPECT_SETTINGS = SubtitleSuspectSettings()
"""지금 쓰는 「확인 필요」 기준 — 표시 계산은 주입이 없으면 이 하나를 읽는다."""


ORIENTATION_LANDSCAPE = "landscape"
"""가로 — 너비가 높이 이상이다(정사각형도 가로다, D66)."""
ORIENTATION_PORTRAIT = "portrait"
"""세로 — 높이가 너비보다 크다."""
ORIENTATIONS = (ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT)
FRAME_RATE_HIGHER = "higher"
"""높은 프레임레이트 — 소수 쪽 낮은 레이트 클립은 프레임을 되풀이해 보일 뿐 잃지 않는다(D71)."""
FRAME_RATE_LOWER = "lower"
"""낮은 프레임레이트 — 소수 쪽 높은 레이트 클립은 프레임을 건너뛰어 보인다."""
FRAME_RATE_TIES = (FRAME_RATE_HIGHER, FRAME_RATE_LOWER)
MODE_AUTO = "auto"
"""작업공간 모드를 불러온 영상 개수 다수로 정한다(W-37, D97)."""
MODE_LANDSCAPE = ORIENTATION_LANDSCAPE
"""가로 모드(롱폼) — 지금 흐름 그대로(한국어 캡션 → Final Cut 캡션 내보내기 → Subtitle Edit)."""
MODE_PORTRAIT = ORIENTATION_PORTRAIT
"""세로 모드(숏폼) — 같은 첫 보내기 + 편집한 프로젝트에 SRT(ko·en) 다시 넣기(W-38)."""
MODE_CHOICES = (MODE_AUTO, MODE_LANDSCAPE, MODE_PORTRAIT)


@dataclass(frozen=True)
class WorkspaceModeSettings:
    """작업공간 모드(D97)의 자동 규칙 — 영상 개수 다수, 같으면 `tie`의 편.

    `tie`는 외부 근거 없는 선택이다 — 타임라인 방향 동점(`orientation_tie`, D66)과 같은 가로를
    골랐다. 모드는 표지·흐름 게이트일 뿐 문서 바이트를 정하지 않는다 — 어느 지문·캐시 열쇠·승인·
    견적에도 없다."""

    tie: str = ORIENTATION_LANDSCAPE

    def __post_init__(self) -> None:
        if self.tie not in ORIENTATIONS:
            sides = " 또는 ".join(ORIENTATIONS)
            raise ValueError(f"tie는 {sides}여야 한다: {self.tie!r}")


WORKSPACE_MODE = WorkspaceModeSettings()
"""지금 쓰는 모드 규칙 — `workspace_mode.decide_mode`의 기본값이다."""


@dataclass(frozen=True)
class FinalCutTimelineSettings:
    """전체 타임라인 하나(D56)의 문서 설정 — 프로젝트(시퀀스) 방향과 프레임레이트를 고르는 규칙.

    프로젝트 방향은 **켠 스파인 클립 길이**가 많은 쪽을 따른다(W-26, D66). `orientation_tie`는 두
    쪽 길이가 같을 때의 편이다 — 외부 근거 없는 선택이고, 가로 영상 기준인 Final Cut 기본
    프로젝트와 같은 쪽을 골랐다. 그 방향 안에서 프레임레이트도 켠 길이(초)가 많은 쪽이고
    (W-27d, D71), `frame_rate_tie`는 레이트끼리 길이가 같을 때의 편이다 — 역시 외부 근거 없는
    선택이고, 낮은 쪽에 맞추면 소수 쪽 프레임을 버리지만 높은 쪽에 맞추면 되풀이할 뿐이라 높은
    쪽을 골랐다. 문서 바이트만 정하므로 어느 지문·캐시 열쇠·승인·견적에도 없다.
    """

    orientation_tie: str = ORIENTATION_LANDSCAPE
    frame_rate_tie: str = FRAME_RATE_HIGHER
    recommended_only: bool = False
    """켜서 보내는 클립(D93) — 참이면 등급이 추천인 조각과 사람이 「사용」으로 정한 조각만 켜고,
    보통·판정 보류·미분석은 끈 채 보낸다(지우지 않는다 — Final Cut에서 V로 되살아난다). 거짓(빌더의
    기본)이면 D80·D82 그대로 보통·보류·미분석도 켠다. 보내기가 쓰는 값은 `FINALCUT_TIMELINE`이다."""

    drop_disabled: bool = False
    """꺼진 클립을 프로젝트에서 뺀다(D94, 2026-10-27 사용자 지시 「disable된 클립을 프로젝트에서
    제외하는 옵션 — 초록색 클립만 출력」) — 참이면 스파인에 켠 클립만 남기고 시퀀스를 그만큼 줄인다.
    원본 전체는 이벤트 브라우저 클립(키워드 그대로)으로 남아 되찾을 수 있다. 사람이 확인하지 않은
    제외 제안이 빠질 클립에 있으면 거부한다(규칙 9·D35). 작업공간 표지 `finalcut_drop_disabled`가
    켜며 기본은 끔이다."""

    def __post_init__(self) -> None:
        if self.orientation_tie not in ORIENTATIONS:
            sides = " 또는 ".join(ORIENTATIONS)
            raise ValueError(f"orientation_tie는 {sides}여야 한다: {self.orientation_tie!r}")
        if self.frame_rate_tie not in FRAME_RATE_TIES:
            sides = " 또는 ".join(FRAME_RATE_TIES)
            raise ValueError(f"frame_rate_tie는 {sides}여야 한다: {self.frame_rate_tie!r}")


FINALCUT_TIMELINE = FinalCutTimelineSettings(recommended_only=True)
"""지금 쓰는 타임라인 문서 설정 — 내보내기 계층(`finalcut_export._document`)이 빌더에 넘긴다.
추천만 켠다(D93, 2026-10-27 사용자 지시 「노란색도 비활성하고 초록색만 활성해서 나가는 형식」)."""


@dataclass(frozen=True)
class FinalCutTitleSettings:
    """SRT 줄마다 다는 Final Cut 타이틀(D91)의 모양 — 템플릿·글꼴·크기·색·테두리·아래 위치.

    외부 근거 없는 조정 대상이다. 크기는 Basic Title 자신의 기본 글자 크기(1080에서 63)를, 위치는
    Apple FCPXML 예시의 아래 자막 자리(1080에서 가운데보다 450 아래)를 프레임 높이 몫으로 옮겼다 —
    세로 프로젝트에서도 같은 비율이다. 테두리는 기본으로 끈다(Final Cut 자신의 내보내기와 같다).
    문서 바이트만 정하므로 어느 지문·캐시 열쇠·승인·견적에도 없다. 템플릿은 Final Cut의 uid
    철자(`.../Titles.localized/…/<이름>.moti`)로 적는다.
    """

    effect_name: str = "Basic Title"
    effect_uid: str = (
        ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
    )
    font: str = "Apple SD Gothic Neo"
    font_face: str = "Bold"
    font_size_fraction: Fraction = Fraction(7, 120)
    font_color: str = "1 1 1 1"
    stroke_color: str | None = None
    stroke_width_fraction: Fraction = Fraction(0)
    position_y_fraction: Fraction = Fraction(5, 12)
    """가운데에서 아래로 내린 거리 ÷ 프레임 높이 — 음수면 위로 올린다."""

    def __post_init__(self) -> None:
        problems = [
            *_title_text_problems(self),
            *_title_color_problems("font_color", self.font_color),
        ]
        if not 0 < self.font_size_fraction <= 1:
            problems.append(
                f"font_size_fraction는 0보다 크고 1 이하여야 한다: {self.font_size_fraction}"
            )
        if not 0 <= self.stroke_width_fraction < 1:
            problems.append(
                f"stroke_width_fraction는 0 이상 1 미만이어야 한다: {self.stroke_width_fraction}"
            )
        if self.stroke_color is None and self.stroke_width_fraction:
            problems.append("테두리 굵기는 stroke_color가 있을 때만 줄 수 있다")
        if self.stroke_color is not None:
            problems += _title_color_problems("stroke_color", self.stroke_color)
        if abs(self.position_y_fraction) > Fraction(1, 2):
            problems.append(
                f"position_y_fraction는 화면 안(±1/2)이어야 한다: {self.position_y_fraction}"
            )
        if problems:
            raise ValueError("Final Cut 타이틀 설정이 잘못됐다 — " + " · ".join(problems))


def _title_text_problems(settings: FinalCutTitleSettings) -> list[str]:
    problems = [
        f"{name}가 비었다"
        for name in ("effect_name", "font", "font_face")
        if not str(getattr(settings, name)).strip()
    ]
    if not settings.effect_uid.strip().endswith(".moti"):
        problems.append(
            f"effect_uid는 Final Cut 템플릿(.moti) 철자여야 한다: {settings.effect_uid!r}"
        )
    return problems


def _title_color_problems(name: str, color: str) -> list[str]:
    """Final Cut 색 철자 — `r g b a` 네 수, 각각 0–1."""
    try:
        values = [float(part) for part in color.split()]
    except ValueError:
        values = []
    if len(values) != 4 or not all(0 <= value <= 1 for value in values):
        return [f"{name}는 0–1 사이 수 넷(r g b a)이어야 한다: {color!r}"]
    return []


FINALCUT_TITLES = FinalCutTitleSettings()
"""지금 쓰는 타이틀 모양 — 타이틀 보내기(`finalcut_export.export`)가 `finalcut_titles`에 넘긴다."""


def subtitle_suspect_problems(settings: SubtitleSuspectSettings) -> list[str]:
    """값역 — 런 길이는 2 이상의 정수, 인사말은 글자·숫자가 든 줄, 문자 이름은 대문자 낱말."""
    problems: list[str] = []
    run = settings.repeat_run_min
    if isinstance(run, bool) or not isinstance(run, int) or run < 2:
        problems.append(f"같은 말 런 길이는 2 이상의 정수여야 한다: {run!r}")
    for phrase in settings.boilerplate_phrases:
        if not isinstance(phrase, str) or not any(char.isalnum() for char in phrase):
            problems.append(f"영상 끝 인사말에 글자가 없다: {phrase!r}")
    problems += hint_problems("영상 끝 인사말", tuple(map(str, settings.boilerplate_phrases)))
    if not settings.allowed_letter_scripts:
        problems.append("허용 문자 이름이 비었다")
    for script in settings.allowed_letter_scripts:
        if not isinstance(script, str) or not script.isascii() or not script.isupper():
            problems.append(f"허용 문자 이름은 유니코드 이름의 대문자 낱말이어야 한다: {script!r}")
    for letter in settings.allowed_letters:
        if not isinstance(letter, str) or len(letter) != 1:
            problems.append(f"허용 글자는 한 글자씩이어야 한다: {letter!r}")
    vocabulary = (settings.small_vocabulary_run_min, settings.small_vocabulary_max_kinds)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in vocabulary):
        problems.append(f"작은 어휘 런 기준은 정수여야 한다: {vocabulary!r}")
    elif vocabulary[0] < 3 or vocabulary[1] < 2:
        problems.append(f"작은 어휘 런은 3줄 이상 · 2종 이상이어야 한다: {vocabulary!r}")
    symbols = settings.symbols_only_min_chars
    if isinstance(symbols, bool) or not isinstance(symbols, int) or symbols < SYMBOLS_ONLY_FLOOR:
        problems.append(
            f"부호뿐인 줄 기준은 {SYMBOLS_ONLY_FLOOR} 이상의 정수여야 한다 — 「...」·「?!」"
            f" 같은 쉼을 짚지 않게: {symbols!r}"
        )
    floor = settings.gemini_heard_coverage_floor
    if not _finite_float(floor) or not 0 < floor <= 1:
        problems.append(f"Gemini가 들은 말의 몫 기준은 (0, 1]이어야 한다: {floor!r}")
    shortest = settings.gemini_heard_min_chars
    if isinstance(shortest, bool) or not isinstance(shortest, int) or shortest < 1:
        problems.append(f"Gemini 비교의 최소 글자 수는 1 이상의 정수여야 한다: {shortest!r}")
    logprob = settings.low_confidence_avg_logprob
    if not _finite_float(logprob) or logprob > 0:
        problems.append(f"낮은 확신의 로그 확률 기준은 0 이하의 유한 실수여야 한다: {logprob!r}")
    no_speech = settings.low_confidence_no_speech_prob
    if not _finite_float(no_speech) or not 0 < no_speech <= 1:
        problems.append(f"낮은 확신의 무음 확률 기준은 (0, 1]이어야 한다: {no_speech!r}")
    return problems


def _finite_float(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


@dataclass(frozen=True)
class RawWorkflowConfig:
    """v2 원본 워크플로의 최상위 설정 — 기본은 오프라인이다(계획 §14.3)."""

    workflow_schema_version: str = WORKFLOW_SCHEMA_VERSION
    models: RawModelRoles = RawModelRoles()
    caption: RawCaptionContract = RawCaptionContract()
    registry: RawModelRegistry = RawModelRegistry()
    pricing: RawPricingSnapshot = RawPricingSnapshot()
    network_mode: str = "offline"
    require_explicit_authorization: bool = True


# ─────────────────────────────────────────────────────────────
#  의미 분석 모델 공급자(D54·D58) — Luna 역할을 Claude가 맡는다(기본 Sonnet 5.5)
# ─────────────────────────────────────────────────────────────

ANALYSIS_PROVIDER_OPENAI = "openai"
ANALYSIS_PROVIDER_ANTHROPIC = "anthropic"
ANALYSIS_PROVIDER_GOOGLE = "google"
OFFICIAL_PRICE_PROVIDERS = frozenset({ANALYSIS_PROVIDER_ANTHROPIC, ANALYSIS_PROVIDER_GOOGLE})
"""공식 단가 스냅샷으로 창당 상한을 재는 공급자(D54·D86) — Luna(OpenAI)만 단가 미확인이다."""

OPENAI_LUNA_MODEL = "gpt-6-luna"
"""OpenAI 쪽 의미 분석 모델(Luna) — W-18 이전의 기본이고, 지금도 고를 수 있다(D58)."""

CLAUDE_MODEL_PREFIX = "claude-"
"""Claude 모델 이름의 접두어 — 공급자 판정의 유일한 근거다. 등록되지 않은 이름은 설정이 거부한다."""

CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
"""등록된 Claude 모델이 모두 받는 `output_config.effort` 값.

Opus 5.5(2026-10-09)·Sonnet 5.5(2026-10-11) 둘 다 다섯 단계 전부다(effort 문서). 모델마다
갈리는 날이 오면 이 공용 표가 아니라 프로파일 칸으로 옮긴다 — 지금은 같은 값이다."""

CLAUDE_MAX_OUTPUT_TOKENS_LIMIT = 128_000
"""등록된 Claude 모델의 동기 Messages 출력 상한 — Opus 5.5·Sonnet 5.5 모두 128K(모델 문서).

`max_tokens`는 이보다 클 수 없다. 적응형 생각 토큰도 이 안에 든다."""

PRICE_TOKEN_UNIT = Decimal(1_000_000)
"""단가의 단위 — 100만 토큰당 USD."""

CLAUDE_STANDARD_INFERENCE_GEO = "global"
"""표준 단가의 추론 지역(데이터 상주 문서, 2026-10-09) — `us`는 모든 토큰 범주가 1.1배다.

요청에 빠지면 작업공간의 `default_inference_geo`가 쓰인다(US로 자동 이전된 조직이 있다).
명시적으로 보내면 US로 묶인 작업공간은 청구 전에 4xx로 닫힌다."""

CLAUDE_STANDARD_USAGE_GEOS = ("global",)
"""응답 `usage.inference_geo`가 표준 단가인 값 — 없으면(None)도 표준으로 본다."""

CLAUDE_STANDARD_USAGE_TIERS = ("standard",)
"""응답 `usage.service_tier`가 표준 단가인 값 — 없으면(None)도 표준으로 본다.

요청에는 `service_tier`를 싣지 않는다 — Opus 5.5에는 우선 등급이 없어 아무 일도 하지
않고, `standard_only`를 받아 주는지 문서로 확인되지 않는다(400 위험). Sonnet 5.5도 같은
이유로 싣지 않는다. 응답 쪽 검사만 둔다."""

TRANSCRIPTION_IDENTITY_LUNA_MODEL = "gpt-6-luna"
"""전사 전용 열쇠에 고정해 두는 의미 분석 모델(D54).

내부 전사 캐시·최종 자막 지문은 설정 전체를 묶는다. 의미 분석 모델이 그 안에서 움직이면
모델을 바꾸는 것만으로 whisper를 다시 사게 된다 — 전사는 그 모델을 듣지 않는다. 기존
작업공간의 열쇠가 그대로이도록 W-14 이전의 기본값으로 못 박는다. 기본값이 바뀌어도
이 값은 움직이지 않는다 — 기본이 Sonnet 5.5가 된 뒤에도(D58) 글자 그대로 둔다.
`OPENAI_LUNA_MODEL`을 가리키지 않는 것도 그래서다: 그 값이 바뀌어도 열쇠는 그대로여야 한다.
"""


@dataclass(frozen=True)
class RawClaudeAnalysisProfile:
    """Claude 의미 분석의 동결 프로파일 — RawWorkflowConfig **밖**에 산다(D54).

    설정에 칸을 더하면 엄격한 왕복(`_config_from_mapping`)이 기존 작업공간을 못 열고,
    설정 전체를 묶는 지문이 전부 흔들린다. 그래서 작업공간은 모델 이름만 저장하고
    단가·엔드포인트·출력 한도는 이 등록표가 정한다.

    칸의 기본값은 Claude Opus 5.5의 2026-10-09 공식 가격표 확인값이다(D54). 다른 모델은
    아래 등록표에서 자기 값으로 덮는다(Sonnet 5.5 — D58). 단가는 0원으로 두지 마라. 계정
    접근·유료 호출은 시험하지 않았다(사람만 한다). `effort`·`max_output_tokens`·`quote_*`는
    외부 근거 없는 조정 대상이다. 이미지 토큰은 ⌈w/28⌉×⌈h/28⌉, 이미지당 4784가 상한이다.
    """

    model: str = "claude-opus-5-5"
    label: str = "Claude Opus 5.5"
    api_base_url: str = "https://api.anthropic.com"
    api_version: str = "2023-06-01"
    timeout_seconds: float = 300.0
    effort: str = "medium"
    max_output_tokens: int = 8192
    inference_geo: str = CLAUDE_STANDARD_INFERENCE_GEO
    pricing_checked_at: date = date(2026, 10, 9)
    pricing_revision: str = "anthropic-claude-opus-5-5-2026-10-09"
    currency: str = "USD"
    input_per_mtok: Decimal = Decimal("4")
    output_per_mtok: Decimal = Decimal("20")
    cache_write_5m_per_mtok: Decimal = Decimal("5")
    cache_write_1h_per_mtok: Decimal = Decimal("8")
    cache_read_per_mtok: Decimal = Decimal("0.20")
    batch_input_per_mtok: Decimal = Decimal("2")
    batch_output_per_mtok: Decimal = Decimal("10")
    image_patch_pixels: int = 28
    image_max_visual_tokens: int = 4784
    quote_text_tokens_per_window: int = 12000
    quote_overhead_tokens: int = 2000
    account_access_status: str = "unverified"
    integration_test_status: str = "not_run"
    source_urls: tuple[str, ...] = (
        "https://platform.claude.com/docs/en/about-claude/pricing",
        "https://platform.claude.com/docs/en/build-with-claude/vision",
        "https://platform.claude.com/docs/en/build-with-claude/structured-outputs",
        "https://platform.claude.com/docs/en/api/errors",
        "https://platform.claude.com/docs/en/api/versioning",
    )


CLAUDE_OPUS_ANALYSIS_PROFILE = RawClaudeAnalysisProfile()
"""Claude Opus 5.5 — D54의 프로파일 그대로다(값은 바꾸지 않았다)."""

CLAUDE_SONNET_ANALYSIS_PROFILE = RawClaudeAnalysisProfile(
    model="claude-sonnet-5-5",
    label="Claude Sonnet 5.5",
    max_output_tokens=12000,
    pricing_checked_at=date(2026, 10, 11),
    pricing_revision="anthropic-claude-sonnet-5-5-2026-10-11",
    input_per_mtok=Decimal("2"),
    output_per_mtok=Decimal("10"),
    cache_write_5m_per_mtok=Decimal("2.50"),
    cache_write_1h_per_mtok=Decimal("4"),
    cache_read_per_mtok=Decimal("0.10"),
    batch_input_per_mtok=Decimal("1"),
    batch_output_per_mtok=Decimal("5"),
    source_urls=(
        "https://platform.claude.com/docs/en/about-claude/pricing",
        "https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
        "https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5",
        "https://platform.claude.com/docs/en/build-with-claude/vision",
        "https://platform.claude.com/docs/en/build-with-claude/effort",
        "https://platform.claude.com/docs/en/build-with-claude/structured-outputs",
    ),
)
"""Claude Sonnet 5.5 — 영상 프레임 추천 평가의 기본 모델(D58, 2026-10-11 공식 문서 확인값).

단가($/100만 토큰): 입력 2 · 출력 10 · 캐시 쓰기 5분 2.50 · 1시간 4 · 읽기 0.10 · 배치 1/5.
이미지는 고해상도 등급(Claude 4.7 이후 — 28px 조각, 장당 4784)이라 Opus 5.5와 같다. 지역은
표준(global) — US 전용은 1.1배다. 엔드포인트·버전·시간 한도는 Opus 5.5와 같다.

적응형 생각이 기본으로 켜져 있고 생각 토큰도 `max_tokens` 안에 든다(effort 문서). 그래서
출력 한도를 Opus의 8192보다 넉넉한 12000으로 둔다 — **외부 근거 없는 조정 대상**이다. effort는
`medium`을 명시한다(이 모델의 API 기본은 `high`다). 창당 보수적 상한은 약 $0.16이다.
`thinking: disabled`·기본값이 아닌 temperature·강제 `tool_choice`는 400이라 본문에 싣지 않는다.
"""

CLAUDE_ANALYSIS_PROFILES: Mapping[str, RawClaudeAnalysisProfile] = MappingProxyType(
    {
        CLAUDE_SONNET_ANALYSIS_PROFILE.model: CLAUDE_SONNET_ANALYSIS_PROFILE,
        CLAUDE_OPUS_ANALYSIS_PROFILE.model: CLAUDE_OPUS_ANALYSIS_PROFILE,
    }
)
"""모델 이름 → 프로파일(읽기 전용). 여기 없는 `claude-*`는 설정이 거부한다.

클라이언트(엔드포인트·버전·시간 한도)·단가·상한은 언제나 고른 모델의 프로파일에서 온다."""

GEMINI_MODEL_PREFIX = "gemini-"
"""Gemini 모델 이름의 접두어(W-30, D84·D86) — 공급자 판정의 근거다. 등록되지 않은 이름은 설정이
거부한다."""

GEMINI_THINKING_LEVELS = ("LOW", "MEDIUM", "HIGH")
"""등록된 Gemini 모델이 모두 받는 `thinkingConfig.thinkingLevel` 값(thinking 문서, 2026-10-25) —
3.8 Flash·4 Argon 둘 다 low·medium·high이고 기본은 medium이다. `MINIMAL`은 이 둘이 받지 않는다."""

GEMINI_MAX_OUTPUT_TOKENS_LIMIT = 65_536
"""동기(background=False) generateContent의 `maxOutputTokens` 상한(thinking 문서) — 넘기면 400이다.

생각 토큰도 이 안에 든다(「including thought tokens」, 기반 시설이 강제하는 끊김) — 그래서 창당
출력 상한은 이 한도 하나로 생각까지 덮는다."""

GEMINI_MEDIA_RESOLUTION_HIGH = "MEDIA_RESOLUTION_HIGH"
"""이미지 부분마다 다는 해상도(`Part.mediaResolution.level`) — Gemini 3·4의 이미지 권장값(media
resolution 문서). 이 값에서 이미지 한 장의 토큰은 크기와 무관하게 `image_tokens`(1120) 이하다."""


@dataclass(frozen=True)
class RawGeminiAnalysisProfile:
    """Gemini 의미 분석의 동결 프로파일 — Claude 프로파일처럼 RawWorkflowConfig **밖**에 산다(D84).

    작업공간은 모델 이름만 저장하고 단가·엔드포인트·출력 한도는 이 등록표가 정한다. 칸의 기본값은
    Gemini 3.8 Flash의 공식 가격표(https://ai.google.dev/gemini-api/docs/pricing, 갱신 2026-10-22,
    확인 2026-10-25) Standard 판이다. 단가는 0원으로 두지 마라.

    `price_valid_through`는 공식 표가 미리 알린 인상 전 마지막 날이다 — 그날이 지나면 이 단가로
    매기지 않는다(가격 미정, 0원이 아니다). 새 단가를 확인해 등록표를 고치기 전까지 견적은 닫힌다.
    창 클립(D85)은 초당 `video_frames_per_second`장 × `video_frame_tokens` + 소리 초당
    `audio_tokens_per_second`로 센다 — 공식 문서 두 곳의 값이 달라(영상 장당 258·280, 소리 25·32)
    큰 쪽을 상한으로 쓴다. `thinking_level`·`max_output_tokens`·`quote_*`는 외부 근거 없는 조정
    대상이다. 결제 연결 키만 쓴다 — 무료 등급은 입력을 제품 개선에 쓴다(가격표). 계정 접근·유료
    호출은 시험하지 않았다.
    """

    model: str = "gemini-3.8-flash"
    label: str = "Gemini 3.8 Flash"
    api_base_url: str = "https://generativelanguage.googleapis.com"
    api_version: str = "v1beta"
    timeout_seconds: float = 300.0
    thinking_level: str = "MEDIUM"
    max_output_tokens: int = 12000
    media_resolution: str = GEMINI_MEDIA_RESOLUTION_HIGH
    image_tokens: int = 1120
    video_frame_tokens: int = 280
    video_frames_per_second: int = 1
    audio_tokens_per_second: int = 32
    pricing_checked_at: date = date(2026, 10, 25)
    pricing_revision: str = "google-gemini-3-8-flash-2026-10-22"
    price_valid_through: date = date(2026, 12, 31)
    currency: str = "USD"
    input_per_mtok: Decimal = Decimal("0.75")
    output_per_mtok: Decimal = Decimal("3.75")
    quote_text_tokens_per_window: int = 12000
    quote_overhead_tokens: int = 2000
    account_access_status: str = "unverified"
    integration_test_status: str = "not_run"
    source_urls: tuple[str, ...] = (
        "https://ai.google.dev/gemini-api/docs/pricing",
        "https://ai.google.dev/gemini-api/docs/media-resolution",
        "https://ai.google.dev/gemini-api/docs/thinking",
        "https://ai.google.dev/api/generate-content",
        "https://ai.google.dev/gemini-api/docs/migrate-to-interactions",
    )


GEMINI_FLASH_ANALYSIS_PROFILE = RawGeminiAnalysisProfile()
"""Gemini 3.8 Flash — 입력 $0.75 · 출력(생각 포함) $3.75 / 100만 토큰, 2026-12-31까지(2027-01-01부터
$1.50 · $7.50). 창당 보수적 상한은 클립(D85)을 넣어 약 $0.068이다(Sonnet 5.5의 약 $0.16의 40% 남짓 —
대부분 출력 한도 12000토큰 몫이다)."""

GEMINI_ARGON_ANALYSIS_PROFILE = RawGeminiAnalysisProfile(
    model="gemini-4-argon",
    label="Gemini 4 Argon",
    pricing_revision="google-gemini-4-argon-2026-10-22",
    price_valid_through=date(2026, 11, 30),
    input_per_mtok=Decimal("2"),
    output_per_mtok=Decimal("10"),
)
"""Gemini 4 Argon — 입력 $2 · 출력(생각 포함) $10 / 100만 토큰, 2026-11-30까지(2026-12-01부터
$4 · $20). 무료 등급이 없다. 출력·생각 한도는 Flash와 같은 값으로 둔다(조정 대상). 창당 보수적
상한은 클립을 넣어 약 $0.180이다(Sonnet 5.5와 비슷하다)."""

GEMINI_ANALYSIS_PROFILES: Mapping[str, RawGeminiAnalysisProfile] = MappingProxyType(
    {
        GEMINI_FLASH_ANALYSIS_PROFILE.model: GEMINI_FLASH_ANALYSIS_PROFILE,
        GEMINI_ARGON_ANALYSIS_PROFILE.model: GEMINI_ARGON_ANALYSIS_PROFILE,
    }
)
"""모델 이름 → 프로파일(읽기 전용). 클라이언트·단가·상한은 언제나 고른 모델의 프로파일에서 온다."""

GEMINI_TRANSCRIBE_MODES = ("verbatim",)
"""단어 시각과 함께 쓸 수 있는 전사 방식 — `smart`는 공급자가 시각과 함께 받지 않는다(D92)."""
GEMINI_TRANSCRIBE_MIN_CHUNK_SECONDS = 180.0
"""1단계 조각의 상한(`analysis_config.MAX_ASR_CHUNK_SECONDS`)과 같은 값 — 프로파일의 단어 시각 길이
한도는 이보다 짧을 수 없다(raw config는 다른 설정을 import하지 않는 잎이라 값을 옮겼다 — 시험이
같음을 잠근다)."""


@dataclass(frozen=True)
class RawGeminiTranscribeProfile:
    """시간 엔진 Gemini 3.5 Transcribe의 동결 프로파일(W-31, D92) — RawWorkflowConfig **밖**에 산다.

    공식 문서(https://ai.google.dev/gemini-api/docs/transcribe, 확인 2026-10-26): `POST
    /v1beta/interactions`, verbatim + `timestamp_granularities: ["word"]` → 단어마다 `start_offset`·
    `end_offset`. 단어 시각을 켜면 길이 한도가 짧아진다(Gemini 문서 30분 · Cloud 문서 15분 — 짧은
    쪽을 쓴다). 인라인 요청은 20 MB까지. 단가는 오디오 입력 분당 $0.003 + 글자 출력 분당 $0.002
    (https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-transcribe). 0원으로 두지 마라.

    상태 표기가 엇갈린다(모델 표 Stable · 출시 글 2026-08-26 public preview · Cloud 문서의 단어 시각
    Experimental) — 보수적인 쪽(`preview`·`experimental`)을 적는다. 그래서 `price_valid_through`는
    알려진 인상일이 아니라 **재확인 기한**이다(외부 근거 없음) — 지나면 사람이 가격표를 다시 읽고
    고치기 전까지 견적이 닫힌다(D84). 결제 연결 키만 쓴다 — 무료 등급은 입력을 제품 개선에 쓴다.
    실응답 계약은 2026-11-01 사람이 작업대에서 실제 영상을 다시 분석해 확인했다(REST 꼴 응답이
    해석기를 지나 줄 19·단어 68 — W-52b2, D104). 계정 접근은 여전히 기록만 한다.
    """

    model: str = "gemini-3.5-transcribe"
    label: str = "Gemini 3.5 Transcribe"
    api_base_url: str = "https://generativelanguage.googleapis.com"
    api_version: str = "v1beta"
    timeout_seconds: float = 300.0
    mode: str = "verbatim"
    granularities: tuple[str, ...] = ("word",)
    language_codes: Mapping[str, str] = MappingProxyType({"ko": "ko-KR"})
    max_audio_seconds_with_word_times: float = 900.0
    inline_request_max_bytes: int = 20_000_000
    audio_input_per_minute: Decimal = Decimal("0.003")
    text_output_per_minute: Decimal = Decimal("0.002")
    input_per_mtok: Decimal = Decimal("2")
    output_per_mtok: Decimal = Decimal("12")
    """영수증 단가 — 응답이 말한 토큰을 이 값으로 계산한다(분당 값은 견적용, 같은 가격표 칸)."""
    currency: str = "USD"
    pricing_checked_at: date = date(2026, 10, 26)
    pricing_revision: str = "google-gemini-3-5-transcribe-2026-10-26"
    price_valid_through: date = date(2027, 1, 26)
    lifecycle_status: str = "preview"
    word_timestamps_status: str = "experimental"
    account_access_status: str = "unverified"
    integration_test_status: str = "passed"
    source_urls: tuple[str, ...] = (
        "https://ai.google.dev/gemini-api/docs/transcribe",
        "https://ai.google.dev/api/interactions-api",
        "https://ai.google.dev/gemini-api/docs/pricing",
        "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-transcribe",
    )


GEMINI_TRANSCRIBE_PROFILE = RawGeminiTranscribeProfile()
"""Gemini 3.5 Transcribe — 분당 약 $0.005(오디오 $0.003 + 글자 $0.002), 2026-10-26 확인."""

GEMINI_TRANSCRIBE_PROFILES: Mapping[str, RawGeminiTranscribeProfile] = MappingProxyType(
    {GEMINI_TRANSCRIBE_PROFILE.model: GEMINI_TRANSCRIBE_PROFILE}
)
"""모델 이름 → 시간 엔진 프로파일(읽기 전용) — 시간 역할에 설 수 있는 Gemini는 이것뿐이다."""

TIMING_ENGINE_WHISPER = "whisper"
TIMING_ENGINE_GEMINI = "gemini"
TEXT_PASS_GATE = "gate"
"""다른 귀 글자가 D37 게이트를 지나면 자막 글자를 바꾼다(whisper 원본)."""
TEXT_PASS_CANDIDATE = "candidate"
"""다른 귀 글자는 후보·「확인 필요」로만 남는다(Gemini 원본, D92 — 글자와 시각이 한 응답이다)."""


@dataclass(frozen=True)
class TimingEngine:
    """1단계 시간 엔진 하나(D92) — 모델과 그 계약 짝, 그리고 엔진마다 다른 1단계 동작.

    `rescue`는 D78 다시 묻기(온도를 올려 같은 엔진에 다시 묻기)를 받는가, `text_pass`는 다른 귀
    글자를 어떻게 쓰는가다. 엔진은 원본마다 고정된다(W-31d) — 바꾸는 길은 「다시 듣기」뿐이다.
    """

    key: str
    model: str
    label: str
    provider: str
    response_format: str
    granularities: tuple[str, ...]
    rescue: bool
    text_pass: str


TIMING_ENGINES: Mapping[str, TimingEngine] = MappingProxyType(
    {
        TIMING_ENGINE_WHISPER: TimingEngine(
            key=TIMING_ENGINE_WHISPER,
            model=RawModelRoles().timing_model,
            label="whisper-1",
            provider=TIMING_PROVIDER_WHISPER_VERBOSE_JSON,
            response_format=TIMING_RESPONSE_FORMAT_VERBOSE_JSON,
            granularities=TIMESTAMP_GRANULARITIES_SEGMENT,
            rescue=True,
            text_pass=TEXT_PASS_GATE,
        ),
        TIMING_ENGINE_GEMINI: TimingEngine(
            key=TIMING_ENGINE_GEMINI,
            model=GEMINI_TRANSCRIBE_PROFILE.model,
            label=GEMINI_TRANSCRIBE_PROFILE.label,
            provider=TIMING_PROVIDER_GEMINI_WORD_INFO,
            response_format=TIMING_RESPONSE_FORMAT_WORD_INFO,
            granularities=TIMESTAMP_GRANULARITIES_WORD,
            rescue=False,
            text_pass=TEXT_PASS_CANDIDATE,
        ),
    }
)
"""엔진 열쇠 → 엔진. 기본(저장된 설정)은 whisper다 — 원본마다 고르는 일은 W-31d가 한다."""


def timing_engine_for_model(model: str) -> TimingEngine:
    for engine in TIMING_ENGINES.values():
        if engine.model == model:
            return engine
    raise ValueError(f"시간 엔진 표에 없는 모델이다: {model!r}")


def timing_engine_view(config: RawWorkflowConfig, engine: TimingEngine) -> RawWorkflowConfig:
    """그 엔진으로 듣는 설정 — 이미 그 엔진이면 **같은 객체**다(열쇠·지문이 바이트 그대로).

    Gemini는 모델·시간 제공자·응답 형식·입자를 함께 바꾼다(한 짝 — `engine_problems`). 저장된 설정의
    칸은 늘지 않는다(`CONFIG_PINNED_FIELDS`)."""
    if config.models.timing_model == engine.model:
        return config
    caption = replace(
        config.caption,
        timing_provider=engine.provider,
        timing_response_format=engine.response_format,
        timestamp_granularities=engine.granularities,
    )
    return replace(
        config, models=replace(config.models, timing_model=engine.model), caption=caption
    )


def engine_problems(config: RawWorkflowConfig) -> list[str]:
    """시간 모델과 시간 제공자는 엔진 표의 한 짝이어야 한다(D92).

    표에 없는 모델은 여기서 보지 않는다 — 사전 검사(`preflight_online_transcription`)가 「조용한
    모델 교체」로 닫는다. Gemini 제공자 계약에 Gemini 엔진이 아닌 모델이 서는 것도 거부한다.
    """
    model = config.models.timing_model
    try:
        engine = timing_engine_for_model(model)
    except ValueError:
        if config.caption.timing_provider == TIMING_PROVIDER_GEMINI_WORD_INFO:
            return [f"Gemini 단어 시각 계약에 Gemini 전사 엔진이 아닌 {model!r}가 섰다"]
        return []
    if config.caption.timing_provider != engine.provider:
        return [
            f"시간 모델 {engine.model!r}의 제공자는 {engine.provider}인데 계약은"
            f" {config.caption.timing_provider}다 — 모델과 계약은 함께 바뀐다"
        ]
    return []


def gemini_transcribe_profile_problems(profile: RawGeminiTranscribeProfile) -> list[str]:
    """프로파일의 자기 일관성 — 단가는 유한한 양수, 방식·시각·한도·엔드포인트는 쓸 수 있는 값."""
    problems = [
        f"{name}이(가) 유한한 양수가 아니다 — 미확인 단가를 무료로 두지 마라"
        for name in (
            "audio_input_per_minute",
            "text_output_per_minute",
            "input_per_mtok",
            "output_per_mtok",
        )
        if not _positive_decimal(getattr(profile, name))
    ]
    if not profile.model.startswith(GEMINI_MODEL_PREFIX):
        problems.append(f"Gemini 모델 이름이 {GEMINI_MODEL_PREFIX!r}로 시작하지 않는다")
    if profile.mode not in GEMINI_TRANSCRIBE_MODES:
        problems.append(f"전사 방식은 단어 시각과 함께 쓸 수 있는 값이어야 한다: {profile.mode!r}")
    if profile.granularities != ("word",):
        problems.append("시간 엔진은 단어 시각(`word`) 하나만 받는다")
    if not profile.language_codes:
        problems.append("언어 잠금 표가 비었다")
    if profile.max_audio_seconds_with_word_times < GEMINI_TRANSCRIBE_MIN_CHUNK_SECONDS:
        problems.append("단어 시각 길이 한도가 1단계 조각(180초)보다 짧다")
    if profile.inline_request_max_bytes < 1:
        problems.append("인라인 요청 한도는 1바이트 이상이어야 한다")
    if not _https_url(profile.api_base_url) or urlsplit(profile.api_base_url).path not in ("", "/"):
        problems.append("api_base_url은 경로 없는 https 호스트여야 한다")
    if not math.isfinite(profile.timeout_seconds) or profile.timeout_seconds <= 0:
        problems.append("timeout_seconds는 유한한 양수여야 한다")
    if profile.price_valid_through < profile.pricing_checked_at:
        problems.append("단가 재확인 기한이 확인한 날보다 앞이다")
    return problems


def gemini_transcribe_language_codes(
    languages: tuple[str, ...], profile: RawGeminiTranscribeProfile | None = None
) -> tuple[str, ...]:
    """계약의 언어(`ko`) → 공급자의 언어 잠금(`ko-KR`). 표에 없거나 비면 보내지 않는다."""
    chosen = profile or GEMINI_TRANSCRIBE_PROFILE
    if not languages or any(code not in chosen.language_codes for code in languages):
        raise ValueError(f"Gemini 전사의 언어 잠금으로 옮길 수 없는 언어다: {languages!r}")
    return tuple(chosen.language_codes[code] for code in languages)


def gemini_transcribe_price_per_minute(
    profile: RawGeminiTranscribeProfile, *, today: date
) -> Decimal | None:
    """분당 단가(오디오 + 글자) — 재확인 기한이 지나면 None(가격 미정, 0원이 아니다)."""
    if today > profile.price_valid_through:
        return None
    return profile.audio_input_per_minute + profile.text_output_per_minute


@dataclass(frozen=True)
class WordCueSettings:
    """Gemini 단어 → 자막 줄 묶기(W-31b, D92) — `RawWorkflowConfig` 밖이고 어느 승인·견적에도 없다.

    끊는 자리는 엔진이 준 단어 경계뿐이다: 문장 끝 부호 뒤 · 앞 단어 끝과의 쉼이
    `pause_break_seconds` 이상인 단어 앞 · 글자(`max_cue_chars`)·길이(`max_cue_seconds`) 한도를
    넘기 전. 글자·길이 한도는
    엔진 `Config`의 `max_chars_per_cue`(16자 × 2줄)·`max_duration`(7초) **그대로**다 — 시험이 같음을
    못 박는다(엔진을 바꾸면 같이 바꾼다). 쉼 0.6초는 외부 근거 없는 조정 대상이다(W-31f에서 잰다).
    값이나 규칙을 바꾸면 `rule_tag`를 올린다 — 그 판이 Gemini 원본의 입력 지문에 실려(W-31c·d)
    캐시된 단어로 무료로 다시 묶인다(사람이 고친 자막은 덮지 않는다).
    """

    rule_tag: str = "word-cues/v1"
    max_cue_chars: int = 32
    max_cue_seconds: Fraction = Fraction(7)
    pause_break_seconds: Fraction = Fraction(3, 5)
    sentence_end_marks: tuple[str, ...] = (".", "?", "!", "…", "。", "？", "！")


WORD_CUE_SETTINGS = WordCueSettings()
"""지금 쓰는 단어 묶기 — `word_cues.group`의 기본값이다."""

WORD_TEXT_VERSION = "word-text/v1"
"""Gemini 단어 → whisper 줄 글자 배정(W-35a, D95)의 규칙 판 — 가운데 자리 · 반열린 줄 · 겹치면
가운데가 가까운 줄(같으면 앞). 임계값이 없다. 규칙을 바꾸면 판을 올린다(글자 캐시 열쇠에 실린다,
W-35b)."""


@dataclass(frozen=True)
class GeminiTextSettings:
    """whisper 원본의 Gemini 글자 경로(W-35b, D95)의 동결 설정 — `RawWorkflowConfig`의 칸이 아니다.

    `SourceTextSettings`와 같은 이유로 설정 밖에 있다(칸을 더하면 whisper 청크 열쇠가 모두 낡는다).
    힌트가 없다 — 문맥·용어집·`system_instruction`을 보내지 않는다(규칙 6, D92). 언어는 등록표가
    `ko-KR`로 옮기는 잠금이다. 모델은 등록된 Gemini 전사 엔진이어야 하고 판은 단어 배정 규칙 판이다
    — 둘 다 글자 캐시 열쇠와 요청 신원에 실린다."""

    schema: str = "raw-source-gemini-text/v1"
    model: str = GEMINI_TRANSCRIBE_PROFILE.model
    languages: tuple[str, ...] = ("ko",)
    version: str = WORD_TEXT_VERSION


GEMINI_TEXT_SETTINGS = GeminiTextSettings()


def gemini_text_problems(settings: GeminiTextSettings) -> list[str]:
    """Gemini 글자 경로 설정의 자기 일관성 — 판·등록된 전사 엔진·언어 잠금이 맞는가."""
    problems: list[str] = []
    if settings.schema != GeminiTextSettings().schema:
        problems.append(f"Gemini 글자 경로 schema는 {GeminiTextSettings().schema}여야 한다")
    if settings.model not in GEMINI_TRANSCRIBE_PROFILES:
        problems.append(f"Gemini 글자 경로 모델 {settings.model!r}은(는) 등록된 전사 엔진이 아니다")
    if settings.version != WORD_TEXT_VERSION:
        problems.append(f"Gemini 글자 경로 판은 {WORD_TEXT_VERSION}여야 한다")
    profile = GEMINI_TRANSCRIBE_PROFILES.get(settings.model)
    if not settings.languages:
        problems.append("Gemini 글자 경로의 언어 잠금이 비었다")
    elif profile is not None:
        missing = [code for code in settings.languages if code not in profile.language_codes]
        if missing:
            problems.append(f"Gemini 글자 경로가 옮길 수 없는 언어다: {missing}")
    return problems


@dataclass(frozen=True)
class EngineCompareSettings:
    """시간 엔진 비교 보고서(W-31f, D92)의 「더 낫다」 기준 — 새 엔진이 넘어야 할 선.

    전부 외부 근거 없는 조정 대상이다(W-31f 계획에서 사람에게 제안한 값 그대로). 시작 적중은 이전
    엔진 이상, 끝 적중은 `end_hit_slack`만큼 낮아도 되고, 시작 MAE는 `start_mae_slack`초까지 늘어도
    되며, 환각류 「확인 필요」 줄은 이전 엔진의 `hallucination_share` 이하, 말소리 재현율(Rτ)은
    `recall_slack` 만큼 낮아도 된다. 보고서가 판정을 적을 뿐 기본 엔진을 바꾸지 않는다 — 바꾸는 것은
    사람이다. 어느 지문·캐시 열쇠·승인·견적에도 없다."""

    end_hit_slack: Fraction = Fraction(5, 100)
    start_mae_slack: Fraction = Fraction(5, 100)
    hallucination_share: Fraction = Fraction(1, 2)
    recall_slack: Fraction = Fraction(2, 100)

    def __post_init__(self) -> None:
        for name in ("end_hit_slack", "start_mae_slack", "recall_slack"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}는 0 이상이어야 한다: {getattr(self, name)}")
        if not 0 <= self.hallucination_share <= 1:
            raise ValueError(f"hallucination_share는 0–1이어야 한다: {self.hallucination_share}")


ENGINE_COMPARE_SETTINGS = EngineCompareSettings()
"""지금 쓰는 비교 기준 — `scripts/engine_compare_report.py`가 읽는다."""


def word_cue_problems(settings: WordCueSettings) -> list[str]:
    """묶기 설정의 자기 일관성 — 한도는 양수, 쉼은 0 이상, 문장 끝 부호와 판 이름은 비지 않는다."""
    problems: list[str] = []
    if not settings.rule_tag.strip():
        problems.append("rule_tag가 비었다 — 묶기 규칙의 판이 지문에 실려야 한다")
    if settings.max_cue_chars < 1:
        problems.append("max_cue_chars는 1 이상이어야 한다")
    if settings.max_cue_seconds <= 0:
        problems.append("max_cue_seconds는 양수여야 한다")
    if settings.pause_break_seconds < 0:
        problems.append("pause_break_seconds는 0 이상이어야 한다")
    if not settings.sentence_end_marks or not all(settings.sentence_end_marks):
        problems.append("sentence_end_marks가 비었다")
    return problems


DEFAULT_ANALYSIS_MODEL: str = RawModelRoles().luna_model
PREVIOUS_DEFAULT_ANALYSIS_MODELS = frozenset({CLAUDE_SONNET_ANALYSIS_PROFILE.model})
"""열 때 기본으로 옮겨 줄 예전 기본(D58의 Sonnet 5.5) — 사람이 따로 고른 모델(Opus·Luna·Gemini)은
옮기지 않는다(D87)."""
"""새 작업공간의 의미 분석 모델 — Claude Sonnet 5.5(D58). 기존 작업공간은 저장된 값을 쓴다."""

ANALYSIS_MODEL_CHOICES: tuple[str, ...] = (
    DEFAULT_ANALYSIS_MODEL,
    *(
        model
        for model in (*GEMINI_ANALYSIS_PROFILES, *CLAUDE_ANALYSIS_PROFILES, OPENAI_LUNA_MODEL)
        if model != DEFAULT_ANALYSIS_MODEL
    ),
)
"""고를 수 있는 의미 분석 모델 — 기본(Gemini 3.8 Flash, D87)이 맨 앞, 그다음 Gemini 4 Argon, Claude
Sonnet 5.5, Claude Opus 5.5, gpt-6-luna.

모델을 바꿔도 이미 산 분석은 그대로 남는다 — 다시 분석하려면 새 견적을 보고 승인한다(유료)."""


def analysis_provider(model: str) -> str:
    """의미 분석 모델의 공급자 — Claude·Gemini 접두어면 그 공급자, 아니면 지금처럼 openai."""
    if model.startswith(CLAUDE_MODEL_PREFIX):
        return ANALYSIS_PROVIDER_ANTHROPIC
    if model.startswith(GEMINI_MODEL_PREFIX):
        return ANALYSIS_PROVIDER_GOOGLE
    return ANALYSIS_PROVIDER_OPENAI


def official_price_model(model: str) -> bool:
    """공식 단가 스냅샷으로 창당 상한을 재는 모델인가 — 승인에 금액이 묶이는 모델이다."""
    return analysis_provider(model) in OFFICIAL_PRICE_PROVIDERS


def claude_profile(model: str) -> RawClaudeAnalysisProfile:
    """등록된 Claude 프로파일 — 없는 이름은 조용히 다른 프로파일로 대체하지 않는다."""
    try:
        return CLAUDE_ANALYSIS_PROFILES[model]
    except KeyError as error:
        raise ValueError(f"등록되지 않은 Claude 모델이다: {model!r}") from error


def gemini_profile(model: str) -> RawGeminiAnalysisProfile:
    """등록된 Gemini 프로파일 — 없는 이름은 조용히 다른 프로파일로 대체하지 않는다."""
    try:
        return GEMINI_ANALYSIS_PROFILES[model]
    except KeyError as error:
        raise ValueError(f"등록되지 않은 Gemini 모델이다: {model!r}") from error


def analysis_model_label(model: str) -> str:
    """화면·CLI에 쓰는 이름 — Claude·Gemini는 등록표의 이름, 나머지는 모델 이름 그대로."""
    provider = analysis_provider(model)
    if provider == ANALYSIS_PROVIDER_ANTHROPIC:
        return claude_profile(model).label
    if provider == ANALYSIS_PROVIDER_GOOGLE:
        return gemini_profile(model).label
    return model


def transcription_identity_view(config: RawWorkflowConfig) -> RawWorkflowConfig:
    """전사 전용 열쇠가 보는 설정 — 의미 분석 모델만 고정값으로 바꾼다(D54)."""
    if config.models.luna_model == TRANSCRIPTION_IDENTITY_LUNA_MODEL:
        return config
    pinned = replace(config.models, luna_model=TRANSCRIPTION_IDENTITY_LUNA_MODEL)
    return replace(config, models=pinned)


def stage1_timing_view(config: RawWorkflowConfig) -> RawWorkflowConfig:
    """1단계 내부 전사 요청이 보는 설정 — 입자만 세그먼트+단어로 바꾼다(W-25c, D73).

    청크 캐시 열쇠는 이 보기가 아니라 저장된 설정으로 잰다 — 단어는 덧붙는 재료라 이미 산 전사
    (단어 없음)를 그대로 쓰고, 새로 사는 청크부터 단어가 실린다. Gemini 엔진(D92)은 이미 단어 시각만
    묻는 계약이라 그대로 둔다."""
    if config.caption.timing_provider != TIMING_PROVIDER_WHISPER_VERBOSE_JSON:
        return config
    caption = replace(config.caption, timestamp_granularities=STAGE1_TIMESTAMP_GRANULARITIES)
    return replace(config, caption=caption)


def claude_profile_problems(profile: RawClaudeAnalysisProfile) -> list[str]:
    """프로파일의 자기 일관성 — 단가는 유한한 양수, 한도·엔드포인트는 쓸 수 있는 값."""
    problems = [
        f"{name}이(가) 유한한 양수가 아니다 — 미확인 단가를 무료로 두지 마라"
        for name in (
            "input_per_mtok",
            "output_per_mtok",
            "cache_write_5m_per_mtok",
            "cache_write_1h_per_mtok",
            "cache_read_per_mtok",
            "batch_input_per_mtok",
            "batch_output_per_mtok",
        )
        if not _positive_decimal(getattr(profile, name))
    ]
    if profile.effort not in CLAUDE_EFFORT_LEVELS:
        problems.append(f"effort가 허용 값이 아니다: {profile.effort!r}")
    if profile.inference_geo != CLAUDE_STANDARD_INFERENCE_GEO:
        problems.append("inference_geo는 표준 단가 지역(global)이어야 한다 — US 전용은 1.1배다")
    if not 1 <= profile.max_output_tokens <= CLAUDE_MAX_OUTPUT_TOKENS_LIMIT:
        problems.append(f"max_output_tokens는 1~{CLAUDE_MAX_OUTPUT_TOKENS_LIMIT}이어야 한다")
    if not _https_url(profile.api_base_url):
        problems.append("api_base_url은 호스트가 있는 https URL이어야 한다")
    if not profile.api_version.strip():
        problems.append("api_version이 비어 있다")
    if not math.isfinite(profile.timeout_seconds) or profile.timeout_seconds <= 0:
        problems.append("timeout_seconds는 유한한 양수여야 한다")
    if min(profile.image_patch_pixels, profile.image_max_visual_tokens) < 1:
        problems.append("이미지 토큰 계산값은 1 이상이어야 한다")
    if min(profile.quote_text_tokens_per_window, profile.quote_overhead_tokens) < 0:
        problems.append("견적 여유 토큰은 음수일 수 없다")
    return problems


def gemini_profile_problems(profile: RawGeminiAnalysisProfile) -> list[str]:
    """프로파일의 자기 일관성 — 단가는 유한한 양수, 한도·엔드포인트는 쓸 수 있는 값(D84)."""
    problems = [
        f"{name}이(가) 유한한 양수가 아니다 — 미확인 단가를 무료로 두지 마라"
        for name in ("input_per_mtok", "output_per_mtok")
        if not _positive_decimal(getattr(profile, name))
    ]
    if not profile.model.startswith(GEMINI_MODEL_PREFIX):
        problems.append(f"Gemini 모델 이름이 {GEMINI_MODEL_PREFIX!r}로 시작하지 않는다")
    if profile.thinking_level not in GEMINI_THINKING_LEVELS:
        problems.append(f"thinking_level이 허용 값이 아니다: {profile.thinking_level!r}")
    if profile.media_resolution != GEMINI_MEDIA_RESOLUTION_HIGH:
        problems.append("media_resolution은 이미지 토큰을 고정하는 MEDIA_RESOLUTION_HIGH여야 한다")
    if not 1 <= profile.max_output_tokens <= GEMINI_MAX_OUTPUT_TOKENS_LIMIT:
        problems.append(f"max_output_tokens는 1~{GEMINI_MAX_OUTPUT_TOKENS_LIMIT}이어야 한다")
    if not _https_url(profile.api_base_url) or urlsplit(profile.api_base_url).path not in ("", "/"):
        problems.append("api_base_url은 경로 없는 https 호스트여야 한다")
    if not profile.api_version.strip():
        problems.append("api_version이 비어 있다")
    if not math.isfinite(profile.timeout_seconds) or profile.timeout_seconds <= 0:
        problems.append("timeout_seconds는 유한한 양수여야 한다")
    if (
        min(
            profile.image_tokens,
            profile.video_frame_tokens,
            profile.video_frames_per_second,
            profile.audio_tokens_per_second,
        )
        < 1
    ):
        problems.append("이미지·영상·소리 토큰 상한은 1 이상이어야 한다")
    if min(profile.quote_text_tokens_per_window, profile.quote_overhead_tokens) < 0:
        problems.append("견적 여유 토큰은 음수일 수 없다")
    if profile.price_valid_through < profile.pricing_checked_at:
        problems.append("단가 유효 마지막 날이 확인한 날보다 앞이다")
    return problems


def _positive_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _https_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.netloc)


def validate_raw_workflow_config(config: RawWorkflowConfig) -> None:
    """동결 설정의 자기 일관성을 검사한다 — 어긋나면 ValueError로 전부 보고한다.

    A1+ 서비스는 설정을 쓰기 *전에* 이 검사를 통과시킨다. "조용한 fallback 금지"와
    "오프라인 기본"이 데이터 계약으로 서는 자리다(계획 §0 추가 요구).
    """
    problems = [
        *schema_problems(config),
        *role_problems(config.models),
        *registry_problems(config.registry),
        *pricing_problems(config.pricing),
        *caption_problems(config.caption),
        *engine_problems(config),
        *network_problems(config),
    ]
    if problems:
        raise ValueError("raw 설정이 계약을 어긴다: " + "; ".join(problems))


def schema_problems(config: RawWorkflowConfig) -> list[str]:
    """namespace는 얼어 있다 — 다른 값은 아직 정의된 적이 없다(계획 §13)."""
    problems: list[str] = []
    if config.workflow_schema_version != WORKFLOW_SCHEMA_VERSION:
        problems.append(f"workflow schema는 {WORKFLOW_SCHEMA_VERSION}여야 한다")
    if config.caption.schema_version != CAPTION_SCHEMA_VERSION:
        problems.append(f"caption schema는 {CAPTION_SCHEMA_VERSION}여야 한다")
    return problems


def role_problems(models: RawModelRoles) -> list[str]:
    """역할은 비어 있거나 공백일 수 없고, 문구·시간이 같으면 가짜 시각이 생긴다."""
    problems = [
        f"{role} 모델이 비어 있다"
        for role, model in (
            ("문구", models.text_model),
            ("시간", models.timing_model),
            ("의미 분석", models.luna_model),
        )
        if not model.strip()
    ]
    if models.text_model.strip() and models.text_model == models.timing_model:
        problems.append("문구 모델과 시간 모델이 같다 — 시각 없는 문구에 가짜 시각이 붙는다")
    problems += [
        f"{role} 모델에 Claude를 쓸 수 없다 — Claude는 오디오를 받지 않는다: {model!r}"
        for role, model in (("문구", models.text_model), ("시간", models.timing_model))
        if analysis_provider(model) == ANALYSIS_PROVIDER_ANTHROPIC
    ]
    if analysis_provider(models.text_model) == ANALYSIS_PROVIDER_GOOGLE:
        problems.append(
            "문구 모델에 Gemini를 쓸 수 없다 — Gemini는 영상 분석 역할만 맡는다(D86):"
            f" {models.text_model!r}"
        )
    if (
        analysis_provider(models.timing_model) == ANALYSIS_PROVIDER_GOOGLE
        and models.timing_model not in GEMINI_TRANSCRIBE_PROFILES
    ):
        problems.append(
            "시간 모델에 Gemini를 쓸 수 없다 — Gemini는 영상 분석 역할만 맡는다(D86, 시간"
            f" 역할은 등록된 전사 엔진만 — D92): {models.timing_model!r}"
        )
    return problems + _analysis_profile_problems(models.luna_model)


def _analysis_profile_problems(model: str) -> list[str]:
    """의미 분석 모델이 Claude·Gemini면 등록표에 있어야 하고 프로파일이 계약을 지켜야 한다."""
    provider = analysis_provider(model)
    if provider == ANALYSIS_PROVIDER_ANTHROPIC:
        claude = CLAUDE_ANALYSIS_PROFILES.get(model)
        if claude is None:
            return [f"의미 분석 모델이 등록되지 않은 Claude 모델이다: {model!r}"]
        return claude_profile_problems(claude)
    if provider == ANALYSIS_PROVIDER_GOOGLE:
        gemini = GEMINI_ANALYSIS_PROFILES.get(model)
        if gemini is None:
            return [f"의미 분석 모델이 등록되지 않은 Gemini 모델이다: {model!r}"]
        return gemini_profile_problems(gemini)
    return []


def registry_problems(registry: RawModelRegistry) -> list[str]:
    """등록표 스냅샷의 형태와 확정 회피 방지(계획 §11.9·§11.10).

    종료 차단은 **모든** 수락되는 등록표에서 켜져 있어야 한다 — 리비전 문자열을
    바꾸는 것으로 `blocks_online_timing`을 무력화하는 우회를 허용하지 않는다(fail-closed).
    확인된 리비전 문자열은 확정 종료일의 열쇠다 — 그 리비전을 달고 종료일을 다르게
    적는 스냅샷도 만들어지지 못한다.
    """
    problems: list[str] = []
    if not registry.revision.strip():
        problems.append("등록표 리비전이 비어 있다")
    if registry.timing_shutdown_date < registry.timing_deprecation_notice_date:
        problems.append("종료일이 공보다 빠르다")
    if registry.docs_checked_at < registry.timing_deprecation_notice_date:
        problems.append("문서 확인일이 공보다 빠르다 — 공지 전에 확인할 수는 없다")
    if not registry.block_online_at_shutdown_date:
        problems.append("종료 차단은 모든 등록표에서 켜져 있어야 한다 — 끄는 설정은 없다")
    if registry.revision == CONFIRMED_REGISTRY_REVISION and (
        registry.timing_shutdown_date != CONFIRMED_TIMING_SHUTDOWN_DATE
    ):
        problems.append("확인된 리비전의 종료일은 2027-02-26이다 — 바꾸려면 새 리비전을 내려라")
    return problems


def pricing_problems(pricing: RawPricingSnapshot) -> list[str]:
    """가격 미확인을 0원으로 두지 않는다 — 등록된 스냅샷은 유한한 양수여야 한다(§11.8).

    NaN·Infinity는 `<= 0` 비교를 그냥 통과하므로 유한성부터 본다. JSON 왕복도
    이 검사를 다시 지난다.
    """
    problems: list[str] = []
    if not pricing.currency:
        problems.append("요금 통화가 비어 있다")
    for label, value in (("문구", pricing.text_per_minute), ("시간", pricing.timing_per_minute)):
        if not value.is_finite():
            problems.append(f"{label} 요금이 유한하지 않다 — NaN·Infinity는 가격이 아니다")
        elif value <= Decimal("0"):
            problems.append(f"{label} 요금이 0 이하다 — 미확인 가격을 무료로 두지 마라")
    return problems


SYMBOLS_ONLY_FLOOR = 4
"""「부호뿐인 줄」 기준의 바닥 — 말 사이 쉼(「…」·「...」·「?!」, 3자 이하)을 짚지 않는다(D77)."""

ISO_639_1_LENGTH = 2
"""whisper 단수 `language`의 모양 — ISO 639-1 두 글자 소문자(D77, `transcribe._language_problems`와
같다)."""


def _timing_language_problems(languages: tuple[str, ...]) -> list[str]:
    """예상 언어 첫째는 whisper 단수 `language`로도 간다(D77) — 열 때 막는다(실행 중이 아니라)."""
    if not languages:
        return []
    first = languages[0]
    if len(first) == ISO_639_1_LENGTH and first.isascii() and first.isalpha() and first.islower():
        return []
    return [f"예상 언어 첫째 {first!r}는 whisper 언어 칸(ISO 639-1 두 글자 소문자)이 아니다"]


def _timing_contract_problems(caption: RawCaptionContract) -> list[str]:
    """시간 제공자·응답 형식·입자는 한 짝이다(D92) — whisper 세그먼트 또는 Gemini 단어."""
    contract = TIMING_PROVIDER_CONTRACTS.get(caption.timing_provider)
    if contract is None:
        known = " · ".join(TIMING_PROVIDER_CONTRACTS)
        return [f"시간 제공자는 {known} 가운데 하나여야 한다: {caption.timing_provider!r}"]
    response_format, granularities = contract
    problems: list[str] = []
    if caption.timing_response_format != response_format:
        problems.append(f"{caption.timing_provider}의 시간 응답 형식은 {response_format}여야 한다")
    if caption.timestamp_granularities not in granularities:
        problems.append(
            "시간 입자는 세그먼트, 또는 세그먼트와 단어뿐이다 — 단어만으로는 골격이 없다(D73)"
            if caption.timing_provider == TIMING_PROVIDER_WHISPER_VERBOSE_JSON
            else "Gemini 시간 입자는 단어(`word`)뿐이다 — 줄은 단어 경계에서 묶는다(D92)"
        )
    return problems


def caption_problems(caption: RawCaptionContract) -> list[str]:
    """요청 형식·힌트·금지 조건 — 호출 전에 차단할 조합을 데이터 단계에서 막는다(§11.4)."""
    problems: list[str] = []
    if caption.profile != CAPTION_PROFILE_LATEST_OPENAI_SRT:
        problems.append(f"자막 프리셋은 {CAPTION_PROFILE_LATEST_OPENAI_SRT}여야 한다")
    problems += _timing_contract_problems(caption)
    if caption.text_response_format != TEXT_RESPONSE_FORMAT_JSON:
        problems.append(
            f"문구 응답 형식은 {TEXT_RESPONSE_FORMAT_JSON}여야 한다 — "
            "srt·vtt는 문구 모델에 못 붙인다"
        )
    # 빈 languages·keywords는 합법이다 — "힌트 없음"이지 무발화가 아니다(§11.4,
    # 코디네이터 재결). 요청 어댑터는 빈 배열을 필드째 빼고 보낸다.
    problems += hint_problems("예상 언어", caption.languages)
    problems += _timing_language_problems(caption.languages)
    problems += hint_problems("고유명사 힌트", caption.keywords)
    if caption.allow_silent_model_fallback:
        problems.append("조용한 구형 모델 fallback은 금지다")
    if caption.allow_guessed_timestamps:
        problems.append("근거 없는 시각 생성은 금지다")
    return problems


def hint_problems(label: str, values: tuple[str, ...]) -> list[str]:
    """힌트는 전송 전에 검증한다 — 줄바꿈·제어문자·빈 칸은 못 넣는다(계획 §11.4)."""
    problems: list[str] = []
    for value in values:
        if not value.strip():
            problems.append(f"{label}에 빈 문자열이 있다")
        elif any(unicodedata.category(char).startswith("C") for char in value):
            problems.append(f"{label}에 줄바꿈·제어문자가 있다")
    return problems


def network_problems(config: RawWorkflowConfig) -> list[str]:
    """온라인은 명시적 승인 없이 켜지지 않는다 — 기본은 오프라인이다(계획 §14.3)."""
    problems: list[str] = []
    if config.network_mode not in {"offline", "online"}:
        problems.append(f"알 수 없는 네트워크 모드 {config.network_mode!r}")
    if not config.require_explicit_authorization:
        problems.append("온라인 실행은 명시적 승인 없이 켤 수 없다")
    return problems


def raw_workflow_config_to_json(config: RawWorkflowConfig) -> str:
    """설정을 엄격한 JSON 문자열로 직렬화한다 — provider_snapshot·jobs 기록용(§9.1).

    date는 ISO 문자열로, Decimal은 문자열로 — 부동소수점을 비용 산술에 끌어들이지
    않는다. NaN·Infinity는 `allow_nan=False`가 거부한다.
    """
    return json.dumps(
        _json_safe(config), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    )


def _json_safe(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    return value


def raw_workflow_config_from_json(payload: str) -> RawWorkflowConfig:
    """JSON 문자열에서 설정을 재구성하고 **검증까지 마쳐** 돌려준다(왕복 계약).

    JSON의 NaN·Infinity 상수는 파싱 단계에서 거부하고, 모양이 어긋난 페이로드는
    ValueError로 끝난다 — 삼키거나 조용히 기본값으로 메우지 않는다.
    """
    try:
        raw = json.loads(payload, parse_constant=_reject_json_constant)
        config = _config_from_mapping(raw)
    except (TypeError, KeyError, IndexError, ValueError) as error:
        raise ValueError(f"raw 설정 JSON이 계약과 다르다: {error}") from error
    validate_raw_workflow_config(config)
    return config


def _reject_json_constant(name: str) -> object:
    raise ValueError(f"JSON 상수 {name}는 설정에 올 수 없다")


def _config_from_mapping(raw: object) -> RawWorkflowConfig:
    """직렬화 모양에서 동결 설정으로 — 필드를 하나하나 명시적으로 짓는다.

    알 수 없는 필드는 거부한다(fail-closed) — 모르는 키를 조용히 버리면 스냅샷과
    설정이 서로 다른 세계를 가리키게 된다.
    """
    top = _mapping_of(
        raw,
        (
            "workflow_schema_version",
            "models",
            "caption",
            "registry",
            "pricing",
            "network_mode",
            "require_explicit_authorization",
        ),
        "설정 루트",
    )
    models_raw = _mapping_of(
        top.get("models"), ("text_model", "timing_model", "luna_model"), "models"
    )
    caption_raw = _mapping_of(
        top.get("caption"),
        (
            "schema_version",
            "profile",
            "timing_provider",
            "languages",
            "keywords",
            "text_response_format",
            "timing_response_format",
            "timestamp_granularities",
            "allow_silent_model_fallback",
            "allow_guessed_timestamps",
        ),
        "caption",
    )
    registry_raw = _mapping_of(
        top.get("registry"),
        (
            "revision",
            "docs_checked_at",
            "account_access_status",
            "integration_test_status",
            "timing_deprecation_notice_date",
            "timing_shutdown_date",
            "block_online_at_shutdown_date",
            "source_urls",
        ),
        "registry",
    )
    pricing_raw = _mapping_of(
        top.get("pricing"),
        ("checked_at", "currency", "text_per_minute", "timing_per_minute"),
        "pricing",
    )
    return RawWorkflowConfig(
        workflow_schema_version=_text(top, "workflow_schema_version"),
        network_mode=_text(top, "network_mode"),
        require_explicit_authorization=_bool(top, "require_explicit_authorization"),
        models=RawModelRoles(
            text_model=_text(models_raw, "text_model"),
            timing_model=_text(models_raw, "timing_model"),
            luna_model=_text(models_raw, "luna_model"),
        ),
        caption=RawCaptionContract(
            schema_version=_text(caption_raw, "schema_version"),
            profile=_text(caption_raw, "profile"),
            timing_provider=_text(caption_raw, "timing_provider"),
            languages=_strings(caption_raw, "languages"),
            keywords=_strings(caption_raw, "keywords"),
            text_response_format=_text(caption_raw, "text_response_format"),
            timing_response_format=_text(caption_raw, "timing_response_format"),
            timestamp_granularities=_strings(caption_raw, "timestamp_granularities"),
            allow_silent_model_fallback=_bool(caption_raw, "allow_silent_model_fallback"),
            allow_guessed_timestamps=_bool(caption_raw, "allow_guessed_timestamps"),
        ),
        registry=RawModelRegistry(
            revision=_text(registry_raw, "revision"),
            docs_checked_at=_date(registry_raw, "docs_checked_at"),
            account_access_status=_text(registry_raw, "account_access_status"),
            integration_test_status=_text(registry_raw, "integration_test_status"),
            timing_deprecation_notice_date=_date(registry_raw, "timing_deprecation_notice_date"),
            timing_shutdown_date=_date(registry_raw, "timing_shutdown_date"),
            block_online_at_shutdown_date=_bool(registry_raw, "block_online_at_shutdown_date"),
            source_urls=_strings(registry_raw, "source_urls"),
        ),
        pricing=RawPricingSnapshot(
            checked_at=_date(pricing_raw, "checked_at"),
            currency=_text(pricing_raw, "currency"),
            text_per_minute=_decimal(pricing_raw, "text_per_minute"),
            timing_per_minute=_decimal(pricing_raw, "timing_per_minute"),
        ),
    )


def _mapping(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise TypeError(f"JSON 객체가 와야 한다: {type(raw).__name__}")
    return cast("dict[str, object]", raw)


def _mapping_of(raw: object, keys: tuple[str, ...], where: str) -> dict[str, object]:
    """알려진 필드만 받는 객체 — 모르는 키는 형태 오류로 끝난다(손실 없는 왕복)."""
    mapping = _mapping(raw)
    unknown = sorted(set(mapping) - set(keys))
    if unknown:
        raise TypeError(f"{where}에 알 수 없는 필드가 있다: {', '.join(unknown)}")
    return mapping


def _field(mapping: dict[str, object], key: str, kind: type[_T]) -> _T:
    if key not in mapping:
        raise KeyError(key)
    value = mapping[key]
    if not isinstance(value, kind):
        raise TypeError(f"{key}는 {kind.__name__}여야 한다: {type(value).__name__}")
    return value


def _text(mapping: dict[str, object], key: str) -> str:
    return _field(mapping, key, str)


def _bool(mapping: dict[str, object], key: str) -> bool:
    return _field(mapping, key, bool)


def _date(mapping: dict[str, object], key: str) -> date:
    return date.fromisoformat(_field(mapping, key, str))


def _decimal(mapping: dict[str, object], key: str) -> Decimal:
    text = _field(mapping, key, str)
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise TypeError(f"{key}는 십진수 문자열이어야 한다: {text!r}") from error


def _strings(mapping: dict[str, object], key: str) -> tuple[str, ...]:
    value = _field(mapping, key, list)
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{key}의 원소는 문자열이어야 한다: {type(item).__name__}")
    return tuple(str(item) for item in value)


RENDER_CONTAINERS = ("mp4",)
"""추천만 이어붙인 초안의 그릇 — H.264 + AAC를 담는 MP4 하나뿐이다(D100)."""
X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
)  # fmt: skip
"""libx264 프리셋 철자(형식의 사실이지 조정 값이 아니다)."""
X264_CRF_RANGE = (0, 51)
"""libx264 8비트 crf가 받는 범위(형식의 사실)."""
NICE_RANGE = (0, 20)
"""POSIX nice 값의 범위 — 미리보기 설정(`ReviewMediaSettings.process_niceness`)과 같은
검사(형식의 사실)."""


@dataclass(frozen=True)
class RenderSettings:
    """추천만 이어붙인 초안 영상 + SRT(W-44, D100)의 설정 — 순수 계획(`render_plan`)과 렌더러가
    읽는다.

    **모든 값이 외부 근거 없는 조정 대상이다.** 최소 조각 길이·합칠 틈은 사람이 보는 초안의 리듬을
    고른 값이고, 인코더·화질·소리는 Final Cut 없이 바로 보는 초안이라는 쓰임에서 고른 값이다.
    어느 지문·캐시 열쇠·승인·견적에도 없다 — 바꾸면 다음 내보내기만 달라진다.

    - `min_part_seconds`: 이보다 짧은 남길 조각은 빼고 `too_short`로 센다(조용히 사라지지 않는다).
      1초 아래 조각은 이어붙이면 번쩍임으로만 보인다.
    - `merge_gap_seconds`: 남길 조각 사이 틈이 이보다 짧고 그 틈이 **자동 노랑뿐**이면 틈째 잇는다
      (사람 표시·빨강·미분석은 잇지 않는다). 0이면 맞닿은 조각만 합친다 — 화면에 보인 그대로다.
    - `video_codecs`: 앞에서부터 있는 인코더를 쓴다 — 하드웨어 인코더가 먼저, libx264가 마지막
      (D100 — 하드웨어 인코더는 미리보기·최종 렌더에만, 분석 바이트에는 쓰지 않는다).
    - `crf`·`preset`: libx264일 때의 화질·속도. `pad_color`: 비율이 다른 원본을 시퀀스 크기에
      맞출 때 남는 칸의 색(ffmpeg 색 철자).
    - 소리는 AAC `audio_rate` Hz · `audio_channels`채널 · `audio_bitrate_kbps` kb/s.
    - 시퀀스 방향·레이트의 동점 규칙은 Final Cut 보내기와 **같은 설정**(`timeline`, D66·D71)을
      쓴다 — 베끼지 않는다.
    - 렌더러(W-44 §5b): 하드웨어 인코더는 crf가 없어 `hardware_bits_per_pixel` × 화소 × 레이트의
      비트레이트로 만든다(0.1이면 1080p30에서 약 6.2 Mb/s). `keyframe_seconds`마다 키프레임이고,
      `track_timescale_min`은 영상 트랙 눈금의 바닥이다(분모를 이 값 이상까지 두 배 — ffmpeg mov의
      기본 규칙과 같은 10000). 조각·이어붙이기 한 번의 한도는 `max(timeout_floor_seconds,
      timeout_media_ratio × 길이)`(D59·D62와 같은 꼴)이고, 렌더 ffmpeg는 nice `priority`로 돈다
      (미리보기와 같은 양보, D62).
    """

    min_part_seconds: Fraction = Fraction(1)
    merge_gap_seconds: Fraction = Fraction(0)
    container: str = "mp4"
    video_codecs: tuple[str, ...] = (
        "h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf", "libx264",
    )  # fmt: skip
    crf: int = 20
    preset: str = "medium"
    audio_codec: str = "aac"
    audio_rate: int = 48_000
    audio_channels: int = 2
    audio_bitrate_kbps: int = 192
    pad_color: str = "black"
    timeline: FinalCutTimelineSettings = FINALCUT_TIMELINE
    hardware_bits_per_pixel: Fraction = Fraction(1, 10)
    keyframe_seconds: Fraction = Fraction(2)
    track_timescale_min: int = 10_000
    timeout_floor_seconds: float = 1800.0
    timeout_media_ratio: float = 6.0
    priority: int = 10

    def __post_init__(self) -> None:
        problems = (
            _render_length_problems(self)
            + _render_codec_problems(self)
            + _render_engine_problems(self)
        )
        if problems:
            raise ValueError("렌더 설정이 잘못됐다 — " + " · ".join(problems))


def _render_length_problems(settings: RenderSettings) -> list[str]:
    problems = [
        f"{name}는 0 이상인 Fraction이어야 한다: {getattr(settings, name)!r}"
        for name in ("min_part_seconds", "merge_gap_seconds")
        if not isinstance(getattr(settings, name), Fraction) or getattr(settings, name) < 0
    ]
    problems += [
        f"{name}는 양의 정수여야 한다: {getattr(settings, name)!r}"
        for name in ("audio_rate", "audio_channels", "audio_bitrate_kbps")
        if not isinstance(getattr(settings, name), int) or getattr(settings, name) <= 0
    ]
    return problems


def _render_engine_problems(settings: RenderSettings) -> list[str]:
    problems = [
        f"{name}는 0보다 큰 Fraction이어야 한다: {getattr(settings, name)!r}"
        for name in ("hardware_bits_per_pixel", "keyframe_seconds")
        if not isinstance(getattr(settings, name), Fraction) or getattr(settings, name) <= 0
    ]
    problems += [
        f"{name}는 양수여야 한다: {getattr(settings, name)!r}"
        for name in ("track_timescale_min", "timeout_floor_seconds", "timeout_media_ratio")
        if isinstance(getattr(settings, name), bool)
        or not isinstance(getattr(settings, name), (int, float))
        or not getattr(settings, name) > 0
    ]
    priority = settings.priority
    low, high = NICE_RANGE
    if isinstance(priority, bool) or not isinstance(priority, int) or not low <= priority <= high:
        problems.append(f"priority는 {low}–{high} nice 값이어야 한다: {priority!r}")
    return problems


def _render_codec_problems(settings: RenderSettings) -> list[str]:
    problems: list[str] = []
    if settings.container not in RENDER_CONTAINERS:
        problems.append(
            f"container는 {RENDER_CONTAINERS} 가운데 하나여야 한다: {settings.container!r}"
        )
    codecs = settings.video_codecs
    if not codecs or any(not codec.strip() for codec in codecs) or len(set(codecs)) != len(codecs):
        problems.append(f"video_codecs는 비지 않고 겹치지 않는 인코더 이름이어야 한다: {codecs!r}")
    low, high = X264_CRF_RANGE
    if not low <= settings.crf <= high:
        problems.append(f"crf는 {low}–{high}여야 한다: {settings.crf}")
    if settings.preset not in X264_PRESETS:
        problems.append(f"preset은 libx264 프리셋이어야 한다: {settings.preset!r}")
    problems += [
        f"{name}가 비었다"
        for name in ("audio_codec", "pad_color")
        if not str(getattr(settings, name)).strip()
    ]
    return problems


RENDER_SETTINGS = RenderSettings()
"""지금 쓰는 초안 렌더 설정 — 계획(`render_plan.build_plan`)과 렌더러의 기본값이다."""


@dataclass(frozen=True, slots=True)
class TranslationSettings:
    """초안의 영어 자막(W-53, D105) — 원본 자막 줄을 Gemini로 영어로 옮기는 설정.

    `english_line_chars = 42`만 외부 근거가 있다(Netflix 영어 TTSG 한 줄 42자). 나머지는 **외부
    근거 없는 조정 대상**이다. 판(`prompt_revision`)·모델·묶음·맥락 줄은 번역 캐시 열쇠에
    실리고(바꾸면 다시 번역한다), 어느 분석 지문·결제 열쇠·등급·한국어 자막에도 없다. 줄 나눔은
    내보낼 때만 한다."""

    model: str = GEMINI_FLASH_ANALYSIS_PROFILE.model
    prompt_revision: str = "translation/v1"
    batch_lines: int = 150
    """한 요청에 옮길 줄 수 — 긴 영상은 이만큼씩 나눠 묻는다(응답 한도 안에 들게)."""
    context_lines: int = 5
    """묶음마다 앞에 붙이는 직전 줄 수 — 옮기지 않고 맥락으로만 읽힌다."""
    english_line_chars: int = 42
    max_output_tokens: int = 16000
    """응답 한도(생각 포함) — 150줄 영어 JSON(약 4천 토큰)에 생각 몫을 넉넉히."""
    thinking_level: str = "LOW"
    """번역은 판정이 아니라 생각을 적게 — 출력 한도 안의 생각 몫이 줄어 견적 상한도 같다."""
    quote_chars_per_token: int = 1
    """견적의 입력 토큰 추정 — 글자 하나 = 토큰 하나(맥락 평가와 같은 보수적인 쪽)."""

    def __post_init__(self) -> None:
        for name in (
            "batch_lines",
            "english_line_chars",
            "max_output_tokens",
            "quote_chars_per_token",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}는 1 이상의 정수여야 한다: {value!r}")
        if isinstance(self.context_lines, bool) or self.context_lines < 0:
            raise ValueError(f"context_lines는 0 이상의 정수여야 한다: {self.context_lines!r}")
        if self.thinking_level not in GEMINI_THINKING_LEVELS:
            raise ValueError(f"thinking_level이 허용 값이 아니다: {self.thinking_level!r}")
        if not self.prompt_revision.strip() or not self.model.strip():
            raise ValueError("번역 판·모델이 비었다")


TRANSLATION = TranslationSettings()
"""지금 쓰는 영어 자막 번역 설정(D105)."""


@dataclass(frozen=True)
class LocalMediaConcurrency:
    """로컬 미디어 작업(분석 프록시·원본 전체 미리보기 만들기)이 한 번에 돌리는 ffmpeg 수(D100 §2).

    **외부 근거 없는 조정 대상이다.** 작업 스레드는 개수 제한이 없어(`jobs.JobManager`) 폴더를
    불러오면 원본마다 프록시·미리보기가 한꺼번에 뜬다 — 4K 디코드가 CPU와 메모리를 다툰다. 한도는
    `max(floor, min(ceiling, CPU 수 // cpu_divisor))`다: 코어 절반을 넘지 않고(분석·화면이 쓸 몫을
    남긴다), 셋을 넘지 않는다(동시 분석 한도 D49와 같은 수). CPU 수를 모르면 `floor`다. 동시에 도는
    수만 정하므로 어느 지문·캐시 열쇠·승인·견적에도 없다 — 바뀌는 것은 걸리는 시간뿐이다.
    """

    floor: int = 1
    ceiling: int = 3
    cpu_divisor: int = 2

    def __post_init__(self) -> None:
        for name in ("floor", "ceiling", "cpu_divisor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}는 1 이상 정수여야 한다: {value!r}")
        if self.ceiling < self.floor:
            raise ValueError(f"ceiling({self.ceiling})이 floor({self.floor})보다 작다")

    def limit(self, cpu_count: int | None) -> int:
        """이 기계의 동시 한도 — `os.cpu_count()`의 값을 받는다(None이면 모른다)."""
        if cpu_count is None or cpu_count < 1:
            return self.floor
        return max(self.floor, min(self.ceiling, cpu_count // self.cpu_divisor))

    def encode_threads(self, cpu_count: int | None) -> int | None:
        """자리 하나의 CPU 인코드(libx264) 스레드 — `CPU 수 // 한도`, 적어도 1(W-45, D100 §6).

        libx264는 스스로 코어의 1.5배쯤 스레드를 띄운다. 자리 셋이 모두 차면 그 셋이 코어를 세 번
        나눠 갖는다 — 몫을 주면 로컬 미디어 인코드의 합이 코어 수를 넘지 않는다. 새 조정값이 아니라
        한도에서 나온 값이다. CPU 수를 모르면 None(ffmpeg가 정한다 — 예전 그대로).
        """
        if cpu_count is None or cpu_count < 1:
            return None
        return max(1, cpu_count // self.limit(cpu_count))


FOLDER_WORKSPACE_NAME_FORBIDDEN = ("/", "\\", ":")
"""작업공간 폴더 이름에 들 수 없는 글자 — 경로 구분자·드라이브 표기(형식의 사실이다)."""


@dataclass(frozen=True)
class FolderWorkspaceSettings:
    """촬영 영상 폴더 하나 = 작업공간 하나(D100 §2)의 자리와 4K 기준.

    - `workspace_dirname`: 작업공간은 폴더 **안**의 이 이름(`<폴더>/_vlogsrt/`)이다. 스캔은 정확히
      이 이름을 건너뛰고, 원본 루트 검사는 이 한 겹의 중첩만 허용한다. 점으로 시작하지 않는다 — 숨김
      경로 검사(`fcp._protected_target_path`)에 걸리지 않게. 외부 근거 없는 선택이다.
    - `sibling_suffix`: 폴더에 쓸 수 없으면(읽기 전용 SD 카드·권한) 형제 폴더 `<폴더><접미사>`로
      물러난다. FCP 관리 작업공간(`<라이브러리>-vlogsrt`)과 같은 꼴이다. 외부 근거 없는 선택이다.
    - `proxy_min_long_edge`: 화면 긴 변이 이 값 이상인 원본만 4K로 본다 — 불러올 때 25% 분석
      프록시(`analysis-proxy/v1`, D47)를 만들고 원본 전체 미리보기를 작게 만든다. 3840은 4K UHD의
      긴 변이다(사용자의 4K 정의). 그 아래 원본은 프레임을 원본에서 바로 디코드한다.
    - `local_media`: 로컬 미디어 작업의 동시 한도(`LocalMediaConcurrency`).

    어느 값도 분석 지문·결제 열쇠(`media_refs.fingerprint`)·승인·견적에 없다. 프록시를 쓰는지는
    프레임·장면 로컬 캐시 열쇠(`frames_fingerprint`)만 가른다(D47).
    """

    workspace_dirname: str = "_vlogsrt"
    sibling_suffix: str = "-vlogsrt"
    proxy_min_long_edge: int = 3840
    local_media: LocalMediaConcurrency = LocalMediaConcurrency()

    def __post_init__(self) -> None:
        problems = [
            f"{name}는 비지 않고 점으로 시작하지 않으며 경로 구분자가 없어야 한다: {value!r}"
            for name, value in (
                ("workspace_dirname", self.workspace_dirname),
                ("sibling_suffix", self.sibling_suffix),
            )
            if not value.strip()
            or value.startswith(".")
            or any(mark in value for mark in FOLDER_WORKSPACE_NAME_FORBIDDEN)
        ]
        edge = self.proxy_min_long_edge
        if isinstance(edge, bool) or not isinstance(edge, int) or edge < 1:
            problems.append(f"proxy_min_long_edge는 1 이상 정수여야 한다: {edge!r}")
        if problems:
            raise ValueError("폴더 작업공간 설정이 잘못됐다 — " + " · ".join(problems))

    def is_uhd(self, width: int | None, height: int | None) -> bool:
        """화면 긴 변이 4K 기준 이상인가 — 크기를 모르면 거짓이다(판정은 호출자가 정한다)."""
        if width is None or height is None:
            return False
        return max(width, height) >= self.proxy_min_long_edge


FOLDER_WORKSPACE = FolderWorkspaceSettings()
"""지금 쓰는 폴더 작업공간 설정 — 스캔·작업공간 자리·4K 프록시·미리보기·동시 한도가 읽는다."""
