# 실제 영상으로 테스트하기

돈이 나가는 단계는 **`vlogsrt record` 하나뿐**이다. 회차당 한 번만 부르고,
그 뒤로는 녹화된 응답으로 무한히 반복한다. 순서를 지키면 실수로 돈이 새지 않는다.

```
0. 준비        무료
1. 확인        무료   probe · dry-run — 오디오만 보고 비용을 미리 센다
2. 녹화        유료   record — 회차당 딱 한 번
3. 반복        무료   run --offline · redo — 여기서 원하는 만큼 굴린다
```

**터미널이 번거로우면 웹 UI를 쓰면 된다.** 아래 절차를 화면으로 옮겨놓은 것이다.

```bash
pip install -e ".[ui]"
make ui                 # http://127.0.0.1:7860
```

뜨면 터미널에 주소가 찍히고 브라우저가 열린다. 이미 쓰는 포트면 옆 번호로 비켜 뜬다.
화면은 경로 입력 · 실행 로그 · SRT 셋뿐이다. 안전장치(`VLOGSRT_COST_GUARD_SECONDS`)는
UI에서도 그대로 동작한다.

---

## 0. 준비

```bash
# 키. .env.example에 있던 키는 유출됐다. 새로 발급해서 넣어라.
cp .env.example .env
$EDITOR .env          # OPENAI_API_KEY=sk-proj-... 채우기

# 나머지
brew install ffmpeg   # ffmpeg + ffprobe
make venv
source .venv/bin/activate
```

`.env`의 `VLOGSRT_OFFLINE=1`은 **그대로 두어라.** 이 값이 1이어도
`record`와 `--online`은 동작한다. 나머지 모든 경로만 막힌다.

---

## 1. 확인 — 돈 쓰기 전에 (무료)

```bash
vlogsrt probe ~/Movies/vlog01.mp4
```

```
길이       734.20초
오디오     .vlogsrt-cache/probe/vlog01.mp3 (5.87MB)
조각       5개 · 각 2.4분 (한도 24.0MB / 3분)
예상 비용   $0.073
```

봐야 할 것:

- **길이** — 비용은 여기 비례한다. 분당 약 $0.006. 조각 수와는 무관하다
- **조각** — 바이트 한도와 초 한도 중 **작은 쪽**이 정한다. 경계는 목표 지점 ±5초에서
  **가장 조용한 곳**으로 옮겨가므로 말 중간에서 잘리지 않는다. 못 찾으면 목표에서
  그냥 자른다 — **어느 경우에도 구간이 사라지지는 않는다**(D25)
- **`각 M분`이 3분을 넘으면 설정이 안 먹은 것이다.** 코드 기본값
  `chunk_max_seconds = 180`이 막아야 하는데 넘어갔다면 `vlogsrt config show`로
  실제 값을 확인하라. D26의 설정이 실행에 닿지 않아 38분 조각으로 돌았고 영상의
  41.4%가 되풀이가 된 적이 있다(D27)
- **조각 길이가 곧 되풀이가 자랄 수 있는 최대 길이다**(D28). 되풀이는 한 API 호출
  안에서만 번지므로 호출 경계가 유일한 강제 리셋이다. 조각을 늘려도 **총비용은 같다**
  — 요금이 분당이기 때문이다

```bash
vlogsrt run ~/Movies/vlog01.mp4 --dry-run
# 길이 734.2초 · 조각 1개 · 예상 $0.147
```

예상 비용은 **자막 + 교차 확인 두 몫**이다. `--no-verify`면 절반이다.
여기에 **되풀이 재전사가 얹힐 수 있다** — 되풀이 구간 길이에만 붙으므로 멀쩡한
영상은 0원이고, 되풀이가 심하면 실행 로그의 `되풀이 N구간 M초`만큼 늘어난다.
끄려면 `--no-repair`(되풀이가 자막에 그대로 남는다).

**처음이라면 짧게 잘라서 시작하라.** 3~5분이면 충분히 감이 온다.

```bash
ffmpeg -i ~/Movies/vlog01.mp4 -ss 00:02:00 -t 00:04:00 -c copy /tmp/test01.mp4
```

---

## 2. 녹화 — 여기서만 돈이 나간다

```bash
vlogsrt record /tmp/test01.mp4
```

```
› 오디오 240초, 예상 비용 약 $0.024
tests/fixtures/api/test01.transcribe.json
tests/fixtures/api/test01.meta.json
```

- 확인 프롬프트는 **안전장치(`VLOGSRT_COST_GUARD_SECONDS`, 기본 2시간)를 넘을 때만** 뜬다.
  그 아래는 예상 비용만 찍고 바로 호출한다
- 파일 이름(`test01`)이 픽스처 키다. 이후 `run`에 넘기는 영상 경로의
  파일 이름이 같아야 픽스처를 찾는다
