# 원본 영상 브라우저 v2 — 워크플로 RUNBOOK

계획: `docs/plans/2026-10-02-raw-video-browser-v2.md`. 이 문서는 두 묶음으로 나뉜다.

- **승인 기준선** — main 3cbf235에서 실제로 실행되는 것만 적는다.
- **후보 통합** — W1이 만든 네 화면 통합(b910fcc)을 승인 전 상태로 적는다.
  루트의 실측 브라우저 시험이 오기 전까지 "동작한다"고 단정하지 않는다.

수동 인수 절차와 미실행 항목은
`docs/reports/2026-10-02-raw-video-manual-acceptance.md`가 맡는다.

## 어디까지 왔나 (2026-10-03, main 3cbf235 기준)

| 단계 | 상태 | 어디서 |
|---|---|---|
| 작업공간 만들기·열기 | 실행 가능 | UI 화면 1 · `vlogsrt media init` |
| SD/폴더 조사·복사·검증(A2) | 실행 가능 | UI 화면 1 · `vlogsrt media import` |
| 전체 원본 순서 확정 | 실행 가능 | UI 화면 1 버튼(전용 CLI 동사는 없다) |
| 결정적 분할 계획·추출·조각 목록(A3) | 실행 가능 | UI 화면 2 · `vlogsrt media plan/extract/clips` |
| 의미 분석(A4) | 실행 가능 — 재생 전용 | UI 화면 2 분석 패널 · `vlogsrt media analyze` |
| 조각 검토·경계 편집·VTT(A5) | 백엔드 승인 — 화면·CLI 대기 | `review_workspace.py`·`segment_review.py`·`review_projection.py` |
| 편집용 폴더 확정·캐시 정리(A6) | 백엔드 승인 — 화면·CLI 대기 | `confirmed_package.py`·`cache_policy.py`·`review_workspace.py` |
| 최종 자막(A7) | 백엔드·라이브러리 승인 — 화면·CLI 대기 | `caption_workspace.py`·`final_video.py`·`final_caption.py` 등 |
| 네 화면 통합(검토·확정·자막 버튼) | 후보 b910fcc — 승인 전 | [후보 통합](#후보-통합--w1-b910fcc-승인-전) 절 |

화면 3(편집용 폴더)은 package 경로 표시·새로고침뿐이고 화면 4(최종 자막)은 설정
요약뿐이다. 둘 다 정직하게 비활성 상태로 남아 있다.

### 승인 기준선 근거

- 캡션 작업공간 백엔드 c6a0f6b + eff0f9e, 병합 cd16d80: 루트 30 시험과 실측
  탐침(두 오디오 트랙·수동 편집·불변 확정·오프라인 0송신) 통과.
- 검토 작업공간 백엔드 313f24d + 648b5d2, 병합 3cbf235: 루트 16 시험과 실측
  탐침(SQLite 크래시 롤백·재시도, 실제 분할→재계획→재추출→VTT→선택·전체 확정→
  정리→재오픈) 통과.
- 이 브랜치에서 `pytest tests/raw` → **841 passed**(2026-10-03 재실행, 합성
  미디어·오프라인, exit 0). 역사 수치 702(f939478)·805(1b7283d+검토 백엔드)는
  각 시점의 기록이다.
- A3·A4 브라우저 증거는 루트가 잰 **자동 브라우저 실측**이다. 사람 실기
  항목과 구분해 수동 인수 보고서에 적는다.

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
- 서버를 다시 띄우면 마지막 작업공간을 다시 열고, 서버 종료로 중단된 작업을
  재개 가능 상태로 돌린다(`recover_interrupted_jobs`).

## 합성 오프라인 재현 — 유료 호출 없이 전 단계 굴리기

두 가지가 다 오프라인·합성 미디어로만 돈다.

### 1. 시험 묶음(가장 빠른 재현)

```bash
pytest tests/raw              # 합성 MP4(ffmpeg lavfi)·기록된 응답만 쓴다
```

2026-10-03 main 3cbf235에서 **841 passed**. 가져오기 중복·복사 실패 재개·경로
검문·순서·추출 재개·검토 리비전·검토 백엔드 종단·출판 원자성(크래시 롤백·동시
편집·CAS·프로세스 즉사)·자막 작업공간(등록·승인·실행·검토·확정)·확정 내보내기·
캐시 보호·최종 자막 직렬화까지 이 안에 있다.

### 2. 진단 CLI 종단(실제 명령 흐름)

임시 디렉터리는 **mktemp가 소유한 새 디렉터리**에 만든다. 고정된 `/tmp` 경로를
쓰면 남의 파일을 겹쳐 쓸 수 있어 쓰지 않는다. 조리법 전체가 같은 셸 변수
`CARD`·`WS`를 다시 쓴다.

```bash
CARD=$(mktemp -d /tmp/vlogsrt-card.XXXXXX)   # 합성 카드 — 이 조리법만 소유한다
WS=$(mktemp -d /tmp/vlogsrt-ws.XXXXXX)       # 빈 작업공간 자리
# 합성 카드 — 검은 화면 2초 MP4 두 개. 촬영 시각은 컨테이너 creation_time에
# 명시적인 ISO-8601(시간대 포함)으로 적는다 — mtime은 순서 근거가 아니다.
ffmpeg -y -f lavfi -i color=black:s=64x32:r=10:d=2 -f lavfi -i anullsrc=r=8000:cl=mono \
  -shortest -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac \
  -metadata creation_time=2026-10-02T09:00:00+00:00 "$CARD/clip_a.mp4"
ffmpeg -y -f lavfi -i color=black:s=64x32:r=10:d=2 -f lavfi -i anullsrc=r=8000:cl=mono \
  -shortest -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac \
  -metadata creation_time=2026-10-02T09:05:00+00:00 "$CARD/clip_b.mp4"

vlogsrt media init "$WS" --name 문서확인
vlogsrt media import "$CARD" --workspace "$WS"            # 제안만 인쇄(쓰기 없음)
vlogsrt media import "$CARD" --workspace "$WS" --confirm  # 순서 확정 + 복사·검증
vlogsrt media inspect --workspace "$WS"
```

촬영 시각 계약: 조사자는 컨테이너가 선언한 `creation_time`을 **유효할 때만**
(ISO-8601·시간대·상식 bound를 통과할 때만) 순서 근거로 쓴다. 값이 없거나 못
미치는 실물 카드에서는 순서를 **사람이 확정 대화에서 명시적으로 확인한다**
(`user_confirmed`) — 파일 수정 시각(mtime)으로 시간순을 메우는 길은 없다.

여기서 `plan`으로 넘어가려면 **전체 원본 순서 확정**이 필요하다. 이 확정은 지금
UI(화면 1의 `전체 원본 순서 확정` 버튼)와 서비스 계층에만 있다 — 전용 CLI 동사는
없다. 진단으로는 서비스를 직접 부른다:

```bash
python - <<'EOF'
from pathlib import Path
from vlogsrt.project.raw.service import RawWorkspaceService
revision = RawWorkspaceService().confirm_global_source_order(Path("WS 경로"))
print(revision.revision_id, len(revision.entries))
EOF
```

```bash
vlogsrt media plan --workspace "$WS"        # 결정적 분할 계획(기본 타일 30초)
vlogsrt media extract --workspace "$WS"     # 로컬 ffmpeg 추출, 프로파일 기본 source_matched
vlogsrt media clips --workspace "$WS"       # 조각 목록 + 전달물(clips.jsonl·CSV·README) 경로
```

`extract`는 `--profile source_matched|edit_prores`, `--handle-before/--handle-after`
(초, 유리수), `--plan`(리비전 지정), `--wait-seconds`를 받는다. 결과는
`clips/package_000001/` 아래 `NNNNNN__t…-t…__c_….mp4` 파일과 전달물로 게시된다.

### 3. 오프라인 재생 분석 — `media analyze`

`vlogsrt media analyze`는 견적을 인쇄하고 **재생(replay) 분석만** 돌린다 — 어떤
플래그도 실제 호출을 켜지 않는다(전체 플래그는 위 CLI 참조). 재생 픽스처를
작업공간 `analysis/replay/`에 두면 가져온 원본의 내용 SHA로 묶여 재생된다:

```bash
python - <<'EOF'
import json
from pathlib import Path
from vlogsrt.project.raw.service import RawWorkspaceService
service = RawWorkspaceService()
root = Path("WS 경로")
sources = {
    asset.content_sha256: [{"start": 0.4, "end": 1.2, "text": "도착해서 일정 설명"}]
    for asset in service.list_source_assets(root)
}
assert all(sources), "원본 SHA가 없다 — 가져오기 확인부터"
fixtures = root / "analysis" / "replay"
fixtures.mkdir(parents=True, exist_ok=True)
(fixtures / "asr-fixtures.json").write_text(
    json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8"
)
(fixtures / "luna-fixtures.json").write_text(json.dumps(
    {"windows": {"window_000000": {
        "window_id": "window_000000",
        "speech_summary": "도착해서 일정을 설명한다",
        "visual_summary": "실내에서 걷다가 문으로 향한다",
        "scene_tags": ["walking", "arrival"],
        "evidence_ids": ["window_000000"],
        "suggested_boundary_ids": [], "editing_notes": [],
        "needs_review": False, "uncertainties": []}}},
    ensure_ascii=False), encoding="utf-8")
EOF
vlogsrt media analyze --workspace "$WS" --estimate-only   # 견적만 인쇄(분석 시작 없음)
vlogsrt media analyze --workspace "$WS"                   # 재생 분석 실행·완결 보고
```

재생할 픽스처가 없는 원본은 실패로 **기록될 뿐** 지어내지 않는다. `--plan`을
붙이면 완성된 분석 결과로 분할 계획 리비전을 새로 만든다(로컬만).

이 조리법 전체(1→3)를 2026-10-03에 mktemp 소유 디렉터리에서 처음부터 다시
돌렸다 — 전 단계 exit 0, 재생 분석 2/2 완결.

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
| `analyze --workspace WS [--sources ID …] [--fixtures DIR] [--estimate-only] [--plan] [--wait-seconds S]` | 분석 견적 인쇄 + **오프라인 재생 분석 실행**(유료 호출 없음). `--estimate-only`는 견적만 인쇄, `--plan`은 완성 결과로 분할 계획 리비전 생성, 재생 픽스처는 기본 `analysis/replay/` |

알려진 화면 문제: `media --help`의 요약 줄(metavar)이 아직 `analyze`를 빠뜨린다.
동사 자체는 목록에 있고 실제로 돈다(위 표는 실제 `--help`·실행으로 확인). 고치는
주체는 W1·루트다 — 이 문서가 대신 고치지 않는다.

추출 작업 재시도는 UI(화면 2 `재시도`)와 서비스 계층에 있다 — CLI `retry`는
가져오기 전용이다.

## 출력 디렉터리 — 조각과 자막은 다른 곳에 산다

| 자리 | 무엇 | 누가 쓰나 |
|---|---|---|
| `originals/` | 가져온 원본, 읽기 전용 | 가져오기 `--confirm` |
| `clips/package_NNNNNN/` | 추출 조각 + 전달물(clips.jsonl·CSV·README). 리비전마다 새 폴더 | `media extract`·화면 2 |
| `clips/confirmed_*` | 확정 내보내기 결과, 읽기전용 동결 | 후보: 화면 3 확정 내보내기 |
| `review/vtt/` | 검토용 VTT(클립 국소) | 후보: 화면 2 검토 |
| `final/captions/published/` | 확정 게시된 최종 SRT·VTT | 후보: 화면 4 확정 |
| `analysis/` | 분석 문서·재생 픽스처(`replay/`) | 분석 단계 |
| `cache/`·`previews/` | 재생성 가능한 캐시·미리보기 — 정리 대상은 이 둘뿐 | 캐시 정리 |

조각(package)은 편집용 폴더의 재료고, 자막(`final/captions`)은 최종 영상에 붙는
확정 결과다. 확정본은 어느 정리도 대상이 아니다.

## 후보 통합 — W1 b910fcc (승인 전)

W1이 같은 저장소의 `Ssojux2/raw-a2-ingest` 가지에 올린 네 화면 통합이다. **아직
루트 인수를 받지 않았다.** 아래는 후보 코드를 읽고 적은 것이고, 루트의 실측
브라우저 시험이 오면 이 절과 수동 인수표를 다시 연다. 백엔드가 없는 빌드에서는
후보 화면의 버튼이 정직한 오류로 닫힌다(병합 뒤 활성).

### 띄우기와 키

- 띄우기 자체는 그대로 `make ui`(루프백 7860)다.
- 실제 호출의 키는 **서버를 띄운 셸의 환경**에서만 온다(`OPENAI_API_KEY`,
  선택 `OPENAI_BASE_URL`). 서버가 시작될 때 한 번 찍고, 그다음은 어떤 적재로도
  문을 다시 열지 않는다.
- `.env` 파일을 읽지 않는다. 화면 JS·localStorage에 키가 실리는 길도 없다 —
  내려받기·재생은 등록 미디어 ID로만 간다.
- 키 없이 실제 호출 버튼을 누르면: "실제 호출에는 서버를 띄운 환경의
  OPENAI_API_KEY가 필요하다 — 키를 내보내고 브라우저 서버를 다시 띄우세요.
  이 화면은 .env 파일을 읽지 않는다."

### 2단계 승인 (실제 호출의 문)

1단계(전사)와 2단계(의미)가 각각 견적→확인란→실행으로 나뉜다. 승인은 화면이
잰 견적의 측정값(출처·스트림·청크·창·모델)에 묶이고, 전송 순간에 게이트가 다시
검사한다. 무성 원본은 1단계 승인 자체가 없다(전사할 오디오가 없다).

### 검토 화면(화면 2 후보 패널)

`검토 시작` → 시간순 조각 표(선택·발화 상태·태그·메모) → 메모 저장·선택 적용·
경계 조정·나누기·이웃 합치기 → `다시 뽑을 조각`(재추출 대상 표시) →
`검토 계획 반영`(편집된 덮개로 새 계획 리비전). 조각 재생에는 검토 자막(VTT)
토글이 붙는다. 편집은 화면이 본 헤드 리비전에 묶인다 — 늦은 편집은 거부다.

### 확정 내보내기·Finder·캐시 정리(화면 3 후보)

선택 조각 ID(비우면 전체) + 확인란 → `확정 내보내기`: 서버 소유 내보내기 ID로
`clips/confirmed_*` 읽기전용 폴더를 만든다. `Finder에서 열기`(경로 검증 뒤
`open -R`)·`내보내기 경로 복사`가 같은 자리를 연다. `캐시 정리`는 확인란 뒤에
`cache/`·`previews/`만 지운다 — 확정본은 물리적으로 보호된다.

### 최종 자막(화면 4 후보)

등록(최종 영상 절대 경로·오디오 트랙 선택) → `실행 추정(로컬)` → 확인란 승인 →
`자막 생성 실행` → 검토(큐 행·파형·미대응) → 수동 편집(글자 교정·시각 조정·큐
나누기·합치기·새 큐·경고 허용·미대응 해결·무발화 기록) → `확정 · SRT/VTT 게시`.
확정 파일은 `final/captions/published/`에 등록되고 내려받기는 등록 미디어
ID(`/media/{id}`)로만 제공된다. 낡은 확정 바이트는 다시 쓰지 않는다.

후보 CLI(승인 전): `vlogsrt media review --workspace WS [--init]`(검토 리비전
생성·인쇄, 로컬만), `vlogsrt media caption --workspace WS --path 절대경로
[--source-id] [--audio-ordinal] [--run]`(등록·추정만 — 실제 실행은 브라우저
승인 경로뿐).

### 재시작·호환성

서버를 다시 띄우면 마지막 작업공간을 다시 열고 중단 작업을 재개 가능 상태로
돌린다(기존 계약). 후보는 중앙 DB를 v6으로 올린다 — v5 위에 검토·자막 저장
스키마를 한 트랜잭션으로 붙이고 기존 행은 그대로 둔다.

### 알려진 결함 (재시험 대기)

같은 형상의 새 계획 리비전으로 추출을 돌리면 "이미 완료된 package" 오류가
났다(루트 A4 실측 브라우저에서 발견). W1이 고치고 루트가 다시 시험하기 전까지
이 문은 **NOT PASS**다.

## 문제 해결

| 증상 | 원인과 처치 |
|---|---|
| 포트 7860이 차 있다 | 서버가 옆 번호로 비켜 뜬다. 안내된 주소로 접속한다 |
| `ffmpeg`를 못 찾는다 | `brew install ffmpeg` 후 PATH 확인 |
| 카드 폴더에 파일이 없다고 한다 | ffmpeg는 폴더를 만들지 않는다 — `mktemp -d`가 만든 디렉터리에 쓴다 |
| 재생 분석이 원본마다 실패로 기록된다 | 픽스처가 없는 것이다 — 기록이 정상, 지어내지 않는다 |
| 서버를 껐다 켰더니 작업이 멈춰 보인다 | 시작 시 중단 작업이 재개 가능 상태로 돌아온다 — 화면에서 재시도 |
| 계획 뒤 원본을 바꿨더니 검토 초기화가 거부된다 | stale profile 가드다 — 원본을 되돌리거나 새로 가져온다 |
| 실제 호출 버튼이 키 오류로 닫힌다(후보) | 서버 셸에서 키를 내보내고 서버를 다시 띄운다 — `.env`는 이 문을 열지 않는다 |
| `--share`를 쓰면 종료된다 | 로컬 전용이다 — 외부 공유 길은 없다 |

## 대기 중인 통합 — 명령이 없는 단계

아래는 **라이브러리·백엔드와 시험은 승인돼 있지만** 공개 서비스·화면·CLI로
아직 열리지 않은 것들이다(후보 b910fcc가 여는 모양은 위 절). 이 문서는 실행
가능한 명령을 만들어 적지 않는다.

- **A5 조각 검토** — 경계 이동·분할·인접 병합·사용/제외 리비전, 검토용 VTT 투영.
  `segment_review.py`·`review_projection.py` + 시험. 지속 작업공간 백엔드는
  `review_workspace.py`·`review_workspace_store.py`(출판은 한 트랜잭션).
- **A6 편집용 폴더 확정** — 검증된 조각을 `clips/confirmed_*`에 불변 복사(게시·
  멱등 검증), 캐시 정리(`cache/`·`previews/`만, 보호 이름 거부).
- **A7 최종 자막** — 최종 영상 등록→문구/시간 모델 명시적 승인→SRT. 캡션
  작업공간 백엔드(c6a0f6b+eff0f9e)까지 승인 — 화면 연결 대기.

## 지키는 선 (이 워크플로의 안전 규칙)

- 가져오기·계획·추출·재생 분석은 **유료 호출이 없다**. 유료 호출이 가능한 길은
  승인 게이트를 지나야만 열리고, 이 문서의 어떤 명령도 그 게이트를 거치지 않는다.
- 원본은 `originals/`에 읽기 전용으로 보존된다. 추출·확정은 복사본을 만들 뿐이다.
- `clips/confirmed_*`와 보호 이름(originals·final·subtitles·review·analysis·jobs·
  config·project DB)은 캐시 정리 대상이 아니다(`cache_policy.py`가 검문한다).
- **AI Rough Cut Final Cut 플러그인(com.ssojux2.vlogsrt.workflow)은 사용자 요청으로
  비활성되어 있다** — 재활성화·재설치하지 마라. 상세는 수동 인수 보고서 참조.
