# 설계 결정 기록

구현하며 내린 판단과 그 근거. 나중에 "왜 이렇게 했지?"가 나올 만한 것만 남긴다.

---

## D1. `docs/PLAN.md` 없이 구현했다

`CLAUDE.md`와 `TASKS.md`가 `docs/PLAN.md`의 섹션을 참조하지만 그 파일은 저장소에 없다.
전체 구현은 `TASKS.md`의 산출물·DoD·필수 테스트 목록과 `CLAUDE.md`의 불변 규칙만으로 했다.

**영향**: PLAN에만 적혀 있었을 세부 규칙(예: §6.6 고아 처리의 정확한 절차,
§6.8 이상치 검사 항목 전체 목록)은 이 문서의 판단으로 대체했다. 아래 D4, D6이 그것이다.
PLAN이 나타나면 이 세 항목을 대조해야 한다.

---

## D2. 문서상 시그니처에서 벗어난 곳 (2군데)

| 함수 | 문서 | 구현 | 이유 |
|---|---|---|---|
| `snap_to_word_boundary` | `(cut_a, text_a, words, boundary_time)` | `+ cfg` | `snap_max_distance`가 필요하다. 6을 코드에 박으면 불변 규칙 6 위반이다 |
| `align(w, a)` | 그대로 유지 | `build_alignment(w, a)` 추가 | opcode만으로는 index map을 얻을 수 없다. `align`은 문서대로 남겨뒀다 |

---

## D3. `Cue.text`는 속성, `text_raw`는 두지 않았다

문서는 `cue.text_raw`를 언급하지만, 줄 목록(`lines`)에서 한 줄로 잇는 속성 하나면 충분했다.
필드를 둘로 나누면 둘이 어긋날 수 있다. `cue.text`가 그 역할을 한다.

---

## D4. 고아 처리는 "전체 골격 재사상"으로 풀었다

**문제**: `build_w`는 `verdict != ok` 세그먼트를 뺀다. 그러면 그 시간대의 gpt 텍스트는
정렬에서 `insert` 블록이 되어 이웃 큐에 통째로 흡수된다. 텍스트는 살아남지만 **엉뚱한 시각에 뜬다.**

**해법**: `rescue_orphans`가 제외 세그먼트를 **포함한** W로 한 번 더 정렬한다
(`build_w(spine, include_all=True)`). 제외 세그먼트가 차지한 A 구간을 잘라내
고아 큐로 만들고, 그 텍스트가 이웃 큐에 흡수돼 있으면 거기서 뺀다(중복 방지).

고아 큐의 시각은 제외 세그먼트 자신의 `start`/`end`다 — 추정이지만 근거 있는 추정이다.
`conf`는 `orphan_conf_max`(0.30)로 고정하고 `time_estimated` 플래그를 단다.

**한계**: 이웃 큐에서 텍스트를 뺄 때 부분 문자열 일치를 쓴다. 같은 문장이 두 번 나오면
엉뚱한 쪽에서 뺄 수 있다. `tests/test_mapping.py::test_orphan`이 중복 없음을 검사한다.

---

## D5. `insert` 블록의 절단점은 `j1`로 보낸다

`map_position`이 `insert`/`delete` 블록 안에 떨어지면 `j1`을 돌려준다.
다만 자모 단위 정렬에서는 `insert` 블록이 음절 중간에 생기는 일이 흔해,
실제 결과는 어느 쪽으로든 한 음절 이내에서 갈린다. 그 뒤 어절 스냅이 정리한다.

`tests/fixtures/synthetic/map_cases.json`이 16개 케이스의 결과를 못 박아 뒀다.
이 값이 바뀌면 정렬 동작이 바뀐 것이다.

---

## D6. 이상치 검사 항목 (PLAN §6.8 대체)

| 수준 | 코드 | 뜻 |
|---|---|---|
| ERR | `bad_range` | `end <= start` |
| ERR | `empty_text` | 텍스트 없음 |
| ERR | `overlap` | 앞 큐와 겹침 |
| ERR | `too_many_lines` | `max_lines` 초과 |
| ERR | `unknown_seg` | 골격에 없는 `seg_id` |
| WARN | `line_too_wide` `too_short` `too_long` `cps` `tight_gap` `low_conf` `no_origin` `out_of_spine` | 기록만 |
| INFO | `orphan` `flag:*` | 참고 |