- `meta.json`에 길이가 들어간다. 그래서 이 뒤로는
  **영상 파일이 없어도** 파이프라인이 돈다

---

## 3. 반복 — 무료 구간

```bash
vlogsrt run /tmp/test01.mp4 --offline -o out/
```

```
out/test01.srt          ← 최종 자막
out/trace.json          ← 재실행용 스냅샷
```

### 볼 순서

교차 확인이 의심 지점을 로그로 짚어주지만 **고쳐주지는 않는다**(규칙 5).
판단은 전부 사람 몫이다.

**① SRT를 영상에 얹어서 본다** — 이게 유일한 검사다.

```bash
# 미리보기 (자막을 태워서 재생)
ffplay -vf "subtitles=out/test01.srt" /tmp/test01.mp4
```

QuickTime은 SRT를 못 읽는다. IINA나 VLC로 열고 자막 트랙을 얹거나,
편집 프로그램(Premiere / DaVinci / 프리미어 캡션 트랙)에 임포트하면 된다.

**② 눈으로 볼 것** — 숫자로 안 잡히는 것들이다.

- 자막이 말보다 늦게/일찍 뜨지 않는가 (**1순위**)
- **초반과 마지막에 자막이 있는가** — D24에서 조용한 인트로·아웃트로가 통째로
  날아간 적이 있다(D25). 제일 먼저 볼 것
- **같은 말이 되풀이되지 않는가** — whisper가 긴 조각에서 빠지는 병리다. 이제
  재전사가 먼저 걷어내므로(D27), 검증 로그의 `되풀이 N구간`에 남는 것은 **재전사도
  회복하지 못한 것**이다
- **한 글자짜리 자막이 줄줄이 이어지지 않는가** — `S`, `F`, `아` 같은 것이 2초 간격으로
  수십 장 서면 그 구간은 whisper가 무너진 것이다(주행음·바람·음악에서 난다). 검출이
  이것을 짚게 된 것은 D29부터다 — 그 전에는 88초·44장이 경고 하나 없이 나갔다
- **자막이 통째로 비는 구간이 없는가** — 재전사가 **긴** 되풀이를 회복하지 못하면
  첫 장만 남기고 그 구간을 비운다. **이 설계의 대가가 나타나는 곳이다.** 실행 로그의
  `WARN … 회복하지 못했다`와 `trace.json`의 `repairs[]`가 어디인지 알려준다.
  너무 잦으면 `repair_max_attempts`를 올리거나 `chunk_max_seconds`를 더 낮춘다
- **마지막 줄의 `남은 되풀이 N초`** — 갈아끼운 뒤에도 남은 것이 있다는 뜻이다.
  `회복 N`만 보고 안심하지 마라. 예전에는 8구간 전부 "회복"으로 찍히는 동안 114초가
  SRT로 나갔다(D28)
- **같은 영상을 두 번 돌리면 자막이 조금 다르다.** 정상이다 — 1차 전사를 온도 0.6으로
  부르기 때문이고, 그 온도가 되풀이가 번지는 통로를 끊는다(D28)
- 한 큐가 화면에 너무 짧게 스쳐 지나가지 않는가
- 줄바꿈이 어색한 데서 끊기지 않는가

기계로 한 번 더 보고 싶다면 오디오 포락선 대조 게이트가 있다 — 파이프라인 밖의
유일한 자동 검사다.

```bash
make verify STEM=test01
```

### 조판이 마음에 안 들면 — API 없이 0.1초

```bash
vlogsrt redo out/trace.json --max-chars 14 -o out/narrow/
vlogsrt redo out/trace.json --max-cps 10  -o out/slow/
vlogsrt redo out/trace.json --preset shorts -o out/shorts/
```

`redo`는 `trace.json`의 골격만 읽는다. **API를 부르지 않는다.**
값이 정해지면 `presets/vlog.toml`에 적어라.

### 고유명사가 틀리면 — 사전

```bash
cat > keywords/ep01.txt <<'EOF'
블루펭귄
망원동
연남동
EOF

vlogsrt keywords show --file keywords/ep01.txt
```

**`keywords`는 API 요청 파라미터다.** 사전을 바꾸면 효과를 보려고
**다시 녹화해야 한다** (`redo`로는 안 된다).

```bash
vlogsrt record /tmp/test01.mp4 --keywords-file keywords/ep01.txt
vlogsrt run /tmp/test01.mp4 --offline --keywords-file keywords/ep01.txt -o out/
```

후보는 SRT를 읽으며 눈으로 찾는다. 자동으로 뽑아주는 단계는 없다.

---

## 4. 점수로 보고 싶다면 — 골든셋

여기부터는 준비가 필요하다. `TASKS.md`의 T0.0이다.

