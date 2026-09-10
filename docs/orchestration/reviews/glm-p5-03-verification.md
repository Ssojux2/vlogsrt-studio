# P5-03 독립 검증 보고 (GLM 검증자, 후보 f2a798f)

VERDICT: **ACCEPT**

## 게이트 (전부 검증자가 직접 실행, 실제 출력)

| 명령 | 결과 |
|---|---|
| `python -m pytest tests/project/test_guard.py` | **27 passed** in 0.09s |
| `python -m pytest tests/project` | **1317 passed** in 35.49s |
| `python -m ruff check vlogsrt ui tests scripts` | All checks passed! |
| `python -m ruff format --check vlogsrt ui tests scripts` | 83 files already formatted |
| `python -m mypy` | Success: no issues found in 42 source files |
| `python scripts/check_invariants.py` | **[OK] 19줄 직접 센 값** (grep -c '^\[OK\]' = 19, FAIL/WARN = 0) |
| `python -m pytest` (전체) | **2085 passed** in 44.00s |
| 기준 재측정 (daadf29 임시 워크트리 collect-only) | **2058 collected** → 2058 + 27 = 2085 정확히 일치 |

## 계약 대조 (항목별)

| 계약 항목 | 판정 | 근거 |
|---|---|---|
| 시그니처·자료형 2개 (필드 명칭 포함) | 충족 | guard.py:75-128 — 계약 그대로, frozen+slots |
| 합집합 원천 전부 (세그먼트·소스 큐·경계 큐·VAD≥0.35·재전사·불확실 덮개) | 충족 | guard.py:218 (`seeds`), 232-237(초안+골격), 240-246(큐), 249-260(재전사 인자+트레스), 263-280(경계, 파일→국소 원점 1회 감산), 283-289(덮개 segment_count==0), 299-307(`>= cfg.vad_positive`) |
| 0.5초 outward 패딩·클램프 | 충족 | guard.py:150-153 (`cfg.guard_padding_seconds` 전 씨앗+VAD), 315-329 (floor/ceil 바깥 환산, [0,total] 클램프) |
| 250ms 미만 절대 버리지 않음 | 충족 | guard.py:45 `GUARD_MIN_RUN_SECONDS`는 이름뿐 — 어떤 함수도 비교하지 않음(전 usage grep 확인). 1표본(62.5µs) 런 생존을 적대 재현으로 확인 |
| 닿는/겹치는 범위 정확 병합 | 충족 | guard.py:332-343 — 퍼징 300세트 합집합 동치·좌표 불변 확인 |
| 빈 STT는 침묵 아님 | 충족 | guard.py:221-222, 283-289 — status=empty는 keep_all이 아니라 덮개 전체 불확실 보호 |
| VAD/의존/런타임/덮개 error·부재 → 전체 보호 + keep_all + review | 충족 | guard.py:485-496, 499-510 — trace.error / vad.error / 창 없는 VAD 성공 / 덮개 없음 전부 |
| VAD 양성 무글자 → 보호 + vad_without_text/review | 충족 | guard.py:155-162 — 나열은 여유 없는 런 그 자체, 증거(패딩 0)와의 차집합 |
| 투영 연결 검증 4종 (source_id·total_samples==sample_count·양수 sample_rate·슬라이스 source_id) | 충족 | guard.py:460-482 — 실제 필드명 사용, bool·비정수 거부 포함 |
| 원점 정확히 한 번 (양·음) | 충족 | guard.py:386-387 `Fraction(샘플, 주파수) + 원점` — 3/7과 −1/16을 검증자가 독립 유도해 대조 일치, 이중 가산시 밀림 확인 |
| interval_outward 동치 공식·앵커 오용 회귀 | 충족 | guard.py:56-61, 391 — anchor=0 파일 끝점 ≡ asset_start 닻 공식; 300초 에셋 기원에서 (150,180), 그릇된 공식은 −8850 |
| 불가청 인스턴스 잔류·제거 권한 없음 | 충족 | guard.py:377-382 — 슬라이스 전체 보호, review 남김 |
| 선언 무음·pre-PCM 부재 = 호출 자체가 없음 | 충족 | 모듈은 PcmReference 없이는 호출 불가(인자 타입+연결 검증), test_guard.py:654-698이 타입 있는 부재 기록과 함께 시연 |
| 임계값이 함수 몸통 리터럴 아님 (규칙 8) | 충족 | config.py:81 `vad_positive=0.35`, config.py:82 `guard_padding_seconds=Fraction(1,2)`, guard.py:45 모듈 상수. guard.py 함수 몸통에 0.35/0.5/0.25 리터럴 없음 |

