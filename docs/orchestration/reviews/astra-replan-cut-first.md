# 재계획 방향 검토 — 컷 선행 + Final Cut 플러그인 (gpt-6-astra, read-only)

- 일시: 2026-09-13
- 검토자: `codex --model gpt-6-astra` (effort xhigh, `--sandbox read-only`)
- 대상: Opus 5가 작성한 재계획 v1 (사용자 요구 — 자막→컷 순서를 컷→자막으로 뒤집고,
  Final Cut 플러그인으로 납품)
- 기준 체크아웃: `34f1301` (제품 HEAD `97e1063`). 검토 중 저장소는 수정되지 않았다.
- 확정본: `docs/plans/2026-09-13-cut-first-and-plugin.md`

**판정: UNSOUND** — 차단 6 · 높음 5 · 중간 1. 그대로 디스패치 금지.

Astra는 구조 검사 20/20 통과를 확인했고, 아래 §3·§5의 부분 전사 결함은 파일을 쓰지 않는
합성 호출로 **직접 재현**했다. 12개 지적 전부가 확정본에 반영되었으며, 뒤집힌 Opus 주장
12건의 목록은 확정본 §7에 있다.

---

**판정: UNSOUND.** 순서 역전은 가능하지만, 현재 재계획은 안전성·부분 전사·export 계약을 충분히 분해하지 못했습니다. 그대로 디스패치하면 안 됩니다.

파일은 수정하지 않았습니다. 현재 체크아웃의 구조 검사 **20/20 통과**를 확인했고, 아래 부분 전사 결함은 파일을 쓰지 않는 합성 호출로 재현했습니다.

**1. [차단] 네 가지 완화책은 전사 보호선을 대체하지 못합니다.**

여섯 span 공급원의 합집합이라는 주장은 맞습니다. 다섯은 `SourceTrace`·경계 전사에 의존합니다. 다만 `_uncertain_spans`는 **인식된 발화가 아니라 글자 없는 응답 구간의 보수적 보호**입니다. 독립적인 음성 검출기 다섯 개가 있다는 뜻은 아닙니다. [guard.py:258](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/vad/guard.py:258)

| 상황 | 현재 코드에서 확인되는 동작과 R-02의 위험 |
|---|---|
| 희미한 발화가 10초 동안 임계값 미만 | 유효한 낮은 확률은 `success`입니다. 전사 보호가 없으면 빈 보호 구간이 허용되고, vision 승인 시 3초만 남길 수 있습니다. |
| 음악 아래 낮은 대사 | 원본 오디오에 섞인 대사를 VAD가 놓치면 위와 같습니다. 이미지 모델은 화면 밖 대사를 확인할 수 없습니다. 별도 연결 음악 트랙이라면 현재 파서가 입력 자체를 거부합니다. |
| 문장 중간 7초 정적 | 현재 기본 패딩 0.5초씩이면 약 6초의 적격 구간이 남습니다. 승인되면 3초를 제거할 수 있습니다. 기존 Whisper 세그먼트가 정적 전체를 포함했다면 그 보호도 잃습니다. |
| 추론 예외·잘못된 확률·창 누락 | 현재는 `VadResult.error` 또는 입력 결함 → 전체 보호 `keep_all`입니다. 이 경로는 반드시 보존해야 합니다. |
| ONNX·의존성·해시 부재 | `VadUnavailable`을 던집니다. 아직 runner가 없으므로 이것을 작업 실패·재개 상태로 연결하는 구현은 없습니다. |

근거: [양성 판정](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/vad/guard.py:337), [실패·coverage 검사](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/vad/guard.py:576), [런타임 부재](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/vad/runtime.py:159), [편집 제외 조건](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/edit.py:371).

`InstanceGuard.status != "success"`는 빈 `protected_frames`여도 제거를 막습니다. 반면 `success + protected=[]`는 제거 권한이 있습니다. `AudioOnlyEvidence(reason=...)`는 **의도적 생략의 표시일 뿐 음성 부재의 증명은 아닙니다**. `AnalysisFailure`도 현재는 필드뿐인 기록형이고, 실제 연결 사용처가 없습니다. [analysis_models.py:19](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/analysis_models.py:19)

