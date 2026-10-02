# 원본 영상 브라우저 v2 — 워크플로 RUNBOOK

계획: `docs/plans/2026-10-02-raw-video-browser-v2.md`. 이 문서는 **지금 브랜치에서
실제로 실행되는 것만** 적는다 — 계획이 그리는 전체 흐름(SD 카드 가져오기 → 원본
시간순 조각 → 편집용 폴더 확정 → 최종 영상 정밀 자막) 중 아직 명령이 없는 단계는
[대기 중인 통합](#대기-중인-통합--명령이-없는-단계)에 그대로 남긴다. 없는 명령을
만들어 적지 않는다.

수동 인수 절차와 미실행 항목은 `docs/reports/2026-10-02-raw-video-manual-acceptance.md`가
마는 곳이다.

## 현재 상태 (main 1b7283d 기준 — 이 브랜치에 A5/A6 검토 백엔드가 더해졌다)

| 단계 | 상태 | 어디서 |
|---|---|---|
| 작업공간 만들기·열기 | 실행 가능 | UI 화면 1 · `vlogsrt media init` |
| SD/폴더 조사·복사·검증(A2) | 실행 가능 | UI 화면 1 · `vlogsrt media import` |
| 전체 원본 순서 확정 | 실행 가능 | UI 화면 1 버튼(전용 CLI 동사는 아직 없다) |
| 결정적 분할 계획·추출·조각 목록(A3) | 실행 가능 | UI 화면 2 · `vlogsrt media plan/extract/clips` |
| 의미 분석 Luna(A4) | 실행 가능 — 1b7283d에서 승인·병합(전용 CLI 동사는 없다) | UI 화면 2 분석 패널 · 서비스 계층 |
| 조각 검토·경계 편집·VTT(A5) | **라이브러리·백엔드만** — 화면·CLI 대기 | `segment_review.py`·`review_projection.py`·`review_workspace.py` |
| 편집용 폴더 확정·캐시 정리(A6) | **라이브러리·백엔드만** — 화면·CLI 대기 | `confirmed_package.py`·`cache_policy.py`·`review_workspace.py` |
| 최종 자막(A7) | **라이브러리만**(A7-3 13ad043 승인·병합) — 화면 4는 읽기 전용, 버튼 비활성 | `final_video.py`·`final_caption.py`·`caption_review.py` 등 |

아래 시험 수치의 702 passed는 f939478 당시의 역사 기록이고, 이 문서 개정
시점(1b7283d + 검토 백엔드)에는 `pytest tests/raw`가 **805 passed**다. 현재
살아 있는 화면 3은 package 경로 표시·새로고침뿐이고 화면 4는 설정 요약뿐이다.
둘 다 정직하게 비활성 상태로 남아 있다.

## 설치

```bash
make venv                     # .venv 생성 + 의존성 설치
source .venv/bin/activate
pip install -e ".[ui]"        # 브라우저 UI(선택이지만 이 워크플로의 본체)
```

`ffmpeg`·`ffprobe`가 PATH에 있어야 한다(`brew install ffmpeg`). 조사·복사·계획·
추출은 전부 로컬이고 **API 키가 필요 없다** — `.env`는 그대로 둔다
(`VLOGSRT_OFFLINE=1` 기본값 유지).

## 브라우저 띄우기

```bash
make ui                       # http://127.0.0.1:7860
```

- 루프백 전용이다. `--share`는 거부되고 외부 공유 길은 없다(계획 §4.1).
- 포트가 차 있으면 옆 번호로 비켜 뜨고 다른 실행 중 UI를 건드리지 않는다.
- 브라우저를 자동으로 안 열려면 `python -m ui.app --no-browser`, 포트는 `--port`.

## 합성 오프라인 재현 — 유료 호출 없이 전 단계 굴리기

두 가지가 다 오프라인·합성 미디어로만 돈다.

### 1. 시험 묶음(가장 빠른 재현)

```bash
pytest tests/raw              # 합성 MP4(ffmpeg lavfi)·기록된 응답만 쓴다
```

2026-10-02 f939478에서 702 passed(약 61초, 역사 기록). 같은 날 main 1b7283d +
검토 백엔드 시점에는 **805 passed**(약 106초). 가져오기 중복·복사 실패 재개·경로
검문·순서·추출 재개·검토 리비전·검토 백엔드 종단·확정 내보내기·캐시 보호·최종
자막 직렬화까지 이 안에 있다.

### 2. 진단 CLI 종단(실제 명령 흐름)

`/tmp`에 합성 카드를 만들어 CLI로 init → import → plan → extract → clips까지
돈다. 전부 로컬이고 네트워크가 없다.

```bash
mkdir -p /tmp/card   # ffmpeg는 폴더를 만들지 않는다 — 먼저 만든다
# 합성 카드 — 검은 화면 2초 MP4 두 개. 촬영 시각은 컨테이너 creation_time에
# 명시적인 ISO-8601(시간대 포함)으로 적는다 — mtime은 순서 근거가 아니다.
ffmpeg -y -f lavfi -i color=black:s=64x32:r=10:d=2 -f lavfi -i anullsrc=r=8000:cl=mono \
  -shortest -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac \
  -metadata creation_time=2026-10-02T09:00:00+00:00 /tmp/card/clip_a.mp4
ffmpeg -y -f lavfi -i color=black:s=64x32:r=10:d=2 -f lavfi -i anullsrc=r=8000:cl=mono \
  -shortest -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac \
  -metadata creation_time=2026-10-02T09:05:00+00:00 /tmp/card/clip_b.mp4

vlogsrt media init /tmp/ws --name 문서확인
vlogsrt media import /tmp/card --workspace /tmp/ws            # 제안만 인쇄(쓰기 없음)
vlogsrt media import /tmp/card --workspace /tmp/ws --confirm  # 순서 확정 + 복사·검증
vlogsrt media inspect --workspace /tmp/ws
```

촬영 시각 계약: 조사자는 컨테이너가 선언한 `creation_time`을 **유효할 때만**
(ISO-8601·시간대·상식 bound를 통과할 때만) 순서 근거로 쓴다. 값이 없거나 못
미치는 실물 카드에서는 순서를 **사람이 확정 대화에서 명시적으로 확인한다**
(`user_confirmed`) — 파일 수정 시각(mtime)으로 시간순을 메우는 길은 없다.

여기서 `plan`으로 넘어가려면 **전체 원본 순서 확정**이 필요하다. 이 확정은 지금
UI(화면 1의 `전체 원본 순서 확정` 버튼)와 서비스 계층에만 있다 — 전용 CLI 동사는
아직 없다. 진단으로는 서비스를 직접 부른다:

```bash
python - <<'EOF'
from pathlib import Path
from vlogsrt.project.raw.service import RawWorkspaceService
revision = RawWorkspaceService().confirm_global_source_order(Path("/tmp/ws"))
print(revision.revision_id, len(revision.entries))
EOF
```

```bash
vlogsrt media plan --workspace /tmp/ws        # 결정적 분할 계획(기본 타일 30초)
vlogsrt media extract --workspace /tmp/ws     # 로컬 ffmpeg 추출, 프로파일 기본 source_matched
vlogsrt media clips --workspace /tmp/ws       # 조각 목록 + 전달물(clips.jsonl·CSV·README) 경로
```

`extract`는 `--profile source_matched|edit_prores`, `--handle-before/--handle-after`
(초, 유리수), `--plan`(리비전 지정), `--wait-seconds`를 받는다. 결과는
`clips/package_000001/` 아래 `NNNNNN__t…-t…__c_….mp4` 파일과 전달물로 게시된다.

## CLI 참조 — `vlogsrt media`

| 명령 | 하는 일 |
|---|---|
| `init PATH --name` | 빈 폴더에 v2 작업공간 생성(9개 표준 디렉터리 + project.toml/sqlite3) |
| `import SOURCE --workspace WS [--confirm]` | 카드/폴더 조사 → 제안 인쇄. `--confirm`이면 순서 확정 + 로컬 복사·SHA 검증 |
| `retry --workspace WS --job JOB` | 실패·중단된 **가져오기** 작업의 명시적 재시도(부분 진행 이어서) |
| `inspect --workspace WS [--batch]` | 자산·순서 리비전·작업 상태 인쇄 |
| `plan --workspace WS [--tile-seconds S \| --intervals FILE]` | 확정된 전체 순서 위 결정적 분할 계획(로컬만) |
| `extract --workspace WS [--plan --profile --handle-* --wait-seconds]` | 계획을 실제 조각 폴더로 추출(로컬 ffmpeg만) |
| `clips --workspace WS [--package]` | package 조각 목록·전달물 경로 인쇄 |

추출 작업 재시도는 UI(화면 2 `재시도`)와 서비스 계층에 있다 — CLI `retry`는
가져오기 전용이다.

## 대기 중인 통합 — 명령이 없는 단계

아래는 **라이브러리·백엔드와 시험은 브랜치에 있지만** 공개 서비스·화면·CLI로
아직 열리지 않은 것들이다. 이 문서는 실행 가능한 명령을 만들어 적지 않는다.

- **A5 조각 검토** — 경계 이동·분할·인접 병합·사용/제외 리비전, 검토용 VTT 투영.
  `segment_review.py`·`review_projection.py` + `tests/raw/test_segment_review.py`·
  `test_review_projection.py`. 검토의 **지속 작업공간 백엔드**(초기화·stale-head
  편집·재계획 환원·측정 VTT·확정 내보내기·관리 정리·미리보기 복원)는 이제
  `review_workspace.py`·`review_workspace_store.py` +
  `test_review_workspace.py`로 이 브랜치에 있다 — W1이 공개 서비스·화면으로
  연다(작업 제출·불투명 ID 등록·Finder 열기는 W1 소유).
- **A6 편집용 폴더 확정** — 검증된 조각을 `clips/confirmed_*`에 불변 복사(게시·
  멱등 검증), 캐시 정리(`cache/`·`previews/`만, 보호 이름 거부).
  `confirmed_package.py`·`cache_policy.py` + `test_confirmed_package.py`·
  `test_cache_policy.py`. 내보내기 리비전 기록은 검토 백엔드가 소유하고 W1이
  신원 등록·중앙 DB 마이그레이션(v6)·Finder 열기를 연다.
- **A7 최종 자막** — 최종 영상 등록→문구/시간 모델 명시적 승인→SRT.
  `final_video.py`·`final_caption.py`·`caption_provider.py`·`caption_artifacts.py`
  (+ 승인·병합된 `caption_review.py` 등 A7-3) + 시험. 화면 4의 `정밀 자막 생성`·
  `SRT 내보내기` 버튼은 `interactive=False`다 — W1 통합 대기.

## 지키는 선 (이 워크플로의 안전 규칙)

- 가져오기·계획·추출은 **유료 호출이 없다**. 유료 호출이 가능한 길은 승인 게이트를
  지나야만 열리고, 지금 이 문서의 어떤 명령도 그 게이트를 거치지 않는다.
- 원본은 `originals/`에 읽기 전용으로 보존된다. 추출·확정은 복사본을 만들 뿐이다.
- `clips/confirmed_*`와 보호 이름(originals·final·subtitles·review·analysis·jobs·
  config·project DB)은 캐시 정리 대상이 아니다(`cache_policy.py`가 검문한다).
- **AI Rough Cut Final Cut 플러그인(com.ssojux2.vlogsrt.workflow)은 사용자 요청으로
  비활성되어 있다** — 재활성화·재설치하지 마라. 상세는 수동 인수 보고서 참조.