## 적대적 재현 (검증자 자체 스크립트, scratchpad/adversarial_p5_03.py)

- **보호 이탈 사냥**: 무작위 씨앗 200세트(세그먼트·큐·VAD·음수/초과 좌표 포함)에서 가드 밖으로 떨어진 보호 표본 **0**.
- **임계 경계**: 0.3499999 제외 / 정확히 0.35 포함 확인.
- **극단 런**: 1표본 VAD 런 보호됨. 길이 0·뒤집힌 세그먼트도 위치 보호.
- **원점**: 양(3/7)·음(−1/16) 모두 검증자 독립 유도와 정확히 일치 — 한 번 더함, 두 번/영 회귀는 다른 값.
- **앵커**: 동치 공식 확인 + 그릇된 공식 −8850 재현.
- **위조 연결**: source_id/total_samples/sample_rate 0 전부 ValueError.
- **실패 닫힘·빈 STT·VAD만 있는 말·남의 소스 경계 큐**: 전부 계약대로.
- **산술 퍼징**: `_merge`·`_difference` 각 300세트 — 무작위 합집합/차집합과 표본 단위 동치.
- **정상 경로 도달**: 성공 가드→인스턴스 투영·반복 슬라이스·병합 슬라이스·불가청/가청 대비 전부 정상 동작 (구현 테스트 27 + 검증자 스크립트 G1).
- 초기 4건 FAIL은 전부 검증자 하네스의 산수 오류(클램프 미반영·기본 큐 오염·range 버그) — 수정 후 전부 PASS, 구현 결함 아님.

## 구현자 수치 재측정

| 주장 | 재측정 | 판정 |
|---|---|---|
| diff --stat 두 파일 698+529 | tests/project/test_guard.py 698, vlogsrt/project/vad/guard.py 529 | 일치 |
| test_guard.py 27 passed | 27 passed | 일치 |
| tests/project 1317 passed | 1317 passed | 일치 |
| ruff clean 83 files / mypy 42 files | 83 files formatted / 42 source files | 일치 |
| 불변식 19/19 [OK] | [OK] 줄 직접 세어 19, FAIL/WARN 0 | 일치 |
| 전체 2085 = 2058 + 27 | 2085 passed; daadf29 collect 2058 | 일치 |
| 네트워크 0 | 네트워크/프로세스 import 없음, 핸 메이드 기록만 사용 | 일치 |

## 범위

- `git diff --stat daadf29..HEAD`: 허용 파일 2개만. `vlogsrt/project/vad/__init__.py` 무수정 — 그 패키지의 관례가 "아무것도 끌어들이지 않는다"이므로 수출선 불추가가 관례 준수.
- 작업 나무 clean, 무단.untracked 파일 없음, `.env.example` 무변경, `tests/fixtures/api/`·`golden/` 무변경.

## 발견

- **정보(행동 불필요)**: 같은 내용 자원(같은 source_id)의 슬라이스는 모듈 수준에서 건강한 참조와 구별되지 않는다 — `PcmReference`에 자원 신원이 없어서다. 브리프가 정한 검사 목록 4개에 포함되지 않아 계약 위반이 아니며, "대여 금지"는 호출자 규율 + P4 잠금/타입 부재 기록이 막는 자리다. P5-05 통합자가 부재 자원의 슬라이스를 건강한 참조로 넘기지 않도록 하는 유일한 주의점.
- **정보**: `_boundary_spans`는 성공 경계 큐뿐 아니라 미해결 원문 큐도 합집합에 넣는다(guard.py:274-279) — 브리트 항목의 엄격한 상위집합이고 방향이 더 보호 쪽이며, 미해결 원문은 어차피 소스 큐가 덮는다.
- **정보**: 창이 0개인 VAD **성공** 결과도 keep_all로 닫는다(guard.py:492-493) — "missing"의 보수적 읽기.
- **없음(결함)**: 보호 범위 제거 경로(hard-stop 조건)는 발견되지 않았다. 이 모듈은 보호만 더한다.
- failing-first 증거는 단일 커밋(f2a798f에 두 파일 동반)이라 이력에서 독립 검증 불가 — 대신 테스트가 하중을 받치는 것을 적대 재현으로 대리 증명.
