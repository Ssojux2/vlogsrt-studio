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
길이     734.20초
오디오   .vlogsrt-cache/probe/vlog01.mp3 (5.87MB)
청크     1개
```

봐야 할 것:

- **길이** — 비용은 여기 비례한다. 분당 약 $0.006
- **청크** — 2개 이상이면 그 경계에서 한두 어절이 사라질 수 있다.
  경계는 24MB 또는 25분 한도로만 정해진다(무음을 찾지 않는다 — D21)

```bash
vlogsrt run ~/Movies/vlog01.mp4 --dry-run
# 길이 734.2초 · 청크 1개 · 예상 $0.073
```

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

프로그램은 자막 품질에 대해 아무 말도 하지 않는다(D21). 판단은 전부 사람 몫이다.

**① SRT를 영상에 얹어서 본다** — 이게 유일한 검사다.

```bash
# 미리보기 (자막을 태워서 재생)
ffplay -vf "subtitles=out/test01.srt" /tmp/test01.mp4
```

QuickTime은 SRT를 못 읽는다. IINA나 VLC로 열고 자막 트랙을 얹거나,
편집 프로그램(Premiere / DaVinci / 프리미어 캡션 트랙)에 임포트하면 된다.

**② 눈으로 볼 것** — 숫자로 안 잡히는 것들이다.

- 자막이 말보다 늦게/일찍 뜨지 않는가 (**1순위**)
- 청크 경계 시각(로그의 `청크 N개`로 알 수 있다) 부근에서 말이 빠지지 않았는가
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
| `model_not_found` / 404 | 계정에 `gpt-4o-transcribe-diarize`가 없다. **과금되지 않는다.** `vlogsrt.toml`의 `[transcribe] transcribe_model`을 계정에 있는 모델로 바꿔라 (whisper-1로 되돌리려면 `transcribe_format = "verbose_json"`과 `chunking_strategy = ""`도 함께) |
| `API가 chunking_strategy=…를 거부했습니다` | 모델과 `chunking_strategy`가 안 맞는다. diarize 모델은 이 값이 **필요하고**(`"auto"`), whisper-1은 이 인자를 **모른다**(`""`). D22 |
| `전사 응답에 segments가 없습니다` | 시각을 주지 않는 모델을 불렀다(`gpt-transcribe` 등). 위와 같은 곳을 고쳐라 |
| `ffmpeg 를 찾지 못했습니다` | `brew install ffmpeg` |
| `API가 […]를 거부했다` 경고 | 모델이 `prompt`/`keywords`/`languages`/`temperature` 중 그 인자를 안 받는다. 그것만 빼고 다시 부른 것이라 자막은 정상적으로 나온다 |
| `전사 응답에서 세그먼트를 얻지 못했습니다` | 전 구간이 빈 응답이다. 오디오에 소리가 있는지 `probe`로 확인 |
| 자막이 통째로 밀림 | `make verify STEM=<stem>`으로 청크 정렬과 골격 시프트를 확인 |
| `make ui` 를 눌러도 아무 반응이 없다 | 주소가 안 찍히면 gradio 미설치다. `pip install -e ".[ui]"` |
| UI 포트를 바꾸고 싶다 | `.venv/bin/python -u -m ui.app --port 7900` |
| `안전장치를 넘습니다` | 긴 영상이다. `.env` 의 `VLOGSRT_COST_GUARD_SECONDS` 를 올려라 (초 단위). 메시지가 넣을 값을 알려준다 |
| 다시 처음부터 | `rm -rf .vlogsrt-cache out/` — 픽스처는 지워지지 않으니 무료로 재실행된다 |

## 돈이 나가는 명령 (이것만 외우면 된다)

```
vlogsrt record <video>          ← 유료
vlogsrt run <video> --online    ← 유료
```

나머지는 전부 무료다.