**최소 수정:** “사람 검토가 전사보다 강하다”를 삭제하십시오. 편집본 재생만으로는 이미 사라진 대사를 발견하기 어렵습니다. 자동 컷은 제안 상태로 두고, **제거된 원본 오디오와 접합부를 비교 청취하는 검토**를 명시해야 합니다. 낮은 임계값·큰 패딩의 효과는 실제 발화 손실 평가로 검증해야 합니다.

**2. [차단] `run = cut → caption`은 유일한 안전망이라는 사람 게이트를 우회합니다.**

계획에는 검토 완료를 입력으로 받는 타입·명령·revision 연결이 없습니다. `audio_only`를 영구적으로 `final_accepted` 불가로 만들면 사람 검토 후 완료 경로가 없고, caption 성공으로 해제하면 앞선 위험한 컷이 세탁됩니다.

현재 `EditPlan`에는 사람 검토 상태가 없으며, revision 재료에도 새 evidence 정보가 없습니다. [edit.py:805](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/edit.py:805), [edit.py:1365](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/edit.py:1365)

**최소 수정:** `analysis_complete`, `review_required`, 사람 승인, `caption_complete`를 분리하고 승인 대상을 컷 revision에 묶으십시오. 연속 `run`은 검토 대기에서 멈추거나 명시적 draft만 만들어야 합니다. 재수출한 FCPXML과 부모 컷의 연결도 보존해야 합니다.

**3. [차단·재현] R-03은 planner 변경만으로 구현할 수 없습니다.**

`plan_source_ranges(reference, cfg)`는 실제로 `[0, sample_count)`를 gapless하게 덮습니다. 부분 범위 인자는 없습니다. [speech.py:223](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/speech.py:223)

planner만 부분화하면 이후 코드가 **전체 source duration과 합쳐진 draft**로 `repair.settle`·`refine`을 실행합니다. repair cutter도 retained 범위 집합을 모릅니다. [speech.py:363](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/speech.py:363), [speech.py:659](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/speech.py:659)

재현 결과:

- `[10,11)`, `[40,41)`, `[70,71)`의 ‘치즈’ 세 개 → **61초짜리 repeat**, repair 요청 `[10,71)`.
- 빠진 구간 58초까지 다시 듣게 됩니다. 복구 실패 시 30초 collapse 기준에도 걸릴 수 있습니다.
- `[10,10.4)`, `[10.5,11)`의 큐 → refine 결과 `[9.9,11.3)`의 **합쳐진 문장**. 제외한 간격과 retained 경계를 넘습니다.

검출기는 요청 경계·시간 간격 없이 이웃 텍스트를 묶고, repeat 길이는 처음 시작부터 마지막 끝까지 잽니다. [repetition.py:124](/Users/ssojux2/Working/vlogsrt/vlogsrt/repetition.py:124), [repair.py:498](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:498), [refine.py:121](/Users/ssojux2/Working/vlogsrt/vlogsrt/refine.py:121)

**최소 수정:** 불연속 retained 구간마다 **전사·repair·refine 전체를 독립 실행**하고, 그 뒤 source 시계·세그먼트 ID를 조립해야 합니다. P4의 범위 국소 시계와 제한된 repair cutter가 더 가까운 재사용 대상입니다. [boundary.py:1238](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:1238), [boundary.py:1509](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:1509)

**4. [차단] “P4 존재 이유가 사라진다”와 “기존 projector면 충분하다”는 거짓입니다.**

투영 산술 자체는 임의의 **지원되는 정상 속도 sub-range**에 맞습니다. 전체 source coverage를 요구하는 계산은 없습니다. 그러나 걸친 큐는 시간을 clamp하고 **전체 글자를 유지한 채 `BOUNDARY_REQUIRED`**로 내보냅니다. 재현에서도 한 큐의 전체 문장이 두 retained 인스턴스에 반복 배치됐습니다. [captions.py:462](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/captions.py:462)

또한 같은 source의 사용 범위가 `[0,10)`, `[5,15)`라면:

- 합집합 `[0,15)` 전사에서는 `[8,12)` 같은 큐가 첫 인스턴스 끝을 넘습니다.
- 각 범위를 별도 `SourceTrace`로 전달하면 현재 projector는 동일 `source_id` 중복을 거부합니다. [captions.py:289](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/captions.py:289)
- 연속 blade는 `logical_slices`에서 합쳐지지만 projector는 개별 clip에 배치합니다. [boundary.py:308](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:308)

**최소 수정:** 범위·인스턴스 소유권, 겹치는 source 사용, 연속 blade, 모델 응답의 경계 초과를 처리하는 계약을 R-03/04에 추가하십시오. 범위별 전사만으로 경계 안전이 증명되기 전까지 P4를 퇴역시키면 안 됩니다.

**5. [차단·재현] `covered_ranges`를 키에 넣는 것만으로 coverage 계약이 닫히지 않습니다.**

`ChunkCoverage`는 **요청한 조각에 응답이 왔는지** 기록합니다. 필요한 타임라인 구간을 모두 요청했는지는 검사하지 않습니다. `SourceTrace.__post_init__`도 coverage 범위를 검증하지 않습니다. [transcribe.py:648](/Users/ssojux2/Working/vlogsrt/vlogsrt/transcribe.py:648), [speech.py:151](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/speech.py:151)

재현:

- retained `[10,12)`, `[20,22)` 중 앞쪽 글자만 있는 `SourceTrace(coverage=())` → projector가 **미해결 표시 없이 SRT 생성**.
- 100초 PCM에 `[10,11)`만 coverage로 기록 → 기존 가드는 `success`, 보호 `[9.5,11.5)`, review 없음.

즉 부분 trace를 나중에 `TranscriptEvidence`로 재사용하면 **미전사 영역이 다시 보호 없음으로 바뀝니다**. [guard.py:587](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/vad/guard.py:587)

**최소 수정:** 요청 범위·완료 범위·실패 범위를 구분하고, caption 완료 전에 필요한 범위와 정확히 대조하십시오. 부분 `TranscriptEvidence`는 범위 밖을 보호하거나 사용을 거부해야 합니다. 키에는 정규화된 범위뿐 아니라 결과를 바꾸는 강제 분할 경계도 포함해야 합니다. 소비자 역시 source ID만으로 세대를 선택하면 안 됩니다. 현재 P4도 동일 PCM의 여러 trace를 함께 받지 못합니다. [boundary.py:1070](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:1070)

샘플 환산은 `source_time - pcm_sample0_file_time` 뒤 ceil/floor여야 합니다. “타임라인과 정확히 같은 범위”는 이 안쪽 샘플 반올림을 정의해야 성립합니다. [boundary.py:846](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:846)

**6. [높음] R-01은 기존 잠금 전체를 media+PCM으로 재도출할 수 없습니다.**

`edit.py`가 cue 타입을 직접 받지 않는다는 주장은 맞습니다. 입력은 `InstanceGuard`, 잠금, unavailable 기록입니다. [edit.py:158](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/edit.py:158)

하지만 잠금의 기원은 셋입니다.

| 잠금 원인 | 전사 없이 동일하게 도출 가능한가 |
|---|---|
| declared-no-audio·stream/PCM unavailable | 가능 |
| `trace.status == "error"` | 불가능: PCM 성공 후 전사 실패 |
| 경계 요청 참여 인스턴스 | 불가능: 잘린 세그먼트와 cue 의존성에서 요청 생성 |

근거: [boundary.py:624](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:624), [boundary.py:734](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:734), [boundary.py:973](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/boundary.py:973). `BoundaryProjection`은 이를 그대로 전달합니다.

**최소 수정:** R-01은 **가용성 잠금만 추출**한다고 고치고, 전사 오류·경계 잠금은 별도 보존하십시오. 컷 선행에서 후자가 없다는 것은 동등한 재도출이 아니라 안전 조건 변경입니다.

또한 원본 미해결은 현재 `ResolvedMedia` 생성 전에 거부됩니다. 원본 SHA가 없는 상태를 기존 `UnavailableSourceAudio`로 꾸밀 수 없습니다. [media.py:217](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/media.py:217)

