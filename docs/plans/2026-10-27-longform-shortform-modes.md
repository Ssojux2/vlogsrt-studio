# 롱폼·숏폼 동시 처리 — 가로/세로 모드 · 세로 자막 다시 넣기(ko·en) · 라이브러리 맥락 평가 (W-37–W-39, D97–D99)

## Context
사용자는 지금 롱폼(가로) 중심인 작업대를 **롱폼과 숏폼을 함께 다루는 형태**로 바꾸려 한다(2026-10-27 지시). 설계는 Fable 5.1(high) 계획 에이전트가 맡았다.
- **가로 모드(롱폼)**: 지금 흐름 그대로다. 한국어 자막 캡션(`SRT?captionFormat=SRT.ko`)을 실어 Final Cut으로 보내고, Final Cut에서 컷 편집한 뒤 Subtitle Edit에서 SRT 작업을 한다.
- **세로 모드(숏폼)**: 첫 단계는 같다(한국어 자막 생성 → 편집 → 한국어 영상과 SRT 내보내기). 그다음 Subtitle Edit에서 ko·en SRT를 만들고, 그 둘을 **사용자가 편집한 프로젝트**에 타이밍이 어긋나지 않게 바로 얹는다.
- **추천 확인을 전체 맥락으로**: 클립 단위 판정에 더해, Gemini가 라이브러리 전체(분석 요약 + 자막)를 보고 맥락에서 평가한 결과를 등급에 반영한다.

**사용자가 정한 것**
- 모드는 라이브러리마다 하나다. 불러온 영상 가운데 많은 방향을 따르고, 사람이 바꿀 수 있다.
- 세로 자막 형식: 지금의 SRT → 타이틀(D91)과 같은 방식이되, Basic Title 대신 Final Cut 내장 **Subtitle** 템플릿을 쓴다. 템플릿 위치: `…/METemplates.localized/Titles.localized/Subtitles.localized/Subtitle.localized/Subtitle.moti`. 게시된 매개변수는 Font · Font Size · Text Color · Background · X/Y Position Offset · Vertical Social Media Safe 등이다.
- 프로젝트 선택: Final Cut에서 내보낸 `.fcpxml`(또는 `.fcpxmld`)을 고른다. 결과는 원래 편집을 그대로 두고 자막만 얹은 **새 프로젝트**다.
- 두 언어: 언어마다 타이틀을 따로 둔다(각자 레인, 같은 템플릿). 위치만 다르게 해서 **영어가 아래(기본 자리), 한국어가 그 위**에 온다. SRT마다 제 타이밍을 쓴다.
- 맥락 평가: **등급에 반영**하고, 범위는 **라이브러리 전체**다. 유료 Gemini 호출이므로 견적을 보고 승인한다(D44).

**순서**: W-37(모드) → W-39(맥락 평가)와 W-38(세로 다시 넣기)을 병행한다. W-38은 사람 단계 H-B1·H-B2(무료 픽스처)가 먼저 와야 한다. 세 트랙 모두 1단계 전사 경로(W-35)를 건드리지 않는다.
**지금 보류 중인 일**: W-35a(단어 배정 모듈)는 전체 시험이 통과했고(pytest 0), 계획 모드라 커밋하지 못했다. 승인되면 먼저 커밋·push한다.

---

## A. 모드 — W-37, D97

**결정**
- 다수는 **영상 개수**로 센다. 길이로 세면 긴 가로 B롤 하나가 숏폼 라이브러리를 뒤집기 때문이다. 동점이면 가로로 한다(설정).
- 방향은 `finalcut_timeline.orientation_of(width, height)`로 정한다(D66과 같은 정의). `SourceAsset.width/height`는 가져올 때 회전을 반영한 표시 크기다.
- 모드는 **표지와 흐름 게이트일 뿐 원본을 제한하지 않는다**.
  - 빌더의 D66 투표(`_sequence_orientation`)는 그대로 둔다. 그래서 기존 가로 라이브러리의 FCPXML 바이트는 바뀌지 않는다.
  - 투표 결과가 모드와 다르면 리포트 메모와 화면 경고 한 줄만 남긴다.

**W-37a — 순수 계층 + 서비스 + CLI**
- 새 파일 `vlogsrt/project/raw/workspace_mode.py`(순수): `decide_mode(orientations, settings) -> ModeDecision(mode, landscape_count, portrait_count, basis)`.
- `raw/config.py`: `WorkspaceModeSettings(tie)`, 상수 `MODE_LANDSCAPE` · `MODE_PORTRAIT` · `MODE_AUTO`.
- `service.py`:
  - 메타 키 `workspace_mode`(`auto|landscape|portrait`, 기본 auto).
  - `workspace_mode(root)` / `set_workspace_mode(root, value)`. `finalcut_drop_disabled` 패턴(service.py ~:1551)을 따른다.
  - 지문·캐시 열쇠·승인·견적에는 넣지 않는다.
