# 순서 역전(컷 → 자막) + Final Cut 플러그인 — 확정 계획

Date: 2026-09-13. 기준 제품 HEAD: `97e1063` (P6-03). 통합 브랜치 `feat/final-cut-project`.

이 문서는 `docs/plans/2026-09-08-final-cut-project.md`를 **대체하지 않고 개정**한다.
바뀌는 것은 (1) 컷과 자막의 순서, (2) 납품 형태, (3) 그 둘이 강제하는 계약이다.
원본 명세의 나머지 — 지원 부분집합, 시계·신원 규칙, 조각 한도, 인수 기준 — 는 그대로다.

방향 검토: `docs/orchestration/reviews/astra-replan-cut-first.md` (gpt-6-astra, UNSOUND
판정 → 12개 지적 전부 반영). 뒤집힌 Opus 주장 12건은 §7.

---

> v1은 `gpt-6-astra` 검토에서 **UNSOUND** 판정(차단 6 · 높음 5 · 중간 1). 12개 지적을
> 전부 반영했고, 뒤집힌 주장은 §7에 기록으로 남긴다. 사용자 판단 4건은 §0에 반영 완료.

## 0. 확정된 결정

| 항목 | 결정 |
|---|---|
| 안전 수준 | **A — 후보 검증 전사.** 제거 후보 블록만 전사해 글자가 나오면 제거를 취소한다 |
| 자막 형태 | **사이드카 SRT만.** FCPXML `<caption>` 임베딩은 안 한다(기준 명세 비목표 유지) |
| Gradio UI | **전체 제거.** D11/D14를 되돌린다. P7-05는 폐기 |
| 플러그인 착수 | **X-01 스파이크만 선행**(사람), 나머지 X는 엔진(R+P7) 뒤 |
| 비전 판정 모델 | **Google `gemini-3.8-flash`** (OpenAI 호환 엔드포인트). 2026-09-13 완료 — D34, 커밋 `b1fbc45` |

---

## Context

### 왜 바꾸는가

현재 파생 계층(`vlogsrt/project/`)은 **자막 → 컷편집**이다. 전체 소스를 전사하고(P3),
경계 큐를 투영하고(P4), 그 큐들을 보호 재료로 가드를 만들고(P5), 컷을 계산한다(P6).
사용자가 원하는 것은 **컷편집 → 자막**이고, 납품 형태는 CLI가 아니라 **Final Cut 안에서
도는 플러그인**이다 — 프로젝트나 클립 하나를 놓고 "컷편집"·"자막 생성" 버튼을 누르는 것.

저장소 전체에 `plugin`·`플러그인`·`FxPlug`·`workflow extension` 문자열이 **한 번도
나오지 않는다.** 지금 못 박힌 형태는 "사람이 XML을 왕복시키고, CLI 먼저 얇은 Gradio
패널 나중"(`docs/plans/2026-09-08-final-cut-project.md:58`)이다.

### 무엇을 얻는가 (정직한 판)

1. **작업 순서가 사람의 편집 순서와 같아진다.** 컷을 확정한 뒤 자막을 만든다.
2. **자막이 최종 편집본을 설명한다** — 사용자가 손으로 더 다듬은 것까지 포함해서.
3. **큐가 컷을 가로지르는 문제가 구조적으로 사라진다** — 단 §3의 범위별 독립 실행을
   제대로 했을 때만. 순서만 바꾸면 오히려 더 나빠진다(§3-A).
4. 두 단계가 분리되어 버튼 둘이 각각 독립으로 의미를 갖는다.

**비용 절감은 이득이 아니다.** v1은 "30% 자르면 STT도 30% 준다"고 썼는데 **틀렸다** —
엔진은 이미 유일 소스마다 한 번만 전사한다(`speech.py:722`). 같은 소스를 두 번 쓰다가
인스턴스 하나를 지우면 타임라인은 줄어도 전사량은 그대로다. 수준 A의 후보 검증 전사까지
붙으면 총량은 오늘과 대체로 비긴다. **우리는 절감이 아니라 순서와 안전을 산다.**

---

## 1. 납품 형태 — Workflow Extension (사실 정정 포함)

