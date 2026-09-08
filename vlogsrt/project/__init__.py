"""vlogsrt.project — Final Cut 파생 계층.

원본 엔진(`vlogsrt/*.py`)과 나란히 살지만 **별개의 뿌리**를 가진다: 이 계층의 설정은
`vlogsrt.project.toml`이고 원본 `vlogsrt.toml`과는 서로를 모른다. import 방향은
project → 레거시 공개 경계뿐이고 그 반대는 금지다(docs/PROJECT_CONTRACTS.md §1).
이 `__init__`은 아무것도 끌어들이지 않는다 — import는 부작용이 없어야 한다.
"""
