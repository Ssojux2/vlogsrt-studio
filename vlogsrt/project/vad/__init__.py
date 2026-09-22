"""vlogsrt.project.vad — Silero VAD 실행 부품 (P5-01).

numpy·onnxruntime(선택 extra `[project]`)를 끌어올 수 있는 유일한 곳이다 —
`scripts/check_invariants.py`가 이 디렉터리 아래로 좁혀 놨다. 이 `__init__`은
아무것도 끌어들이지 않는다: 모듈 import가 곧 extra 유무 검사가 되어, extra가
없는 환경에서 발견 계층의 진단이 죽는 일을 막는다.
"""
