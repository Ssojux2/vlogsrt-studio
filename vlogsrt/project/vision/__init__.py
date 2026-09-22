"""vlogsrt.project.vision — 프레임 샘플과 판정 정책 갈래 (P5-04·P5-05).

이 `__init__`은 아무것도 끌어들이지 않는다: import가 곧 extra 유무 검사가
되는 일은 없다(vad와 같은 선). 프레임 추출 subprocess는 레거시 공개 경계
`vlogsrt.preprocess`를 통하고, 비전 모델 **호출**도 이 디렉터리 밖 — 레거시
공개 경계 `vlogsrt.transcribe`의 `request_vision`(P5-05) — 에서 일어난다.
이 안의 `sampling`·`policy`는 조립·해석만 하고 스스로는 아무것도 부르지 않는다.
"""