| 형태 | 실제로 되는 것 | 이 제품에 |
|---|---|---|
| FxPlug 4 | 프레임 효과. `FxCommandAPI`로 **playhead 이동·undo/redo는 된다** | ✗ 임의 컷 재구성 API는 없다 |
| Motion 템플릿 | 타이틀·효과 | ✗ 기준 명세가 이미 비목표 |
| **Workflow Extension** | FCP 창 안의 macOS app extension | **✓ 패널의 올바른 형태** |

**정정 1 — `FCPXTimeline`으로 타임라인을 읽을 수 없다.** 공개 API는 active sequence·시간
범위·playhead·observer뿐이다. **클립 구조 전체나 FCPXML 취득 API는 문서에 없다.**
v1이 "호스트에서 직접 읽는 최적화"로 예정한 것은 근거가 없다. 삭제.

**정정 2 — 입력은 수동 XML export만이 아니다.** Apple에 *Supporting Drag and Drop to
Receive Final Cut Pro Data* 문서가 있다 — **브라우저/사이드바에서 클립·프로젝트를 패널로
직접 끌어다 놓는 공식 경로**다. 사용자가 말한 UX에 훨씬 가깝다.

**정정 3 — 출력도 드래그만이 아니다.** *Sending Data Programmatically to Final Cut Pro*
(Apple events Open Document)로 FCPXML import를 요청할 수 있다.

두 문서 모두 제목은 확인했으나 본문 자동 열람이 막혀 **정확한 UTI·payload·코드는 X-01
스파이크에서 확인해야 한다.** 그 전까지 계약은 "수동 export 드롭"이고, 위 둘은 스파이크
결과에 따라 채택한다.

변하지 않는 것: **서드파티는 열려 있는 타임라인을 제자리에서 고칠 수 없다.** 결과는
언제나 새 FCPXML이다. 엔진이 이미 FCPXML in / FCPXML out이라 **플러그인은 껍데기지
재작성이 아니다.** FCP 라이브러리 DB는 계속 안 건드린다.

**정정 4 — 입력 범위가 요구를 아직 못 맞춘다.** 현재 파서(`fcpxml.py:132`)는
`library/event/project/sequence/spine`을 각각 요구한다. **브라우저의 단일 클립 XML은
그대로 못 받는다.** 연결 오디오·타이틀·transition·compound·retime도 거부한다.
단일 클립을 명시적 프로젝트로 감싸는 **입력 어댑터**(R-08)가 필요하고, "사람이 고친 현재
타임라인"도 지원 부분집합 안에서만이라는 것을 UI가 말해야 한다.

---

## 1.5 컷편집 방법론 (현행 — P5/P6에 이미 구현됨)

> 재계획이 순서만 다루고 알고리즘 자체를 안 적어서 추가한다. 아래는 **이미 머지된**
> 동작이고, 값은 전부 `vlogsrt/project/config.py:84-96`에서 온다.

**한 문장: 6초 이상 아무도 말하지 않는 구간에서 3초만 남기고 나머지를 리플 제거한다 —
단 비전 모델이 "반복적인 평범한 B-roll"이라고 확인했을 때만.**

| 단계 | 하는 일 | 값 / 근거 |
|---|---|---|
| 1. 보호 구간 | Silero VAD가 16k PCM을 512샘플씩 훑어 발화를 찾고 양옆에 패딩 | `vad_positive = 0.35`, `guard_padding_seconds = 0.5` |
| 2. 적격 런 | 인스턴스마다 보호의 여집합. **6초 미만은 후보가 아니다.** 잠긴·부재·`keep_all`·불가청 인스턴스는 블록을 안 낸다 | `block_min_seconds = 6` · `edit.py:158` |
| 3. 블록 분할 | 적격 런을 10초 이하로 프레임 정렬해 자른다. 짧은 잔여는 명시적 유지 | `block_max_seconds = 10` |
| 4. 후보 3개 | 블록마다 앞/중앙/뒤, 각 정확히 3초. 중앙은 half-up. 같으면 하나만. **전체 삭제 후보는 존재할 수 없다** | `keep_seconds = 3` · `edit.py:548` |
| 5. 프레임 표본 | 블록 전체 1fps + 블록 양끝 + 후보 3개 중심. 프록시 우선 | `vision_max_images = 16`, `vision_long_edge = 768` |
| 6. 비전 판정 | 후보 ID 하나 또는 `keep_all`/`uncertain`. **모델에게 XML·시각·경로·음소거 권한을 주지 않는다** | `vision_apply_confidence = 0.8` |
| 7. 적용 | 블록당 **최대 하나**. 3초 남기고 리플 제거. 소스 오디오·순서·기존 갭 보존. 자른 뒤 가드에 **다시 대조** | |

