# Q3 — 글자는 gpt-transcribe, 시각은 whisper-1 (하이브리드) · 규칙 1·5 개정

> **설계 브리프 v1 — 방향 검토용.** 구현 착수 전 (A) Opus·(B) GLM 두 갈래 검토를 받는다.
> 사용자 결정(2026-09-16): 자막 모델을 `gpt-transcribe`로 진행. 단독으로는 시각이 없으므로
> **하이브리드**를 택했다.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`. 기준은 PROTO-5·6 머지 뒤(지금 `8ca214d`, pytest 2488).
**툴체인 shim 사망**: git은 `/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은
`.venv/bin/python`, `make` 금지. `xmllint` 정상.

---

## 1. 왜 하이브리드인가 — 이미 두 번 무너진 길을 피한다

- `gpt-transcribe`는 **타임스탬프를 주지 않는다.** 녹화 픽스처
  `tests/fixtures/api/fr_day_1_2_fhd.gpt.json`의 응답 필드는
  `['languages', 'logprobs', 'text', 'usage']`뿐이다(D24 근거와 동일).
- **D21~D23**: diarize 모델 단독 → 걷어냄. **D24**: 시각을 무음 검출로 재고 글자를
  **길이 비례로 배분** → 초반·마지막 3~6분 소실, 타이밍 밀림 → **D25에서 whisper-1 복귀.**
- 그러므로 이 설계의 절대선:
  - **시각·세그먼트 경계·큐 개수는 whisper-1 `segments[]`에서만 온다.** 바뀌는 것은 글자뿐.
  - **글자를 시간 비례로 배분하지 않는다**(D24의 실패 모양). 글자 대응은 **문자 정렬**로만.

## 2. 규칙 개정안 (D37 초안)

- **규칙 1 개정**: "시각은 whisper `segments[]`에서만 온다. **글자는 whisper, 또는 같은
  조각의 `gpt-transcribe` 응답을 whisper 글자에 문자 정렬한 결과**에서 온다. 시각을
  유도·배분·추정하는 코드는 여전히 금지."
- **규칙 5 개정**: 검증(`verify.py`)은 여전히 출력을 못 바꾼다. **글자 교체는 검증이 아니라
  별도 단계 `retext.py`**이고, 그 모듈만 큐 글자를 바꿀 권한을 갖는다. `retext`는 시각·큐
  개수·`seg_ids`를 못 바꾼다.
- **규칙 2 유지**: `spine[]`은 whisper 글자 그대로 불변 — **증거로 남는다.** 교체는 큐를
  만드는 순간(`spine_to_cues`)에 적용된다.

## 3. 파이프라인 자리

```
draft → repair.settle → spine(whisper, 불변)
      → [NEW] gpt-transcribe 조각 응답 확보 (같은 조각 경계)
      → [NEW] retext: spine 세그먼트별 교체 글자 계산 (순수 함수)
      → spine_to_cues(글자 = 교체 글자, 시각·seg_ids = spine) → polish → refine → SRT
      → verify (이미 받은 gpt 응답 재사용 — 두 번 호출하지 않는다)
```

- 오늘 검증은 **SRT 저장 뒤에** gpt-transcribe를 부른다(`pipeline.run`). 하이브리드는 그
  호출을 **SRT 저장 앞**으로 옮기고 결과를 검증이 재사용한다 → **추가 API 비용 없음**
  (검증 기본 켜짐 기준).
- **verify가 대조할 글자**: 교체 후 큐를 gpt 응답과 비교하면 자명하게 일치한다. 검증은
  **whisper 기준 큐**(`spine_to_cues(spine)`의 원래 글자)와 비교해 "두 모델이 얼마나 달랐나"를
  계속 보고한다. — **검토 요청 지점.**

## 4. 정렬 알고리즘 (순수 함수, `vlogsrt/retext.py`)

조각 하나마다:

1. 그 조각 시간 범위의 spine 세그먼트를 모은다(`ChunkResponse.offset` 경계).
2. whisper 글자를 이어 붙인 문자열 `W`와 각 세그먼트의 문자 범위를 기록한다.
   gpt 글자 `G`는 그 조각 응답의 `text`.
3. `difflib.SequenceMatcher(None, W_norm, G_norm, autojunk=False)`의 opcode로 `W`의 각 문자
   위치 → `G`의 위치 대응을 만든다(정규화: 공백 정리만; 대조용 정규화와 출력 글자는 분리).
4. 세그먼트 `[a, b)` → `G`의 대응 구간을 잘라 교체 글자로 삼는다. **삽입**(whisper에 없고
   gpt에만 있는 글자)은 **앞 세그먼트에 붙인다**(조각 첫머리면 뒤 세그먼트). 어절을 쪼개지
   않도록 경계를 가까운 공백으로 민다.
