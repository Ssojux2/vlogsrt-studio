# A7-2 최종 자막 엔진 라이브러리 — 작업 보고 (Claude-GLM glm-5.3[1m] max)

날짜: 2026-10-02 · 분기: `Ssojux2/raw-a7-adapters` (base a5567c8) · 태스크: task_9b1accd7d4f2

## 1. 무엇을 만들었나

**범위**: A7-2 엔진 라이브러리만. UI·서비스·workspace·CLI·기존 엔진 파일은
건드리지 않았다(아래 4번 참조 — A7-1 온도 인터는 승인 대기 중인 **질문**이지
수정이 아니다).

### 신규 파일 (전부 이 태스크 소유)

| 파일 | 내용 |
|---|---|
| `vlogsrt/project/raw/caption_config.py` | `FinalCaptionSettings` 동결 설정 — 청크 상한(≤180초·≤24MB, D26·D28 동결), 프롬프트 리비전, 초안 언어, 읽기 속도 경고선(초기 제안값 20 CPS). 값역 검사 포함. |
| `vlogsrt/project/raw/final_video.py` | FCPXML 없는 `FinalVideoInput`. `register_final_video` = SHA-256 신원 → 오디오 스트림 확정(단일 트랙이 아니면 `AmbiguousAudioSelection`, 없으면 `FinalVideoNoAudio` — §11.11의 `no_audio`와 무발화 구분) → 검증 사본(해시 같으면 재복사 없음, 어긋나면 `FinalVideoCopyFailed`, 등록 뒤 원본 변경은 `FinalVideoChanged`) → `manifest final-video.json` → `select_audio`(기존 `preprocess.extract_pcm_s16le` 경계)로 잰 원점·샘플 수. `plan_caption_chunks`(전체 빈틈없기, 꼬리 포함), `caption_chunk`(오프셋=샘플/주파수 정확 유리수), `pcm_span_cutter`(`repair.Source.cutter`용), `to_final_time`(원점을 정확히 한 번). |
| `vlogsrt/project/raw/final_caption.py` | 실행 엔진. `plan_caption_run`(지문=리비전+오디오 선택+범위+모델+힌트+엔진 임계값+등록표 리비전, Decimal 비용 추정+복구 예약 — 요금은 `capabilities.price_per_minute`에서만), `CaptionApproval`(7개 등식 바인딩+예산 상한), `run_final_captions`(시간 경로 → `repair.settle` 어댑터(RESCUE→TimingProvider, ARBITER→TextTranscriptProvider, 커터=저장 PCM 슬라이스) → 불변 골격 → 문구 경로 **청크당 응답 하나** → `retext.plan` 게이트 → 글자만 교정(시각·seg_ids·순서 불변) → 큐·검토 항목·검증 표시). 상태 5종 구분(`no_audio`는 등록 단계, `confirmed_no_speech`는 전부 정상 응답+전부 비어 있을 때만, 실패 섞이면 `partial_failure`+명시적 초안). |
| `vlogsrt/project/raw/caption_artifacts.py` | 원자적 게시 — `draft.<lang>.srt`(엔진 코덱)/`draft.<lang>.vtt`(같은 자료 별도 직렬화+재파싱), `caption_trace.json`, `alignment_report.json`, `quality_report.json`(speech_basis 포함), `provider_snapshot.json`, `responses/<request_id>.json`. **직렬화 후 재파싱 검사를 통과해야** 게시하고, 다른 리비전의 게시 자리는 `ArtifactRevisionMismatch`로 봉쇄. `manual_edits.jsonl` 이름만 정의(A7-3 소유). |
| `tests/raw/test_final_video.py` (16) · `tests/raw/test_final_caption_run.py` (26) · `tests/raw/test_final_artifacts.py` (8) | 아래 매트릭스 참조. `test_final_caption.py`는 기존 `tests/project/test_final_caption.py`와 모듈명이 겹쳐 `-run`으로 명명(과제의 `test_final_*` 규칙 유지). |

### 핵심 계약 증명 (테스트)

- **교정+시각 불변**: 틀린 whisper 문구("망원동 커피에")가 게이트 통과 후 교정되고,
  큐 시각은 `원점 + 청크 시작 샘플/주파수 + 세그먼트 시각`과 **유리수로 정확히**
  같다(재전사로 교체된 세그먼트 포함). 실추출(`-itsoffset 2`로 오디오가 늦은 합성
  최종본)에서도 잰 원점이 정확히 한 번만 들어간다.
- **미대응 문구**: 세그먼트 사이 글자는 `unmapped_text` 기록일 뿐 큐 수는 골격 수와
  같다 — 가짜 cue 없음.
