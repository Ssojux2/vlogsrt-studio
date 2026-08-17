---
description: 아키텍처 불변 규칙 8개를 검사한다
---

```bash
make invariant
pytest tests/test_invariants.py -q
```

실패 항목이 있으면 **코드를 고치기 전에 먼저 보고하라.**
불변 규칙을 어겨야 할 이유가 생겼다면 그것 자체가 설계 재검토 신호다.
규칙 전문은 `CLAUDE.md`의 "아키텍처 불변 규칙"에 있다.
