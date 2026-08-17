"""웹 UI. `vlogsrt/` 패키지 **밖**에 있다.

핵심 파이프라인의 의존성은 `openai`, `tomli-w`, `rich` 셋뿐이라는 불변 규칙 7을
지키려고 분리했다. 여기서는 gradio를 써도 되지만, `vlogsrt/`는 절대 안 된다.
"""