`ERR`은 파이프라인이 고친다(시간 보정 → 충돌 해소 → 빈 큐 제거).
정상 실행에서 `ERR`은 0이어야 한다.

---

## D7. 부동소수 여유값 `EPSILON = 1e-6`

`min_gap = 0.08`을 정확히 맞춘 큐가 `0.0799999...`로 계산돼 위반으로 잡혔다.
`validate`와 `evaluate`의 임계값 비교에만 여유값을 준다. **임계값 자체는 그대로다.**

---

## D8. 오프라인 실행은 영상 파일 없이도 돈다

`{stem}.meta.json`에 `duration`과 `silences`를 저장해 둔다. 오프라인이면 이걸 먼저 보고,
있으면 ffmpeg를 아예 부르지 않는다. 골든셋 영상(`golden/*.mp4`)은 `.gitignore` 대상이라
저장소를 새로 받은 사람도 픽스처만 있으면 파이프라인을 돌려볼 수 있다.

---

## D9. 출력 파일을 두 이름으로 쓴다

`TASKS.md`가 `eval/b0/ep01.srt`(회차 이름)와 `out/output.review.srt`(고정 이름)를
둘 다 DoD에서 참조한다. 양쪽을 만족시키려고 같은 내용을 두 이름으로 쓴다.
`<stem>.srt`가 정본이고 `output.srt`는 별칭이다.

---

## D11. gradio UI는 패키지 **밖**에 둔다

불변 규칙 7은 의존성을 `openai`, `tomli-w`, `rich` 셋으로 못 박는다. 웹 UI 요청이
들어왔을 때 선택지는 셋이었다.

1. `vlogsrt/`에 gradio를 넣는다 → 규칙 7 위반. 핵심 파이프라인이 무거워지고,
   `make check`가 UI 설치 여부에 흔들린다
2. UI를 만들지 않는다 → 사용자의 실제 요구를 거절하는 것
3. **UI를 `ui/`에 두고 선택적 extra로 만든다** ← 선택

`ui/`는 vlogsrt를 라이브러리로 쓰고, `vlogsrt/`는 gradio를 모른다.
`[project.dependencies]`는 그대로 3개이며, gradio는 `[project.optional-dependencies] ui`에 있다.
`scripts/check_invariants.py`가 `vlogsrt/`의 gradio import를 금지 목록으로 막는다.

`ui/service.py`(순수 로직)와 `ui/app.py`(화면)를 가른 이유도 같다.
service는 gradio를 import하지 않아 UI 없이도 테스트가 돈다
(`tests/test_ui_service.py`, 23개).

### gradio 6에서 부딪힌 것

- `theme`/`css`가 `Blocks()`에서 `launch()`로 옮겨졌다
- `launch(show_api=...)`가 사라졌다
- `gr.Video`의 `postprocess`가 더 이상 `(video, subtitle)` 튜플을 받지 않는다
  (독스트링에는 남아 있지만 코드에는 없다). `gr.update(subtitles=경로)`도
  경로 문자열을 그대로 보내 프런트가 못 읽는다. **컴포넌트 인스턴스를 돌려주면**
  생성자가 SRT를 VTT로 바꿔 FileData로 감싸준다 — 그 경로를 썼다
- `no_site_packages = true`를 켰다. ui extra가 numpy를 끌어오는데 numpy 스텁이
  3.12 문법이라 `python_version = "3.11"` 설정으로는 파싱조차 실패했다.
  이제 mypy 결과가 무엇이 설치돼 있는지에 흔들리지 않는다

---

## D10. 검증하지 못한 것

다음은 **골든셋과 실제 API 픽스처가 있어야** 판정할 수 있다. 현재는 미검증이다.

- 관문 1·2·3, 최종 관문 (전부 `docs/DASHBOARD.md` 참조)
- T1.6 `srt` vs `segments[]` 동등성 — `response_format="srt"` 픽스처가 필요하다
- 청크 분할의 실제 동작 — 24MB를 넘는 실제 오디오가 필요하다
  (분할 계획 로직은 `tests/test_preprocess.py`에서 순수 함수로 검증했다)
- `LiveTranscriber`의 재시도 경로 — 실제 429/5xx 응답이 필요하다
- UI의 유료 버튼(녹화) — 무료 경로와 차단 로직은 실제 서버를 띄워 확인했지만,
  실제 API를 부르는 경로는 못 돌려봤다