실측: 합성 픽스처 **500프레임/20초 → 325프레임/13초**, 제거 1개·정확히 175프레임.

**하지 않는 것**: 말버릇 점프컷, 대사 줄이기, 순서 바꾸기, NG 판별. 무음 다듬기 +
시각적 확인이 전부다 — 기준 명세의 "conservative roughcut"이 이 뜻이다.

**재계획이 바꾸는 자리는 둘뿐**: 1단계의 **증거**(전사 다섯 갈래가 빠지고 VAD만 남는다)와,
4·6단계 **사이에 끼는 R-05 후보 검증 전사**. 2·3·4·7단계는 한 줄도 바뀌지 않는다.

---

## 2. 안전성 — 이 재계획의 핵심 위험

`vad/guard.py:160`의 `build_source_guard`는 보호를 여섯 갈래에서 합집합하고 **다섯이
전사에서 온다.** 컷을 먼저 하면 VAD만 남는다.

### A. v1의 안전 논증은 틀렸다 — 삭제

v1은 "두 단계 사이의 사람 검토가 전사 보호선보다 강하다"고 썼다. **아니다.** 편집본
재생만으로는 **이미 사라진 대사를 발견할 수 없다.** 더 나쁜 것은 순서다 — 오검출된
발화를 **먼저 영상에서 지우면** 그 뒤 전사는 남은 것만 듣기 때문에 손실을 발견할
기회조차 사라진다.

`InstanceGuard`의 비대칭을 정확히 봐야 한다(`edit.py:371`):

| 상태 | 제거 권한 |
|---|---|
| `status != "success"` (빈 `protected_frames`여도) | **없음** ✓ |
| `status == "success"` + `protected == []` | **있음** ← `protected=[]` 계열, 5회 재발 |

`AudioOnlyEvidence(reason=...)`는 **의도적 생략의 표시일 뿐 음성 부재의 증명이 아니다.**
그것만으로 제거 권한을 주면 같은 결함의 여섯 번째 재발이다.

### B. 채택한 안전 수준 — A: 후보 검증 전사

```
VAD → 후보 블록(complement(guard) ∩ ≥6초 ∩ 적격)
    → ★ 후보 블록만 전사해 확인 ★
        · 글자가 나온다        → 그 블록 보호, 제거 취소
        · 글자가 없다          → vision 판정으로 넘긴다
        · 빈 응답 / 오류       → 보호 (무음의 증거가 아니다 — 기준 명세 line 44, D23)
    → 남은 후보만 vision → EditPlan
```

**불변 방향: 전사는 보호만 더할 수 있고 제거 권한을 줄 수 없다.** 이 한 줄이 fail-safe를
구조로 만든다. 비용은 *제거 후보* 길이에만 비례하고, VAD가 이미 무음이라 본 구간이라 작다.

Astra가 제시한 대안 순서 — *"발화 보호용 분석 → 컷 제안·검토 → 최종 자막 생성"* — 이
곧 수준 A다. 사용자의 "자막을 나중에"라는 요구는 **산출물 순서**에 대한 것이지 내부
전사 증거 사용 금지가 아니다.

### C. 상태 기계 — `run`은 사람 게이트를 우회한다

v1의 `vlogsrt project run = cut → caption`은 **유일한 안전망을 건너뛴다.** 그리고 지금
`EditPlan`에는 사람 검토 상태가 없고 revision 재료에도 없다(`edit.py:805`, `:1365`).

```
analysis_complete → review_required → (사람 승인, 컷 revision에 결속) → caption_complete
```

- 승인은 **특정 컷 revision**에 묶인다. 다른 revision으로 새지 않는다.
- `caption` 성공이 승인을 대신하지 못한다 — 그러면 위험한 컷이 세탁된다.
- 연속 `run`은 **`review_required`에서 멈추거나 명시적 draft만** 만든다.
- 재수출한 FCPXML은 부모 컷 revision과의 연결을 보존한다.