- **검증 불변**: verify on/off — SRT 바이트 동일, 문구 호출 수 동일(재사용).
- **라이브 경로 문**: `network_mode=online` 또는 **실전송 어댑터가 주입된 경우**
  모두에서 `VLOGSRT_OFFLINE` → 승인 바인딩(7필드 등식+예산) → 수명주기
  `preflight_online_transcription`가 첫 호출 앞에서 순서대로 닫힌다. 위조·낡은
  승인 7종 전부 차단, 제공자 부재·종료일(2027-02-26) 도달은 **호출 0회**로 종료.
- **실패 은폐 금지**: 문구 실패 청크 → `partial_failure`+명시적 초안(whisper 글자
  유지, correction=unavailable), 시간 실패 → 그 조각에 큐 없음, 꼴이 어긋난 응답
  → `MalformedCaptionResponse` 기록, 낡은 신원(청크 변조) → `ChunkBindingMismatch`
  기록, 취소 → 끝난 증거 보존+`cancelled`, 예약 장부 → 예산 초과 다음 요청 차단.
- **되풀이 재전사**: echo 되풀이가 재전사로 갈아끼워짐(`replaced`, 심판 0호출),
  회복 못 한 긴 되풀이(32초)는 첫 장만 남고(`collapsed`) 교정 제외·검토 상태 기록.
- **리비전 격리**: 같은 파일명 덮어쓰기=새 리비전, 다른 리비전의 게시 자리 봉쇄,
  매 실행마다 새 호출(응답 재사용 경로 자체가 없음).
- **입력 불가침**: 원본 바이트·검증 사본 해시 불변, 중복 복사 없음, 사본 변경
  감지(`require_unchanged`), `FinalVideoNoAudio`·`AmbiguousAudioSelection` 구분.
- **빈 결과 불확실성**: `speech_basis`(`confirmed_no_speech`/`unconfirmed_empty`/
  `cues_present`)를 결과·품질 보고서에 기록 — 실패 섞인 빈 결과는 무발화 확정이 아님.

## 2. 검증 증거 (참 명령·실제 종료 코드)

| 명령 | 결과 |
|---|---|
| `make check PY=/Users/ssojux2/Working/vlogsrt/.venv/bin/python` | **통과** (lint+format+types+test+invariants, exit 0) |
| `python -m pytest -q` (풀스위트, 오프라인 픽스처) | **3620 passed**, exit 0 (기준 3572 + 신규 48) |
| `python -m pytest tests/raw/ -q` | 통과, exit 0 |
| `python -m mypy` (vlogsrt+ui 76파일) | no issues, exit 0 |
| `python scripts/check_invariants.py` | **21/21 OK**, exit 0 |

유료 API 호출 0회 — 네트워크 없음. ffmpeg는 합성 최종본 제작·실추출 시험에만
사용(skip 표시). `.env`·API 픽스처·`golden/`·기존 패키지·리비전 미접근.

## 3. 승인 대기 — 재전사 온도 (루트 질문 발송)

엔진 `repair.settle`은 재전사를 1차보다 뜨겁게(0.8→1.0, D28) 묻는다. A7-1의
`TransportTimingProvider.timed_transcript`에 `temperature` 인자가 없어 **라이브**
재전사가 온도 없이(API 기본 0) 나갈 수 있다. 나의 어댑터는 시그니처 검사로
온도를 받는 제공자에만 값을 넘긴다(오프라인·가짜는 무관, 계약·시험은 닫혀 있음).
루트에 (A) `caption_provider.py`에 선택 인자 한 줄 추가 승인 여부, (B) A7-3에서
래퍼 제공자로 해결 중 어느 쪽인지 질문했다(`orca orchestration ask`). 답변 전까지
기존 파일은 무편집 — 이 보고 시점의 코드는 (B)를 전제로도 그대로 유효하다.

## 4. 경계 준수

- 신규 4모듈+3시험 외 기존 파일 0편집 (git diff로 확인 예정).
- 모델 리터럴 없음(전부 `RawWorkflowConfig`/`RawModelRoles`에서), 요금 리터럴 없음
  (`capabilities.price_per_minute`), 임계값은 설정(`FinalCaptionSettings`·엔진
  `Config`)에서만 — 코드 리터럼 0 (invariant 8·18 통과).
- `pipeline.run` 미호출 — `repair.settle`/`retext.plan`/`output` 코덱을 라이브러리로만
  소비(루트 지시 확인). whisper에 prompt·keywords 안 감(시간 요청 빌더에 힌트 인자 없음).
- A7-3 소유물(검토 UI·final 게시·수동 patch 적용)은 만들지 않았다 — `manual_edits.jsonl`
  상수와 타입만 남김.

## 5. 남은 것

- 위 온도 인터페이스 루트 결정 (A/B).
- A7-3: 서비스·UI 통합, 검토 화면, final 게시, 수동 patch/revision, 작업관리자 연결.
- 실계정 통합시험·요금 검증은 별도 승인 후(이번 범위 밖, NOT_RUN 명시).