- CLI `media mode --workspace [--set …]`. 보내기 리포트에 `mode` 칸과 불일치 메모를 더한다.
- 불변식: `workspace_mode.py`는 순수하다. `plan()`과 `render()`는 모드를 받지 않는다(모드는 문서 바이트를 바꾸지 못한다).
- 시험 `tests/raw/test_workspace_mode.py`: 개수 다수, 동점, 크기 없음, override, 메타 왕복, 가로 라이브러리 바이트 동일.

**W-37b — 화면 + D97**
- 「Final Cut 보내기」 아코디언(`_finalcut_drop_panel`, ui/workspace_service.py ~:4800)에 라디오 「모드: 자동(가로 N · 세로 M) / 가로 / 세로」를 두고, 머리줄에 「가로 모드」·「세로 모드」 배지를 단다.
- 세로 모드일 때만 「자막 다시 넣기 (세로)」 단추가 보인다(연결은 W-38c).
- 렌더 골든(`test_render_cost`)은 배지가 들어가는 자리만 다시 잠근다.
- D97 기록, CLAUDE.md 「제품 표면」 갱신, TASKS W-37.

---

## B. 세로 — 편집한 프로젝트에 SRT(ko·en) 다시 넣기 — W-38, D98

**사람 선행 단계(무료)**
- **H-B1**: Subtitle 타이틀 하나를 얹은 프로젝트를 Final Cut 12.4에서 XML로 내보낸다. 매개변수 `param key` 철자를 잠그는 데 쓴다. Basic Title 열쇠(finalcut_titles.py:49-55)도 이렇게 배웠다.
- **H-B2**: 실제로 편집한 세로 프로젝트를 `.fcpxml`과 `.fcpxmld` 두 형식으로 내보낸다. 타이틀 · 전환 · 연결 클립 · compound(ref-clip) · gap · 리타임을 하나씩 넣고, tcStart가 0이 아닌 변형도 하나 만든다. 같은 프로젝트에서 Export Captions(SRT)도 한 파일 받는다. 이것으로 「SRT 0초 = 시퀀스 시작」(tcStart 포함 여부)을 확정한다.

**W-38a — 사용자 프로젝트를 느슨하게 읽기(순수)**
- 새 파일 `raw/finalcut_project_read.py`. 엄격한 `fcpxml.parse_project`는 그대로 둔다.
- 재사용: `fcpxml.resolve_info_path`(`.fcpxmld` → Info.fcpxml), `timebase.parse_fcpx_time` · `format_fcpx_time` · `frame_nearest`.
- 받는 범위:
  - FCPXML 1.14만 받고, `project`는 정확히 하나여야 한다.
  - 시퀀스의 format(프레임 길이·크기), tcStart, duration을 읽는다.
  - 스파인 직속 `asset-clip · clip · ref-clip · sync-clip · mc-clip · gap`은 시간 항목으로 읽는다. `transition`은 세기만 하고, audition은 활성 자식으로 본다.
  - **그 밖의 모든 요소는 읽지 않고 그대로 통과**시킨다.
- 시각 T를 덮는 항목에 닻을 둔다.
  - gap이면 gap에 두고 `in_gap`으로 센다.
  - 리타임(`timeMap`)된 클립과 `scaleEnabled="1"`이면서 레이트가 다른 클립은 닻으로 쓰지 않고 `unplaceable_retimed`로 센다. 시각을 유도하지 않는다(규칙 1).
  - 그 밖에는 D91 `_local_start` 규칙(클립 안 시각 = start + (T − offset))을 그대로 쓴다.
- 레인: 프로젝트에서 쓰인 가장 큰 양수 lane을 기준으로 en = max+1, ko = max+2. 레인은 쌓이는 순서일 뿐이고, 화면 위치는 Y 오프셋이 정한다.

**W-38b — Subtitle 템플릿 두 레인 배치 + 쓰기**
- `raw/config.py` `FinalCutSubtitleSettings`:
  - 템플릿 이름·uid, 글꼴(Apple SD Gothic Neo Bold), 크기(높이 비율), 색, `Vertical Social Media Safe` 켬, 배경 불투명도.
  - 언어별 Y 오프셋: en = 0(아래), ko = 한 줄 위(글자 크기 × 1.4 / 높이 — 외부 근거 없는 조정 대상).
