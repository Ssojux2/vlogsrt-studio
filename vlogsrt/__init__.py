"""vlogsrt — 브이로그 자막 SRT 생성기.

**whisper-1이 자막의 시각과 세그먼트 경계를 만들고, 시각은 원본 오디오 타임라인 그대로
들어간다.** 글자는 gpt-transcribe가 세그먼트마다 교정한다(D37). 이 패키지는 그것을 읽기
좋은 SRT로 조판할 뿐이다.
자세한 불변 규칙은 CLAUDE.md 참조.
"""

__version__ = "0.1.0"
# 글자 교정 매핑이 기록된다 — 없으면 `redo`가 gpt 글자를 whisper로 되돌린다(D37)
TRACE_VERSION = "10.0"