### D. 가드 시그니처

`protected=[]` 계열 재발을 막기 위해 **빈 기본값을 두지 않는다**:

```python
@dataclass(frozen=True)
class TranscriptEvidence:          # covered_ranges 밖은 증거가 아니다
    trace: SourceTrace
    boundary: BoundaryProjection | None
    repairs: tuple[Repair, ...]
    covered_ranges: tuple[SampleRange, ...]     # 필수 — §3-D

@dataclass(frozen=True)
class AudioOnlyEvidence:           # 명시적 선언이지 누락이 아니다
    reason: Literal["cut_before_transcript"]

def build_source_guard(reference, vad, cfg, *, evidence: SpeechEvidence) -> SourceGuard
```

- `AudioOnlyEvidence`만으로는 제거 권한이 생기지 않는다. 권한은 **R-05의 후보 검증**이 준다.
- `VadResult.error`·창 결함 → 현재의 `keep_all` 전체 보호 경로(`guard.py:576`)를 **그대로
  보존한다.** `VadUnavailable`(`runtime.py:159`)은 러너의 작업 실패·재개 상태로 연결한다
  (지금은 러너가 없어 미연결).
- `vad_without_text`는 `AudioOnlyEvidence`에서 **전 구간이 되어 의미가 없다** — 그 상태에서는
  산출하지 않거나 다른 이름으로 기록한다. 안 그러면 발화 런마다 리뷰가 하나씩 생겨
  `final_accepted`를 영구 차단하거나 리뷰를 무시하는 습관을 만든다.

---

## 3. 부분 전사 — v1이 가장 크게 틀린 곳

### A. 플래너만 바꾸면 안 된다 [Astra 재현]

`plan_source_ranges(reference, cfg)`(`speech.py:223`)는 `[0, sample_count)`를 빈틈없이
덮고 부분 범위 인자가 없다. **플래너만 부분화하면 뒤의 코드가 합쳐진 draft 하나로
`repair.settle`·`refine`을 돈다.** 재현된 결과:

- `[10,11)`·`[40,41)`·`[70,71)`의 '치즈' 셋 → **61초짜리 되풀이 하나**로 묶이고 repair
  요청이 `[10,71)`이 된다. **빠진 58초를 다시 듣는다.** 회복 실패 시 collapse 기준에도
  걸린다. 검출기는 시간 간격을 보지 않고 이웃 텍스트를 묶으며 길이를 첫 시작부터 마지막
  끝까지 잰다(`repetition.py:124`, `repair.py:498`).
- `[10,10.4)`·`[10.5,11)`의 큐 → refine이 **`[9.9,11.3)`의 한 문장으로 합친다**
  (`refine.py:121`). 제외한 간격과 retained 경계를 넘는다.

같은 계열로 `refine(cues, cfg, duration)`은 `duration>0`일 때만 끝을 클램프한다
(`refine.py:198-209`). 전체 duration을 주면 `lead_out`·`min_duration`이 큐를 범위 밖으로
밀고, 그러면 `_place_instance`가 **컷마다 `BOUNDARY_REQUIRED`를 붙인다**
(`captions.py:482-497`) — 경계 재전사로 무한 후퇴한다.

### B. 올바른 형태 — 범위마다 엔진 전체를 독립 실행

**불연속 retained 구간마다 전사·repair·refine을 통째로 따로 돌리고** 그 뒤 소스 시계와
세그먼트 ID를 조립한다. 이것이 정확히 **P4가 이미 하는 일**이다: `boundary.py:1238`(범위
국소 시계), `:1261`(`refine(..., run.local_duration)`), `:1509`(제한된 repair cutter).

부수 효과: `BoundaryTranscript`의 캐시 키에 정확한 `sample_range`가 이미 있어 **범위별
증분 캐시가 공짜로 따라온다.**

**단, P4를 그대로 쓸 수는 없다** — `SourceTrace`가 먼저 있어야 하고
`(source_id, pcm_analysis_key)` 쌍으로 닫히며, 같은 PCM의 여러 trace를 함께 못 받는다
(`boundary.py:1070`). 이 제약을 푸는 것이 R-03의 실제 작업이다.

### C. P4를 퇴역시키지 마라 [v1 주장 철회]