- 새 파일 `raw/finalcut_subtitle.py`(순수). 언어마다 다음 순서로 만든다.
  1. `finalcut_titles._snap_all`(시퀀스 격자) → `_one_lane`(겹친 줄은 레인마다 다음 줄 시작에서 자르고 센다).
  2. 닻을 찾는다.
  3. `SubtitlePiece`를 만든다. 글자는 `finalcut_export.caption_text`(D96)를 거친다.
  - `_snap_all`과 `_one_lane`은 공개 이름으로 바꾸기만 하고 동작은 그대로 둔다(D91 시험이 지킨다).
- 쓰기: `project/export.py:_edit_fcpxml_parts`처럼 **사용자 트리를 복사하고 덧붙이기만** 한다(규칙 9).
  - `resources`에 effect 하나를 더하고, 각 닻에 `<title lane offset start="3600s" …>` + param + text + text-style-def를 단다.
  - 프로젝트 이름에 「 · 자막 ko+en」을 붙이고 uid는 새로 만든다. 이벤트 이름은 그대로 둔다. 같은 라이브러리로 가져오면 미디어가 중복되지 않는다.
  - DOCTYPE을 되살린 뒤 설치된 DTD로 검증(`xml_validation.validate_xml`)한다. 실패하면 쓰지 않는다.
  - `finalcut_export._reserve` / `_publish` / `_validate`를 공용으로 뽑아 쓴다. 리포트에는 언어별 `placed · trimmed · outside · tails · in_gap · unplaceable_retimed · empty`를 싣는다.
- 시험(H-B2 픽스처): 두 언어 레인과 Y 위치, 한 언어만, 레인별 트림, 국소 시각, gap, **원본 노드가 보존되고 diff에는 덧붙인 노드만 있음**, DTD 통과, 왕복(각 타이틀이 SRT 시각과 ½프레임 안).

**W-38c — 서비스 · 화면 · CLI · D98**
- 서비스 `insert_project_subtitles(root, project, ko_srt, en_srt|None)`: 세로 모드가 아니면 거부한다. 작업 종류 `finalcut_subtitles`, 지문에 세 경로를 넣어 다시 시도할 수 있게 한다.
- `ui/chooser.py`: 새 종류 `PROJECT_KIND`(`fcpxml` · `fcpxmld`, 번들은 파일처럼 고른다). 순서대로 프로젝트 → 한국어 SRT(필수) → 영어 SRT(취소하면 없음).
- 단추 「자막 다시 넣기 (세로)」: 끝나면 Final Cut을 열고 결과 줄과 「같은 라이브러리로 가져오기」 안내를 보인다.
- CLI `media finalcut-subtitles --workspace --project … --ko … [--en …]`.
- D98: 사용자 프로젝트에 덧붙이는 쓰기 경로 · 레인 규칙 · 위치 · SRT 0초 기준 · 리타임은 세기만 한다.
- **H-B3**(Final Cut 인수, 무료): 미디어 중복 없음 · 한국어가 영어 위 · 임의 큐 5개의 시작이 1프레임 안 · 레이트가 섞인 클립 위의 큐.

---

## C. 라이브러리 전체 맥락 평가(Gemini) → 등급 반영 — W-39, D99

**설계 핵심**
- 순수 등급 계층(`tiering.py`)은 값만 받는다. `ContextSignals`가 D60의 `TimingSignals`처럼 두 번째 외부 값이 된다.
  - 생산자는 서비스의 `_tier_context` 한 자리뿐이다. 불변식 `check_tier_timing_only_from_service`를 넓혀 이것도 검사한다.
- **경계 · 빨강 · 보류는 못 건드린다.** 마지막 단계 `_contextualized`에서만 바꾼다.
  - 내리기: 녹색인데 `redundant`나 `off_topic`이고 확신이 문턱 이상이면 노랑으로 내리고 사유를 단다(예: 「맥락: 중복 — 0014 03:12와 같은 장면」).
  - 올리기: 노랑인데 `core`이고 확신이 `high`이며, 허용된 좁은 칸(`usable|good × speech_dense`, 다른 제한이 없는 경우)일 때만 녹색으로 올린다.
  - 사람 결정은 지금처럼 이긴다. 등급판은 `raw-tier/v7`로 올린다. 바뀐 등급은 D93(추천만 켜기)·D94(꺼진 클립 빼기)에 그대로 이어진다.
- **낡으면 쓰지 않는다**: `context_key` = sha256(촬영 순서의 원본 · 분석 revision · 증거 문서 sha · 원본 자막 리비전 sha + tier_version + 모델 + 안내문 판 `context-review/v1` + 설정). 열쇠가 다르면 「낡음 — 다시 평가」로 보이고 등급에는 쓰지 않는다. W-35b가 글자를 바꾸면 자동으로 낡는다.