5. 폴백:
   - 조각 유사도(`ratio`)가 `retext_min_similarity` 미만 → **그 조각 전체 whisper 글자 유지**, WARN.
   - 교체 글자가 비는데 whisper 글자가 있으면 → **whisper 글자 유지**(규칙 9: 큐를 비우지 않는다).
   - gpt 응답이 없거나 호출 실패 → 전체 whisper 글자 유지, WARN. **SRT를 막지 않는다.**
6. **되풀이 재전사가 손댄 구간(`Repair.span`)의 세그먼트는 교체하지 않는다.** 접힌(collapse)
   구간은 whisper 쪽이 첫 장만 남았는데 gpt 글자는 긴 실제 발화일 수 있어, 정렬하면 한 큐에
   수십 초 분량 글자가 몰린다. — **검토 요청 지점**(대안: 교체하되 길이 상한).

출력: `seg_id → 교체 글자` 매핑 + 조각별 기록(유사도, 적용/폴백 사유) → `trace.json`의
`retext[]`. 교체된 큐에는 `flags`에 `retext`.

## 5. 설정 (규칙 8)

- `retext_enabled: bool = True`, CLI `--no-retext`.
- `retext_min_similarity: float` — **외부 근거 없음.** `cut_*`·`verify_*`·`repair_*`·`polish_*`와
  같은 조정 대상 계열로 등록하고 근거를 `DECISIONS.md` D37에 남긴다. 초기값은
  `fr_day_1_2_fhd` 픽스처의 조각별 실측 분포를 보고 정한다(이 브리프는 값을 정하지 않는다).
- 모델 이름은 기존 `verify_model`(`gpt-transcribe`)을 재사용할지, `retext_model`을 새로 둘지 —
  **검토 요청 지점.**

## 6. 불변식 변경

- `retext.py`는 순수 함수: `transcribe`·`preprocess`·ffmpeg·API 를 모른다.
- `retext.py`는 `refine`·`output`·`layout`·`verify`·`repair`를 import하지 않는다.
- `verify.py`는 여전히 `refine`·`output`을 import하지 않고 출력을 못 바꾼다(기존 검사 유지).
- `difflib.SequenceMatcher`는 `autojunk=False`(부수 규칙).
- 검증 on/off에 SRT 바이트 동일 — **retext on 상태에서도** 유지되어야 한다.

## 7. 단계 분할

- **Q3-1 (엔진)**: `retext.py` + `pipeline.py` 배선 + `config.py` + `cli.py --no-retext` +
  불변식 + D37. 파생 계층 무변경.
- **Q3-2 (파생 계층)**: `project/speech.py`의 소스 큐, `project/boundary.py`의 경계 재전사
  큐에 같은 교체 적용. **`analysis_key`/설정 지문에 retext 설정이 들어가야** 옛 완성물이 재사용되지
  않는다. 경계 재전사 구간은 gpt 호출이 **추가 비용**이 되므로 교체할지 여부 — **검토 요청 지점.**

## 8. 테스트 (Q3-1)

- 오프라인 `fr_day_1_2_fhd` 픽스처로: 큐 개수·시각·`seg_ids`가 retext on/off에 **완전히 같다**.
  글자만 다르다.
- 유사도 미만 조각 → 그 조각 whisper 글자 그대로 + WARN + trace 기록.
- 교체 글자 빈 세그먼트 → whisper 글자 유지.
- `Repair.span` 안 세그먼트 → 교체 안 됨.
- 검증 on/off SRT 바이트 동일(retext on).
- gpt 응답 없음/예외 → SRT는 whisper 글자로 정상 저장.
- 합성 단위 테스트: 삽입·삭제·치환 opcode별 세그먼트 배정, 어절 경계.

## 9. 방향 검토자에게 — 특히 봐 달라

1. §4의 정렬이 **D24(길이 비례 배분)의 실패를 다른 모양으로 되풀이하지 않는가.** 삽입 몰림,
   whisper 환각 구간(되풀이 아닌 환각)에서 gpt 글자가 한 큐에 몰리는 경우.
2. §4-6 되풀이 구간 제외가 옳은가, 아니면 gpt 글자가 **접힌 구간의 잃어버린 발화를 되살릴
   유일한 기회**인가(규칙 9와의 관계).
3. §3 verify가 whisper 기준 큐와 비교하는 설계의 의미.
4. `polish`(문장부호만) 이전에 gpt 글자가 들어오면 규칙 5의 "polish는 한글·영문·숫자를 못
   지운다"가 여전히 의미 있는가.
5. Q3-2에서 소스 분석 키·완성물 재사용이 조용히 옛 whisper 글자를 내보낼 경로.
6. 픽스처 재녹화가 필요한가(사람 전용·유료) — 필요하면 무엇을.