투영 산술 자체는 임의의 지원 sub-range에 맞고 전체 coverage를 요구하지 않는다. 그러나:

- 클립 끝에 걸친 큐는 **시간만 clamp하고 글자는 통째로 유지**한 채 두 인스턴스에
  **중복 배치된다**(`captions.py:462`). 한 문장이 두 번 나온다.
- 같은 소스를 `[0,10)`·`[5,15)`로 겹쳐 쓰면 합집합 `[0,15)` 전사의 `[8,12)` 큐가 첫
  인스턴스 끝을 넘는다.
- 범위마다 별도 `SourceTrace`로 넘기면 **projector가 같은 `source_id` 중복을 거부**한다
  (`captions.py:289`).
- 연속 blade는 `logical_slices`에서 합쳐지지만 projector는 개별 clip에 배치한다
  (`boundary.py:308`).

**R-03/R-04는 범위↔인스턴스 소유권, 겹치는 소스 사용, 연속 blade, 모델 응답의 범위
초과를 명시적 계약으로 가져야 한다.**

### D. coverage 계약 [Astra 재현]

`covered_ranges`를 키에 넣는 것만으로 닫히지 않는다. `ChunkCoverage`는 **요청한 조각에
응답이 왔는지**만 기록하고(`transcribe.py:648`), 필요한 구간을 전부 요청했는지는 아무도
검사하지 않는다. `SourceTrace.__post_init__`(`speech.py:151`)도 범위를 검증하지 않는다.

재현: retained `[10,12)`·`[20,22)` 중 앞쪽 글자만 있는 `SourceTrace(coverage=())`가
**미해결 표시 없이 SRT를 만든다.** 100초 PCM에 `[10,11)`만 coverage로 넣으면 가드가
`success`·보호 `[9.5,11.5)`·리뷰 없음이 된다 — **부분 trace를 나중에 `TranscriptEvidence`로
재사용하면 미전사 영역이 다시 무보호가 된다**(`guard.py:587`). `protected=[]` 계열의
여섯 번째 얼굴이다.

필요한 것:

- **요청 범위 / 완료 범위 / 실패 범위**를 구분하는 타입.
- caption 완료 전에 **필요 범위와 정확히 대조**.
- 부분 `TranscriptEvidence`는 **covered 밖을 보호하거나 사용을 거부**.
- 캐시 키에 정규화된 범위 + **결과를 바꾸는 강제 분할 경계**.
- 소비자가 `source_id`만으로 세대를 고르지 못하게.
- 샘플 환산은 `source_time − pcm_sample0_file_time` 뒤 ceil/floor(`boundary.py:846`).

---

## 4. 작업 계획

실행 순서: **X-01(사람, 병행 가능) · P6-04b → R-00 → R 나머지 → P7(재작성) → X 나머지**

### P6-04b — 패키지 내보내기 보정

후보 `defe23f`는 `ruff format --check` 실패(`tests/project/test_export.py:523`)와 Fable의
P7 블로커 둘을 안고 있다. **보정 브리프는 아직 커밋되지 않았다**(Sol이 초안 중).

**v1의 "R과 무관"은 철회.** `defe23f:export.py:590`의 `publish_package`는 **필수
`EditCaptionSet`과 `BoundaryProjection`**을 받고 다섯 파일을 만든다 — **STT 0회의 컷
패키지를 만들 수 없다.** 빈 caption/boundary 객체를 끼우는 것은 이 계획이 경계하는
"없음 → 빈 성공"이다. P6-04b는 그대로 보정해 머지하고, R-07이 export를 다시 연다.

### R 페이즈 — 순서 역전 (엔진)