**7. [차단] 컷 전용 export 계약이 빠졌습니다.**

직접 확인한 후보 `defe23f:vlogsrt/project/export.py:590`의 `publish_package`는 **필수 `EditCaptionSet`과 `BoundaryProjection`**을 받으며, SOURCE/EDIT SRT까지 다섯 파일을 만듭니다. R-02에서 `evidence_kind`만 추가해서는 STT 0회의 컷 패키지를 만들 수 없습니다.

**최소 수정:** 자막 미실행을 표현하는 명시적 컷 패키지 타입·publisher를 추가하십시오. 빈 caption/boundary 객체를 만들어 끼우면 계획이 경계하는 “없음 → 빈 성공”이 됩니다. 자막 단독 산출물도 `EditPlan` 없이 완료될 수 있는 계약이 필요합니다. P6-04b를 먼저 보정하는 순서는 가능하지만 **R과 무관하다는 주장은 삭제**해야 합니다.

**8. [높음] revised P7은 명령 이름만 바꿀 수 없습니다.**

| 작업 | 필요한 최소 수정 |
|---|---|
| R-02 | ownership에 `project/config.py`, 설정 parser·검증 테스트, evidence를 포함한 revision/직렬화 추가 |
| R-04 | `Config`, artifact/workspace 경로, typed unavailable·실패 결과, cancellation/checkpoint 경계 명시 |
| R-05 | R-02/03보다 먼저 계약 결정. `tests/test_invariants.py`와 ledger/브리프 갱신 포함 |
| P7-01/02 | 부분 요청 키·완료 상태·두 설정 세대·컷/자막 revision 관계·범위별 비용으로 재작성 |
| P7-04 | identity rebind, stale 검증, cache 선택, 실패 disposition을 R-04로 **실제 이전**한 뒤에만 축소 |
| P7-05/06 | Gradio 작업을 유지할지 제거할지 결정하고 acceptance 의존성 수정 |

현재 runner 브리프는 full-source 비용을 요구하고 `captions`의 네트워크 사용을 금지합니다. [p7-02:50](/Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/p7-02-runner.md:50) P7-04는 caption 요소 무시 이상의 계약을 갖고 있습니다. [p7-04:28](/Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/p7-04-reproject.md:28)

“레거시/UI 한 글자도 변경 없음”도 틀립니다. P7-03은 `vlogsrt/cli.py`, P7-05는 `ui/service.py`, `ui/app.py`를 수정합니다. **기존 동작 보존**으로 바꾸십시오.

R-04가 공개 `speech` 실행기나 주입된 `Transcriber`를 호출하는 것은 openai/one-call 검사 위반이 아닙니다. openai 직접 import만 금지이고, one-call 검사는 `transcribe.py` 안의 `transcriptions.create` 호출 지점 수를 셉니다. 이를 완화할 이유는 없습니다. [check_invariants.py:114](/Users/ssojux2/Working/vlogsrt/scripts/check_invariants.py:114), [check_invariants.py:996](/Users/ssojux2/Working/vlogsrt/scripts/check_invariants.py:996)

**9. [높음] 제품 입력 범위가 “이미 열린 클립/프로젝트” 요구를 충족하지 못합니다.**

현재 파서는 `library/event/project/sequence/spine`을 각각 요구합니다. 브라우저의 단일 클립 XML은 그대로 받을 수 없습니다. 연결 오디오·타이틀·transition·compound·retime도 거부합니다. [fcpxml.py:132](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/fcpxml.py:132), [fcpxml.py:87](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/fcpxml.py:87)

**최소 수정:** 단일 클립을 명시적 프로젝트로 감싸는 입력 어댑터와 지원 범위 안내를 추가하십시오. “사람이 고친 현재 타임라인”도 지원 부분집합에 한정됨을 명시해야 합니다. X-01은 엔진 완료 뒤 선택 작업이 아니라 **입력·출력 계약을 결정하는 선행 스파이크**여야 합니다.

**10. [높음] Workflow Extension 선택은 타당하지만 API 설명은 과장·누락이 있습니다.**

