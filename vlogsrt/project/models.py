"""FCPXML **전송 모델 전용** 네임스페이스.

이 자리는 P1의 검토된 FCPXML 파서 모델을 위한 것이다 — 계약(§3)이 말하는
불변 전송 구조만 여기 산다. 분석 모델은 P5가 별도의 `analysis_models.py`에서
소유한다. P0에는 아무 데이터클래스도 만들지 않는다: 투기적 필드를 미리 박는
것이 그 자체로 계약 위반이다. 이후 여기 들어오는 모든 데이터클래스는
`frozen=True`여야 한다 — `scripts/check_invariants.py`의 파생 frozen 검사가
가변 선언을 거부한다(docs/PROJECT_CONTRACTS.md §8).
"""
