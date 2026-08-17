"""vlogsrt — 브이로그 자막 SRT 생성기.

골격(spine)은 whisper-1 세그먼트, 출력 텍스트는 gpt-transcribe 정본.
자세한 불변 규칙은 CLAUDE.md 참조.
"""

__version__ = "0.1.0"
TRACE_VERSION = "3.0"  # 2.0의 canonical·conf·whisper_text는 whisper 단독 전환(D19)으로 사라졌다