**W-39a — 순수 평가 계층 + 등급 연결**
- 새 파일 `raw/context_review.py`(순수):
  - `build_input(sources_in_order)`: 원본마다 등급 구간(`span_id`) · 근거 · 창 요약과 태그 · 자막 줄을 모은다. 예산은 `ContextReviewSettings`이고, 줄인 양은 센다.
  - `payload(...)`: `build_gemini_payload`처럼 `responseJsonSchema`를 쓴다.
  - `parse(...)`: 모르는 id나 enum 밖 값은 거부한다.
  - `context_key(...)`.
- 출력 schema: `{storyline_summary, sources:[{asset_id, segments:[{span_id, verdict: core|supporting|redundant|off_topic, duplicate_of, confidence: low|medium|high, reason}]}]}`.
- `tiering.py`: `ContextSignals` · `UNKNOWN_CONTEXT` · `_contextualized` · `CAP_CONTEXT` · `LIFT_CONTEXT`. `RawTierSettings`에 `context_*` 문턱과 허용 칸을 더하고, 판을 v7로 올린다.
- Final Cut 키워드 「맥락 중복」·「맥락 벗어남」·「맥락 핵심」.
- 시험: 경계·빨강 불변, 내리기·올리기 표, 문턱, 거부, 예산 세기, 열쇠 민감도. 골든은 v7 표지만 다시 잠근다.

**W-39b — 서비스: 견적 · 승인 · 실행 · 저장**
- `context_quote(root)`: 토큰을 추정하고 `RawGeminiAnalysisProfile` 단가로 금액을 낸다. 작업공간 분석 모델이 Gemini가 아니면 Flash를 쓴다. `price_valid_through`가 지났으면 가격 미정으로 거부한다. digest = 열쇠 + 금액.
- `context_run(root, digest)`:
  - digest를 대조한다. 같은 열쇠의 성공 문서가 이미 있으면 **호출하지 않는다**.
  - 호출은 `transcribe.request_gemini_generate`(유일한 유료 문)로만 한다.
  - `analysis/context_review/<key>.json`과 영수증(`measure_gemini_usage`)을 남긴다. 실패하면 사유를 문서로 남기고 등급은 그대로 둔다.
- `_tier_context`: `read_scope` 메모로 한 번 읽는다(D63). 평가가 들어오면 정리 고리가 등급 표시만 다시 단다.
- 시험(가짜 Gemini): 견적 → 승인 → 반영 · 같은 열쇠는 0호출 · 자막이 바뀌면 낡음과 원복 · 실패 표시 · 가격 미정 거부.

**W-39c — 화면 + D99**
- 설정 서랍의 「맥락 평가 (Gemini)」에 다음을 둔다.
  - 견적 줄: 원본 수 · 토큰 · 금액
  - 「승인하고 평가」 단추(키가 없으면 안내)
  - 상태: 날짜 · 모델 · 지금/낡음 · 실패
  - 줄거리 요약
- 검토 조각에 「맥락: 중복·벗어남·핵심」 배지와 근거를 단다.
- D99 기록, CLAUDE.md 등급 문단과 `tier_*` 표 갱신.
- **H-C1**(유료, Flash로 약 $0.1–0.3): 실데이터 한 라이브러리를 평가하고, 중복 판정 10개를 표본 확인한다.

---

## 위험
- Subtitle `param key`는 H-B1 전에는 확정할 수 없다. Final Cut 업데이트로 템플릿이 바뀔 수도 있다.
- ET로 다시 직렬화하면 공백과 엔티티가 바뀐다. 시험은 노드 단위로 비교한다.
- SRT 0초 기준(tcStart 포함 여부)은 H-B2로 확정한다.
- 다른 라이브러리로 가져오면 미디어가 복사된다. 안내 문구로 알린다.
- 맥락 평가로 올라간 추천은 D93에 따라 켜져 나간다. 그래서 올리기 칸을 좁게 두고 `high`만 받는다.

## 검증
- 세션마다 `make check`(ruff · mypy · pytest · `check_invariants.py`)와 새 불변식 셋을 돌린다.
  - 모드는 순수하고 문서 밖에 있다.
  - 맥락 계층은 순수하고 생산자가 하나다.
  - 사용자 프로젝트 쓰기는 덧붙이기만 한다.
- 골든: D91 타이틀 문서와 가로 라이브러리 FCPXML은 바이트 그대로다. 렌더 골든은 배지와 v7 자리만 다시 잠근다.
- 사람 단계: H-B1 · H-B2(무료 픽스처) → H-B3(Final Cut 인수), H-C1(유료 1회).
