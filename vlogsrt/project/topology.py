"""실제 원본 미디어의 오디오 스트림 위상 검사(S2A-2 마지막 단위).

한 스트림만 분석하는 오늘의 파이프라인에서 오디오 스트림이 둘 이상인 원본은
**지원하지 않는 입력**이다: 하나를 골라 듣고 남은 트랙을 무음으로 두면 XML
선언과 실제가 어긋난 채 유료 호출이 열린다. 그래서 이 관문은:

- **실제 파일**을 ffprobe로 본다 — XML의 `hasAudio` 선언은 증거가 아니다
  (`hasAudio=0`으로 선언된 파일에 숨은 트랙이 있어도 잡는다).
- **스트림 수**로만 거절한다 — 스테레오·멀티채널 채널은 한 스트림이므로
  지원된다. 0스트림(선언된 무음)과 1스트림은 그대로 지나간다.
- 확인 **실패**는 거절과 다른 실패다 — 도구 부재·마감 초과·비0 종료·깨진
  출력은 실행 가능한 오류로 올라가고, 절대 '스트림 0개·무음'으로 번역하지
  않는다(`preprocess._run_probe`의 계약).
- 유료 추정(`inspect`)과 유료 실행(`analyze --online`)이 같은 관문을 지난다 —
  저장된 추정이 있어도 실행 때 다시 잰다. Swift 준비 검사의 캐시와 무관하게
  파이썬 실제 경로가 스스로 이 관문을 지킨다.

`has_audio` 선언 계약(media.py)은 그대로 산다 — 이 모듈은 선언을 고치지
않고, 그 아래의 실제를 본다.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from vlogsrt.preprocess import probe_audio_stream_count
from vlogsrt.project.media import ResolvedMedia

_SUPPORTED_AUDIO_STREAMS = 1
"""지원하는 오디오 스트림 수의 상한 — 조정 대상이 아니라 지원 범위 자체다."""

__all__ = ["UnsupportedAudioTopology", "require_supported_audio_topology"]


class UnsupportedAudioTopology(RuntimeError):
    """오디오 스트림이 둘 이상인 원본 — 한 스트림만 듣고 남은 트랙을 무음으로
    둘 수 없으므로 지원하지 않는다. 교정 글은 믹스다운을 가리킨다."""


def _message(source: Path, count: int) -> str:
    return (
        f"오디오 스트림이 {count}개다: {source} — 지원하지 않는 구성이다."
        " 오디오를 하나의 스트림으로 믹스다운한 원본으로 다시 내보내세요."
        " 한 스트림만 분석하면 남은 트랙을 무음으로 가정하게 된다."
    )


def require_supported_audio_topology(media: Sequence[ResolvedMedia]) -> None:
    """해석이 끝난 미디어 전체의 실제 오디오 스트림 수를 확인한다.

    같은 원본을 가리키는 리소스 여럿은 한 번만 확인한다(경로로 묶는다 —
    내용 해시는 이미 해석이 끝난 뒤다). 지원 범위 안이면 돌아오는 값은
    없고, 벗어나면 `UnsupportedAudioTopology`, 확인 자체가 실패하면 그
    오류가 그대로 올라간다(절대 부재로 번역하지 않는다).
    """
    seen: set[Path] = set()
    for row in media:
        original = row.original
        if original in seen:
            continue
        seen.add(original)
        count = probe_audio_stream_count(original)
        if count > _SUPPORTED_AUDIO_STREAMS:
            raise UnsupportedAudioTopology(_message(original, count))
