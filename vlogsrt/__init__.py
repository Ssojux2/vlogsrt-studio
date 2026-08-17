"""vlogsrt — 브이로그 자막 SRT 생성기.

모델 하나가 자막의 시각과 글자를 모두 만들고, 이 패키지는 그것을 SRT로 조판한다.
자세한 불변 규칙은 CLAUDE.md 참조.
"""

__version__ = "0.1.0"
TRACE_VERSION = "4.0"  # 3.0의 words·silences는 단일 모델 전환(D21)으로 사라졌다