- 공개 `FCPXTimeline`은 active sequence·시간 범위·playhead·observer를 제공합니다. **클립 구조 전체나 FCPXML 취득 API는 문서에 없습니다.** 이를 “직접 읽기 최적화”로 예정하지 마십시오. 공개 API에 임의 타임라인 재구성 기능이 없다는 판단은 타당합니다. [Apple FCPXTimeline](https://developer.apple.com/documentation/professional_video_applications/fcpxtimeline?language=objc)
- 입력은 수동 XML export만이 아닙니다. **FCP sidebar/browser에서 클립·프로젝트를 직접 드래그해 받는 공식 경로**가 있습니다. 사용자가 원하는 UX에 더 가깝습니다. [Apple 수신 drag-and-drop](https://developer.apple.com/documentation/professional-video-applications/supporting-drag-and-drop-to-receive-final-cut-pro-data)
- 출력은 **Apple events의 Open Document로 FCPXML import를 요청**할 수 있습니다. `host.timeline`의 import 기능을 찾는 작업과 구분하십시오. [Apple 프로그램 방식 전송](https://developer.apple.com/documentation/professional-video-applications/sending-data-programmatically-to-final-cut-pro?language=objc)
- FxPlug가 타임라인 관련 기능을 전혀 제공하지 않는다는 표현은 부정확합니다. playhead 이동·undo/redo 등의 `FxCommandAPI`가 있습니다. 다만 이 제품에 필요한 임의 컷 재구성 API는 확인되지 않습니다. [Apple FxCommandAPI](https://developer.apple.com/documentation/professional_video_applications/fxcommandapi?language=objc)

따라서 Workflow Extension은 **FCP 내부 패널의 적합한 형태**이지 FCPXML 연동의 유일한 경로는 아닙니다. X-05는 단순 `NSItemProvider` 파일 URL만으로 완료 판정하지 말고, 공식 pasteboard/file-promise 교환과 browser/timeline 각각의 결과를 검증해야 합니다. [Apple 송신 drag-and-drop](https://developer.apple.com/documentation/professional-video-applications/supporting-drag-and-drop-for-data-sent-to-final-cut-pro)

**11. [높음] X-03/04/06의 배포·권한 설계가 부족합니다.**

Python+ffmpeg 동봉은 기존 엔진 보존 관점에서 합리적입니다. 그러나 **ffmpeg 때문에 비샌드박스 헬퍼가 필수라는 주장은 틀립니다.** Apple은 sandbox를 상속하는 command-line helper를 지원합니다. 외부 배포와 sandbox 해제는 별도 결정입니다. [Apple App Sandbox](https://developer.apple.com/documentation/security/protecting-user-data-with-app-sandbox)

최소 추가 작업은 다음과 같습니다.

- XML 접근 권한과 그 안에 적힌 미디어 경로의 접근 권한을 구분하고, 사용자 선택·bookmark·프로세스 간 권한 전달을 설계할 것. [Apple 파일 접근](https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox)
- extension/container 공유 workspace, 작업 수명, 재접속·취소·인증된 IPC를 정할 것. XPC는 선택지이며 별도 privileged helper는 필요성이 입증되지 않았습니다. Apple의 장시간 작업 위임 권고는 확인되지만, 이 extension의 구체적 메모리 상한은 확인하지 못했습니다. [Apple Workflow Extensions](https://developer.apple.com/documentation/professional-video-applications/workflow-extensions)
- Python뿐 아니라 `numpy`, `onnxruntime`, native `.so`/dylib, ffmpeg/ffprobe를 포함한 아키텍처·최소 OS·재배치·서명 시험을 할 것. ONNX는 wheel의 기존 `data/` 패키지 경로를 유지해야 합니다. [pyproject.toml:19](/Users/ssojux2/Working/vlogsrt/pyproject.toml:19)
- Developer ID, 모든 실행 코드의 서명, Hardened Runtime, timestamp, notarization/stapling을 포함할 것. Keychain 접근 주체와 STT/vision 자격증명 전달, Apple events 사용 시 automation 권한도 별도 정의할 것. [Apple 공증 요구사항](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution?language=objc)
- FFmpeg의 실제 configure 옵션·포함 라이브러리·정확한 대응 소스와 라이선스 배포를 기록할 것. “LGPL 빌드” 한 줄로 끝나지 않습니다. [FFmpeg 배포 지침](https://ffmpeg.org/legal.html)

`xmllint` 존재와 FCPXML 1.14 DTD 접근도 설치 검증에 넣어야 합니다.

**12. [중간] 비용 절감과 가드 비교 검증의 기준이 잘못됐습니다.**

타임라인 30% 제거가 STT 30% 감소라는 등식은 성립하지 않습니다. 동일 source 구간을 두 번 사용하다 한 인스턴스를 없애면 타임라인은 줄어도 유일 전사량은 그대로입니다. repair·재시도·캐시 상태도 다릅니다. 기존 엔진부터 source 분석을 중복 제거합니다. [speech.py:722](/Users/ssojux2/Working/vlogsrt/vlogsrt/project/speech.py:722)

또한 `TranscriptEvidence guard ⊇ AudioOnlyEvidence guard`는 **같은 VAD 결과·임계값·패딩**에서만 기대할 수 있습니다. audio-only에 더 낮은 임계값·큰 패딩을 쓰면 포함 관계가 역전되는 부분이 생깁니다.

**최소 수정:** 비용은 유일한 실제 요청 샘플 수와 요청 종류별로 비교하십시오. 가드 단조성 테스트와 새로운 설정의 발화 손실 평가를 분리하십시오.

순서에 대해서는 **“발화 보호용 분석 → 컷 제안·검토 → 최종 자막 생성”**이 더 낫습니다. 사용자의 자막 생성 순서 요구가 내부 전사 증거까지 금지하는 것은 아닙니다. 저비용 VAD는 후보를 만드는 데 유용하지만 발화 부재 증명으로 충분하지 않습니다. 후보 주변 전사를 추가해도 빈 응답을 무음 확정으로 쓰면 안 됩니다.

짧은 retained 범위를 독립 전사하면 긴 반복 전파는 줄지만, 시작 어절·문맥 손실은 늘 수 있습니다. 이미 잘린 첫 음절을 모델이 복원했다고 보장할 수 없습니다. 짧은 범위에서는 D29 검출 개수·길이 조건과 D28의 짧은 반복 유지 정책도 여전히 작동하므로, **“짧아졌으니 repair 문제 해결”은 아닙니다**. 연속 blade는 합치고, 실제 불연속 경계는 격리하며, 잘린 발화는 unresolved로 남겨야 합니다.

규칙 개정은 다음처럼 좁히는 편이 안전합니다.

> **규칙 4:** 레거시 전사 입력은 계속 전체를 빈틈없이 덮는다. 프로젝트 자막 입력은 명시적으로 선택된 타임라인 revision의 가청 source 범위를 정해진 샘플 환산으로 도출한다. 전사·repair는 그 요청 범위를 빠짐없이 처리하며, VAD·빈 응답·오류를 이유로 입력을 추가로 제외하지 않는다. 자동 컷 후보는 승인된 자막 입력과 구분한다.

> **규칙 9:** 원본 미디어·전사 기록은 보존한다. 파생 영상의 제거는 source/instance 범위, 근거, 실패 상태, 복원 위치와 검토 상태를 기록한 EditPlan으로만 표현한다. 미처리·실패·경계 잘림은 빈 성공으로 바꾸지 않는다. 자동 컷의 미검토 상태는 후속 자막 생성으로 해제되지 않는다.

“영상 자체가 없으니 자막 손실이 아니다”는 수정 근거로 부적절합니다. **오검출된 발화를 먼저 영상에서 지우면 그 뒤 전사에서는 손실을 발견할 기회까지 없어집니다.** 규칙 완화가 아니라 파생 편집의 별도 권한·감사 계약이 필요합니다. 현재 원문은 [CLAUDE.md:43](/Users/ssojux2/Working/vlogsrt/CLAUDE.md:43), [CLAUDE.md:71](/Users/ssojux2/Working/vlogsrt/CLAUDE.md:71)입니다.

