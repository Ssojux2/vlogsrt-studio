---
name: "source-command-eval"
description: "골든셋 전체를 재고 대시보드를 갱신한다"
---

# source-command-eval

Use this skill when the user asks to run the migrated source command `eval`.

## Command Template

```bash
make eval
```

`eval/<변형>/<회차>.srt`를 `golden/<회차>.srt`와 비교해 `docs/DASHBOARD.md`를 갱신한다.

보고할 것:
- 직전 행 대비 CER / 큐 시작 MAE / 큐 경계 F1 변화
- 관문 조건 충족 여부 (관문1 F1 > 0.80, 관문2 CER 절반 + 타이밍 불변)
- 나빠진 지표가 있으면 **원인 가설과 함께** 보고하라. 임계값을 돌려 가리지 마라.
