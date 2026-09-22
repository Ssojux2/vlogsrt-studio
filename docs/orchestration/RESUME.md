# 재개 지점 — 2026-09-16 작업 중단

> 사용자가 이동으로 작업을 중단시켰다. 재개할 때 **이 파일부터 읽어라.**
> 다 끝나면 이 파일을 지운다.

## 지금 어디까지 왔나

작업 라인: worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`,
브랜치 `Ssojux2/fcp-prototype` @ **`8ca214d`** (트리 깨끗, **pytest 2488 passed**,
불변식 20, ruff·mypy 통과 — 코디네이터가 직접 재측정).

문서 라인: `feat/final-cut-project` @ **`6136638`**.

**러너가 완성됐다.** 세 단계가 다 있고 아직 아무도 부르지 못한다:

```python
prepare_audio(project_path, artifact_dir, *, overrides, policy, config, project_config)  # PROTO-2 d8eed17
analyze_audio(prepared, *, transcriber, session=None)                                    # PROTO-3 7cae54d
publish_project(analyzed, *, vision_client, destination, online=False, dtd_path=None)     # PROTO-4 8ca214d
```

| | | |
|---|---|---|
| PROTO-1 / 1b | 거짓 성공 차단 / 낡은 장면 6개 | ✅ `83dcc57` · `3b7337b`·`03e22ae` |
| PROTO-2 / 3 / 4 | 러너 1·2·3 of 3 | ✅ `d8eed17` · `7cae54d` · `8ca214d` |
| **PROTO-5** | `project/commands.py` + `cli.py` — **비용 경계** | ⏸ **여기서 멈췄다** |
| PROTO-6 | e2e + `docs/RUNBOOK.md` | 📋 |
| PROTO-7 | **실물 FCPXML 실행** | 🧑 사람 |

## 재개할 때 할 일 — 순서대로

### 1. (B) GLM 방향 검토 결과를 회수한다

중단 시점에 **(B)가 아직 돌고 있었다**(pid 16517, `claude-glm ... --permission-mode plan`).
출력은 여기다:

```
/private/tmp/claude-501/-Users-ssojux2-Working-vlogsrt/6e235c15-3468-4728-b95c-00682e29a5a2/scratchpad/dr-proto5-glm.txt
```

**`/private/tmp`라 재부팅이면 날아간다.** 비어 있거나 없으면 재디스패치한다:

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
SP=<위 scratchpad 경로>
env -u ANTHROPIC_API_KEY claude-glm --effort max --permission-mode plan \
  -p "$(cat $SP/dr-proto5-input.md)" > $SP/dr-proto5-glm.txt 2>&1
```

입력 파일(`dr-proto5-input.md`)도 같이 날아갔으면
`docs/orchestration/direction-review-charter.md` + `docs/orchestration/tasks/proto-5-cli.md`를
이어 붙이고 끝에 "보고서 전문을 stdout에 출력하라" 한 단락을 더하면 된다(헌장에 적혀 있다).

### 2. (A)의 UNSOUND를 판정한다 — **아직 반영 안 했다**

(A)는 이미 끝났고 `docs/orchestration/reviews/opus-proto-5-direction.md`에 아카이브했다.
**판정 UNSOUND.** 지적 둘:

1. **§3-a의 처방이 `--offline`을 지키지 못한다.** 브리프는
   `transcribe.get_transcriber(cfg, online=online)`을 "이 함수가 비용 안전장치다"라며
   그대로 쓰라고 했는데, 그 함수는 `force_offline`이 거짓이면
   `not online and env.is_offline()`을 본다 — 즉 `--offline`을 명시해도 `.env`의
   `VLOGSRT_OFFLINE`이 `0`이면 픽스처로 안 간다는 지적으로 보인다. **전문을 읽고
   코드로 확인한 뒤 판정하라.**
2. **종료 코드 표에 필요한 값이 러너 반환 타입에 없다.** `PublishedProject`에서
   `final_accepted`를 어떻게 꺼내는지 확인해야 한다 —
   `package: ExportPackage`가 그 값을 싣는지, 아니면 review.json을 다시 읽어야 하는지.

**두 보고서를 다 받은 뒤** 겹침·불일치를 정리해 브리프를 v2로 고치고, §9에 기록을
남긴다(PROTO-2·3·4와 같은 형식). 뒤집힌 내 주장은 반드시 표로 남긴다.

### 3. 그 다음에야 PROTO-5를 디스패치한다

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
env -u ANTHROPIC_API_KEY claude-glm --effort max --permission-mode auto \
  -p "$(cat /Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/proto-5-cli.md)
<구현자 꼬리말 — PROTO-4 디스패치와 같은 문구>"
```

## 이 머신에서 반드시 기억할 것

- **Xcode 라이선스 미동의로 툴체인 shim이 죽어 있다**(exit 69): `git`·`python3`·`make`.
  - git은 **`/Library/Developer/CommandLineTools/usr/bin/git`** (2.50.1, 정상).
  - 파이썬은 **`.venv/bin/python`**.
  - **`make check`를 부르지 마라** — 각 브리프 §7의 여섯 명령을 개별로 돌린다.
  - **`xmllint`는 정상이다**(rc 0). `/usr/bin` 전체가 죽은 것이 아니다.
  - 사람이 고치려면: `sudo xcodebuild -license accept`.
- **메모리가 빡빡하다**(여유 0.8~1.9GB). 시스템이 모자라면 코디네이터의 `sleep` 대기
  루프를 먼저 죽인다 — **워커는 살아 있다.** 알림이 안 오면 `ps -p <pid>`로 확인하라.
  워커 둘을 동시에 띄우기 전에 `top -l 1 -n 0 | grep PhysMem`을 보라.
- **`.env.example`에 사용자 소유 미커밋 변경이 있다.** 읽지도 스테이징하지도 되돌리지도
  마라. `git add`는 항상 경로를 명시한다.

## 절대 잊으면 안 되는 규율

- **픽스처 수정은 별도 승인 항목이다.** Q1B에서 픽스처를 구현자 소유에 넣었다가
  레일이 실데이터에서 무력화됐고 게이트는 그걸 잡지 못했다.
- **단언을 약화해 테스트를 통과시키지 않는다.** 장면 구성을 고치는 것과 단언을
  무르게 하는 것은 다르다(PROTO-1b가 그 선을 지킨 예다).
- **보고서를 결론으로 받지 않는다.** PROTO-2·3·4에서 매번 뮤테이션으로 레일이 실제로
  무는지 직접 확인했고, 매번 값이 있었다.
- **방향 검토는 두 갈래**다 — (A) 컨텍스트 분리 Opus, (B) GLM. 헌장은
  `docs/orchestration/direction-review-charter.md`. 한쪽만 돌리면 매번 한 묶음을 놓쳤다.

## 파킹된 것 (손대지 않는다)

- **Q1/Q1B repair 레일** — `Ssojux2/q1b-repair-rails` @ `899efbc`. Fable UNSOUND,
  정정 7건 중 2건은 **유료 재실행**이 있어야 닫힌다.
- R-01~R-09(순서 역전 본체, R-00만 완료), P7 재작성, X-01~X-06(Workflow Extension).
- **PENDING_HUMAN**: fr_day_3 유료 재실행, 실제 FCP import/relink, 77분 미디어,
  X-01 스파이크, 공증 앱 설치, 희미한/음악 아래 발화의 실제 검출률.