| 태스크 | 소유 | 내용 |
|---|---|---|
| **R-00** 계약 결정 | `CLAUDE.md`, `DECISIONS.md`(**D35**), `PROJECT_CONTRACTS.md`, `docs/plans/` | **R-02/03보다 먼저.** §5의 규칙 4·9 개정 확정. Final Cut 계층은 현재 `DECISIONS.md`에 항목이 하나도 없다 — 그 갭도 닫는다 |
| **R-01** 가용성 잠금 | 새 `project/availability.py` | **가용성 잠금만** 추출: declared-no-audio, stream/PCM unavailable. `trace.status=="error"` 잠금과 경계 요청 잠금은 **재도출 불가**이며, 컷 선행에 그것이 없다는 것은 *동등한 재도출이 아니라 안전 조건 변경*임을 명시 기록. 원본 미해결은 `ResolvedMedia` 생성 전에 거부되므로(`media.py:217`) 기존 `UnavailableSourceAudio`로 꾸밀 수 없다 |
| **R-02** 증거 있는 가드 | `vad/guard.py`, **`project/config.py`**, `edit.py`, export | §2-D. `SpeechEvidence` 분기, `keep_all` 경로 보존, `vad_without_text` 재해석, evidence를 **revision 재료와 직렬화**에 포함 |
| **R-03** 범위별 독립 전사 | `project/speech.py`, `project/boundary.py` | §3-B/C/D. 범위마다 엔진 전체 독립 실행, 범위 국소 시계, 제한된 repair cutter, 요청/완료/실패 범위 타입, 범위↔인스턴스 소유권, 겹침·연속 blade·범위 초과 계약, 같은 PCM의 다중 trace 수용 |
| **R-04** 타임라인에서 자막 | 새 `project/recaption.py` | `caption_timeline(...)`. `Config`·artifact/workspace 경로·typed unavailable·실패 결과·cancellation/checkpoint 경계를 **시그니처에 명시**. 주입된 `Transcriber`를 부른다 — openai·one-call 불변식 위반 아님(확인함) |
| **R-05** 후보 검증 전사 | `edit.py` 또는 새 모듈 | §2-B. 후보 블록만 전사, 글자가 나오면 보호. **전사는 보호만 더하고 제거 권한을 주지 못한다.** 빈 응답·오류는 보호 |
| **R-06** 검토 상태 기계 | `edit.py`, export, workspace | §2-C. 네 상태, 승인을 컷 revision에 결속, 부모 컷 연결 보존 |
| **R-07** 컷 전용 패키지 | `project/export.py` | 자막 없는 컷 패키지 타입·publisher, `EditPlan` 없는 자막 단독 산출물 |
| **R-08** 입력 어댑터 | `project/fcpxml.py` 또는 새 모듈 | 단일 브라우저 클립을 명시적 프로젝트로 감싼다. 지원 부분집합 안내 |
| **R-09** Gradio 철거 | `ui/**` 삭제, `pyproject.toml`, `scripts/check_invariants.py`, `PROJECT_CONTRACTS.md` §1·§6, `DECISIONS.md`(**D36**) | 의존성 없음 — 아무 때나 병행 가능. mypy `files`에서 `ui` 제거, `ui` extra 제거, 불변식의 `ui/**` 스캔 제거, 계약 §1의 `ui/**` 행 제거, **D11/D14를 폐기 결정으로 기록**. 게이트 수치(ruff 87파일·mypy 43파일)가 바뀌므로 ledger도 갱신 |

### P7 (재작성) — 이름만 바꾸는 것이 아니다

| 브리프 | 필요한 재작성 |
|---|---|
| P7-01/02 | 부분 요청 키, 완료/실패 상태, 두 설정 세대, **컷 revision ↔ 자막 revision 관계**, 범위별 비용. 현재 러너 브리프는 full-source 비용을 요구하고 `captions`의 네트워크를 금지한다(`p7-02-runner.md:50`) — 둘 다 새 순서와 충돌 |
| P7-03 | 명령 표면 재설계(아래). `vlogsrt/cli.py`를 **수정한다** |
| P7-04 | identity rebind·stale 검증·cache 선택·실패 disposition을 **R-04로 실제 이전한 뒤에만** 축소. 지금 브리프는 caption 요소 무시보다 훨씬 넓다(`p7-04-reproject.md:28`) |
| P7-05 | **폐기** (R-09) |
| P7-06 | acceptance 의존성에서 UI 제거, 새 상태 기계 반영 |

```text
vlogsrt project inspect  INPUT --workspace DIR             # 지원 부분집합 + 범위별 비용 추정
vlogsrt project cut      INPUT --workspace DIR --out DIR   # 결과는 review_required
vlogsrt project approve        --workspace DIR --revision REV
vlogsrt project caption  INPUT --workspace DIR --out DIR   # 승인된 컷 위에서만 최종 산출
```