1. 브이로그 3편에서 3~5분씩 잘라 `golden/ep01.mp4` ~ `ep03.mp4`
2. **손으로 완벽한 자막**을 만들어 `golden/ep01.srt` ~ `ep03.srt`
   (텍스트와 타이밍 둘 다 정확해야 한다)
3. `vlogsrt record golden/ep0{1,2,3}.mp4` (약 $0.18)

```bash
vlogsrt run golden/ep01.mp4 --offline -o eval/full/       # 기본 설정
vlogsrt redo eval/full/trace.json --preset shorts -o eval/shorts/
make eval                                                 # → docs/DASHBOARD.md
```

`vlogsrt eval <내것> <정답>`으로 한 편만 볼 수도 있다.

관문 조건은 `docs/DASHBOARD.md` 아래쪽 표에 있다.
**타이밍 지표가 나빠졌다면 CER이 아무리 좋아져도 통과가 아니다.**

---

## 막히면

| 증상 | 원인과 조치 |
|---|---|
| `픽스처가 없습니다` | 그 영상은 아직 녹화 전이다. 메시지가 알려주는 `record` 명령을 그대로 실행 |
| `오프라인 모드입니다` | `record`나 `--online` 없이 실제 호출을 시도했다. 정상 동작이다 |
| `model_not_found` / 404 | 계정에 그 모델이 없다. **과금되지 않는다.** `vlogsrt.toml`의 `[transcribe] transcribe_model`을 계정에 있는 모델로 바꿔라. 시각은 오디오에서 오므로 타임스탬프를 주는 모델일 필요가 없다 |
| `API가 chunking_strategy=…를 거부했습니다` | 기본값은 빈 문자열이라 보내지 않는다. 채워놓았다면 비워라 — 자르는 건 우리 몫이다(D24) |
| `전사 응답에 text가 없습니다` | 응답 형식이 안 맞는다. `transcribe_format`을 `json` 또는 `text`로 |
| **자막이 소리와 어긋난다** | 무음 검출 문제다. `vlogsrt probe <video>`로 발화 구간 수를 보고 `silence_threshold_db`·`silence_min_duration`을 조정하라(D24) |
| `묶음 N개 중 M개가 발화 구간 1개뿐이다` 경고 | BGM·소음으로 무음이 안 잡혔다. 타이밍이 고정 창 방식으로 퇴화한 상태다. `silence_threshold_db`를 올려라 |
| `ffmpeg 를 찾지 못했습니다` | `brew install ffmpeg` |
| `API가 […]를 거부했다` 경고 | 모델이 `prompt`/`keywords`/`languages`/`temperature` 중 그 인자를 안 받는다. 그것만 빼고 다시 부른 것이라 자막은 정상적으로 나온다 |
| `전사 응답에서 세그먼트를 얻지 못했습니다` | 전 구간이 빈 응답이다. 오디오에 소리가 있는지 `probe`로 확인 |
| `글자가 없이 돌아온 조각 N개` 경고 | 그 구간에는 자막이 서지 않는다. 멈추지는 않는다(규칙 9). 그 구간에 소리가 있는지 확인 |
| `조각 N이 계획보다 짧다` 경고 | ffmpeg 분할이 오디오 일부를 잃었다. `make clean` 후 재실행 |
| `글자가 없는 큐 N개를 버렸다` 경고 | 정제 버그다. 로그와 함께 보고 |
| 자막이 통째로 밀림 | `make verify STEM=<stem>`으로 조각 정렬과 골격 시프트를 확인 |
| `make ui` 를 눌러도 아무 반응이 없다 | 주소가 안 찍히면 gradio 미설치다. `pip install -e ".[ui]"` |
| `파일 대화상자 오류: … (-600)` | 창을 앞으로 끌어오는 데 실패한 것이다. 대화상자는 그래도 떠야 하며, 안 뜨면 **경로를 칸에 직접 붙여넣으면 된다** |
| 대화상자가 브라우저 뒤에 숨는다 | 정상이다. Dock에서 osascript를 클릭하거나 `Cmd+Tab`으로 앞으로 가져와라 |
| UI 포트를 바꾸고 싶다 | `.venv/bin/python -u -m ui.app --port 7900` |
| `안전장치를 넘습니다` | 긴 영상이다. `.env` 의 `VLOGSRT_COST_GUARD_SECONDS` 를 올려라 (초 단위). 메시지가 넣을 값을 알려준다 |
| 다시 처음부터 | `rm -rf .vlogsrt-cache out/` — 픽스처는 지워지지 않으니 무료로 재실행된다 |

## 돈이 나가는 명령 (이것만 외우면 된다)

```
vlogsrt record <video>          ← 유료
vlogsrt run <video> --online    ← 유료
```

나머지는 전부 무료다.
