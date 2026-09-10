"""vlogsrt.project.vision — 프레임 샘플 갈래 (P5-04).

이 `__init__`은 아무것도 끌어들이지 않는다: import가 곧 extra 유무 검사가
되는 일은 없다(vad와 같은 선). 프레임 추출 subprocess는 레거시 공개 경계
`vlogsrt.preprocess`를 통하고, 비전 모델 호출은 이 디렉터리 밖(P5-05)이다.
"""