`run` 단일 명령은 **두지 않는다** — 사람 게이트를 우회한다. 필요하면 `--draft`로
명시적 초안만.

**"레거시·UI는 한 글자도 안 바뀐다"는 철회.** P7-03이 `vlogsrt/cli.py`를 수정하고,
R-09가 `ui/**`를 지운다. 올바른 표현은 **"레거시 자막 엔진의 동작 보존"** —
`vlogsrt run|redo|eval|probe|record`의 관측 가능한 동작과 SRT 바이트, 단방향 import 계약,
20개 불변식은 그대로다.

### X 페이즈 — macOS Workflow Extension

| 태스크 | 내용 |
|---|---|
| **X-01** 선행 스파이크 (사람) | (a) 브라우저/사이드바 드래그로 무엇이 오는가(UTI·payload·FCPXML 여부), (b) Apple events Open Document로 FCPXML을 돌려보낼 수 있는가, (c) `FCPXTimeline`이 실제로 주는 것. **입출력 계약을 여기서 확정한다.** Xcode + 설치된 FCP 필요 |
| **X-02** 앱 + 익스텐션 골격 | SwiftUI 컨테이너 + `.appex`. 버튼: 컷 / 승인 / 자막. 입력은 X-01 결과에 따라 드래그 수신 우선, 수동 export 드롭이 fallback |
| **X-03** 엔진 동봉 | `python-build-standalone` + `vlogsrt` 휠 + **`numpy`·`onnxruntime`·native dylib** + Silero ONNX(휠의 `data/` 경로 유지, `pyproject.toml:19`) + ffmpeg/ffprobe. 아키텍처·최소 OS·재배치·서명 시험. `xmllint` 존재와 FCPXML 1.14 DTD 접근도 설치 검증에 넣는다 |
| **X-04** 헬퍼와 권한 | 장시간 작업을 익스텐션 밖으로. **v1의 "ffmpeg 때문에 비샌드박스 헬퍼가 필수"는 철회** — Apple은 sandbox를 상속하는 command-line helper를 지원한다. 샌드박스 해제와 외부 배포는 **별도 결정**. **XML 접근 권한과 그 안에 적힌 미디어 경로 접근 권한을 구분**하고 사용자 선택·security-scoped bookmark·프로세스 간 권한 전달을 설계. extension/container 공유 workspace, 작업 수명, 재접속·취소·인증된 IPC |
| **X-05** 결과 반환 | 공식 pasteboard/file-promise 교환으로 browser·timeline **각각** 검증. `NSItemProvider` 파일 URL만으로 완료 판정하지 않는다. 자막 사이드카 `.srt` 가져오기는 **Relative to Timeline** |
| **X-06** 서명·배포 | Developer ID, **모든 실행 코드 서명**, Hardened Runtime, timestamp, notarization/stapling. Keychain 접근 주체와 STT/vision 자격증명 전달, Apple events 사용 시 automation 권한. **FFmpeg 실제 configure 옵션·포함 라이브러리·대응 소스·라이선스 배포 기록** — "LGPL 빌드" 한 줄로 끝나지 않는다 |

---

## 5. 규칙 4·9 개정 (Astra 문안 채택)

v1의 근거 — *"제거된 구간은 영상 자체가 없으니 자막 손실이 아니다"* — 는 **부적절하다.**
오검출된 발화를 먼저 영상에서 지우면 그 뒤 전사에서 손실을 발견할 기회까지 없어진다.
규칙 완화가 아니라 **파생 편집의 별도 권한·감사 계약**이 필요하다.

> **규칙 4 (개정):** 레거시 전사 입력은 계속 전체를 빈틈없이 덮는다. 프로젝트 자막
> 입력은 명시적으로 선택된 타임라인 revision의 가청 source 범위를 정해진 샘플 환산으로
> 도출한다. 전사·repair는 그 요청 범위를 빠짐없이 처리하며, VAD·빈 응답·오류를 이유로
> 입력을 추가로 제외하지 않는다. 자동 컷 후보는 승인된 자막 입력과 구분한다.

