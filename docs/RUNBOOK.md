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
UI에서도 유료 버튼은 동의 체크 없이는 동작하지 않고, 안전장치 시간을 넘으면 막는다.

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
무음 구간  48개
오디오     .vlogsrt-cache/probe/vlog01.mp3 (5.87MB)
```

봐야 할 것:

- **길이** — 비용은 여기 비례한다. 분당 약 $0.012 (두 모델 합)
- **무음 구간** — 0개면 `loudnorm` 뒤에도 소리가 꽉 찼다는 뜻이다.
  BGM이 크면 그럴 수 있고, 그러면 청크 분할이 말 중간을 자를 수 있다
- **오디오 크기** — 24MB를 넘으면 청크로 쪼개진다

```bash
vlogsrt run ~/Movies/vlog01.mp4 --dry-run
# 길이 734.2초 · 청크 1개 · 예상 $0.147
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
› 오디오 240초, 예상 비용 약 $0.048
tests/fixtures/api/test01.whisper.json
tests/fixtures/api/test01.gpt.json
tests/fixtures/api/test01.meta.json
```

- 확인 프롬프트는 **1시간(`VLOGSRT_COST_GUARD_SECONDS`)을 넘을 때만** 뜬다.
  그 아래는 예상 비용만 찍고 바로 호출한다
- 파일 이름(`test01`)이 픽스처 키다. 이후 `run`에 넘기는 영상 경로의
  파일 이름이 같아야 픽스처를 찾는다
- `meta.json`에 길이와 무음 구간이 들어간다. 그래서 이 뒤로는
  **영상 파일이 없어도** 파이프라인이 돈다

---

## 3. 반복 — 무료 구간

```bash
vlogsrt run /tmp/test01.mp4 --offline -o out/
```

```
out/test01.srt          ← 최종 자막
out/test01.review.srt   ← 검토용
out/test01.report.md    ← 어디를 볼지
out/trace.json          ← 재실행용 스냅샷
```

### 볼 순서

**① `report.md`의 "우선 확인"** — 신뢰도 낮은 순으로 정렬돼 있다.
whisper와 gpt 텍스트가 나란히 있으니 어느 쪽이 맞는지 바로 판단된다.

**② `review.srt`를 영상에 얹어서 본다** — 두 모델이 갈린 글자만 `[?…]`로 싸여 있다.

```bash
# 미리보기 (자막을 태워서 재생)
ffplay -vf "subtitles=out/test01.review.srt" /tmp/test01.mp4
```

QuickTime은 SRT를 못 읽는다. IINA나 VLC로 열고 자막 트랙을 얹거나,
편집 프로그램(Premiere / DaVinci / 프리미어 캡션 트랙)에 임포트하면 된다.

**③ 눈으로 볼 것** — 숫자로 안 잡히는 것들이다.

- 자막이 말보다 늦게/일찍 뜨지 않는가
- 한 큐가 화면에 너무 짧게 스쳐 지나가지 않는가
- 줄바꿈이 어색한 데서 끊기지 않는가

### 조판이 마음에 안 들면 — API 없이 0.1초

```bash
vlogsrt redo out/trace.json --max-chars 14 -o out/narrow/
vlogsrt redo out/trace.json --max-cps 10  -o out/slow/
vlogsrt redo out/trace.json --preset shorts -o out/shorts/
```

`redo`는 `trace.json`의 골격과 큐 초안만 읽는다. **API를 부르지 않는다.**
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

후보는 `report.md`의 "사전 갱신 후보" 절이 알려준다
(`블루펭귄 ← 블루팽귄 4회` 형식, 2회 이상 반복된 것만).

---

## 4. 점수로 보고 싶다면 — 골든셋

여기부터는 준비가 필요하다. `TASKS.md`의 T0.0이다.

1. 브이로그 3편에서 3~5분씩 잘라 `golden/ep01.mp4` ~ `ep03.mp4`
2. **손으로 완벽한 자막**을 만들어 `golden/ep01.srt` ~ `ep03.srt`
   (텍스트와 타이밍 둘 다 정확해야 한다)
3. `vlogsrt record golden/ep0{1,2,3}.mp4` (약 $0.18)

```bash
vlogsrt baseline golden/ep01.mp4 --offline -o eval/b0/   # whisper 그대로
vlogsrt run      golden/ep01.mp4 --offline -o eval/b2/   # 최종
make eval                                                # → docs/DASHBOARD.md
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
| `model_not_found` / 404 | `gpt-transcribe` 모델 ID가 계정에서 다르게 불릴 수 있다. `vlogsrt.toml`에 `[transcribe]` `gpt_model = "..."`로 덮어써라 |
| `ffmpeg 를 찾지 못했습니다` | `brew install ffmpeg` |
| `전역 유사도 0.3x` 경고 | 두 전사가 서로 다른 말을 하고 있다. 무음이 대부분이거나 언어 설정이 틀렸을 때 나온다. whisper 골격 텍스트로 폴백해 출력한다 |
| `판정을 통과한 세그먼트가 없습니다` | 전 구간이 환청/무음 판정을 받았다. 오디오가 거의 무음인지 `probe`로 확인 |
| 자막이 통째로 밀림 | 청크 경계 문제일 수 있다. `trace.json`의 `spine` 시각과 영상을 대조 |
| `make ui` 를 눌러도 아무 반응이 없다 | 주소가 안 찍히면 gradio 미설치다. `pip install -e ".[ui]"` |
| UI 포트를 바꾸고 싶다 | `.venv/bin/python -u -m ui.app --port 7900` |
| 다시 처음부터 | `rm -rf .vlogsrt-cache out/` — 픽스처는 지워지지 않으니 무료로 재실행된다 |

## 돈이 나가는 명령 (이것만 외우면 된다)

```
vlogsrt record <video>          ← 유료
vlogsrt run <video> --online    ← 유료
```

나머지는 전부 무료다.