> **규칙 9 (개정):** 원본 미디어·전사 기록은 보존한다. 파생 영상의 제거는 source/instance
> 범위, 근거, 실패 상태, 복원 위치와 검토 상태를 기록한 EditPlan으로만 표현한다.
> 미처리·실패·경계 잘림은 빈 성공으로 바꾸지 않는다. 자동 컷의 미검토 상태는 후속
> 자막 생성으로 해제되지 않는다.

원문 위치: `CLAUDE.md:43`, `CLAUDE.md:71`.

---

## 6. 검증 방법

1. **오프라인 합성 인수**(P7-06 확장): `cut` → 실제로 짧아짐 → `review_required`에서
   멈춤 → `approve` → `caption` → 큐가 컷을 가로지르지 않음 → revision 결속 확인.
2. **범위별 전사 회귀** — §3-A의 두 재현을 테스트로 고정: 떨어진 세 '치즈'가 하나의 긴
   되풀이로 묶이지 않을 것, refine이 간격을 넘어 합치지 않을 것.
3. **coverage 회귀** — §3-D의 두 재현: 부분 coverage가 미해결 표시 없이 SRT를 만들지 않을
   것, 부분 trace가 미전사 영역을 무보호로 만들지 않을 것.
4. **후보 검증의 방향성** — 후보 블록에서 글자가 나오면 반드시 제거가 취소되고, 빈 응답과
   오류도 보호가 되는지. 그리고 **어떤 전사 결과도 제거 권한을 새로 만들지 못하는지.**
5. **가드 단조성**(설정 고정) — **같은 VAD 결과·임계값·패딩**에서 `TranscriptEvidence`
   가드 ⊇ `AudioOnlyEvidence` 가드. 낮은 임계값·큰 패딩을 쓰면 포함 관계가 역전되므로
   **새 설정 평가는 별도 시험**으로 분리한다.
6. **비용** — 타임라인 % 가 아니라 **유일한 실제 요청 샘플 수를 요청 종류별로** 비교.
7. `make check`: Ruff, mypy, pytest, 불변식 20/20 (+R-00·R-09가 바꾸는 것).
8. **PENDING_HUMAN**: 실제 FCP import/re-export, 원본 4K relink, 실제 77분 미디어, 유료
   STT/vision, X-01 스파이크, 공증 앱 설치, **희미한/음악 아래 발화의 실제 검출률**.

---

## 7. v1에서 뒤집은 주장 (기록)

| v1이 주장한 것 | 실제 |
|---|---|
| 사람 검토가 전사 보호선보다 강하다 | 삭제. 재생으로는 사라진 대사를 못 찾는다 |
| 30% 자르면 STT 30% 절감 | 틀림. 유일 소스 단위 중복 제거 때문에 성립 안 함 |
| P4의 존재 이유가 사라진다 | 틀림. P4가 오히려 R-03의 재사용 기반이다 |
| `plan_source_ranges`만 넓히면 된다 | 틀림. repeat가 간격을 넘어 묶이고 refine이 합친다 |
| 기존 projector면 충분하다 | 틀림. 걸친 큐의 글자 중복, `source_id` 중복 거부 |
| `covered_ranges`를 키에 넣으면 닫힌다 | 틀림. coverage는 요청 대비 응답만 본다 |
| R-01이 잠금 전체를 재도출한다 | 틀림. 가용성 잠금만. 나머지는 안전 조건 변경 |
| P6-04b는 R과 무관 | 틀림. `publish_package`가 caption/boundary를 필수로 받는다 |
| 레거시·UI는 한 글자도 안 바뀐다 | 틀림. P7-03이 CLI를 고치고 R-09가 UI를 지운다 |
| 호스트 API로 타임라인 직접 읽기(최적화) | 그런 공개 API가 없다. 대신 드래그 수신이 있다 |
| FxPlug는 타임라인을 전혀 못 건드린다 | 과장. `FxCommandAPI`는 있다(컷 재구성은 없다) |
| ffmpeg 때문에 비샌드박스 헬퍼가 필수 | 틀림. sandbox 상속 헬퍼가 지원된다 |

Astra가 **확인하지 못한 것**: 실제 희미/음악 발화 재현율, 짧은 범위의 유료 Whisper
품질·청구량, 설치된 Workflow Extension SDK의 헤더와 실행 동작, FCP import/re-export,
선택 클립 전달, 공증 앱 설치. 전체 `make check`도 실행하지 않았다.
