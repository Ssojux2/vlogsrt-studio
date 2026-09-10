# Task P5-03b: close the guard's config bypass and its two identity holes

## Authority

Binding spec `VAD guard`. Correction of candidate `f2a798f`. Coordinator: Opus 5
(Run `run_a091d4869a98`). Adjudicated by Fable 5.1 high — full verdict with raw probe output at
`docs/orchestration/reviews/fable-p5-03-guard-direction.md` (read it in the main repo at
`/Users/ssojux2/Working/vlogsrt`). Offline only.

## 무엇이 잘못됐나 — 네 구현은 브리프대로였다

방향 검토가 `f2a798f`를 **SOUND_WITH_CONCERNS**로 판정했다. 시각을 유도한 곳은 **없고**,
합집합·keep_all·origin 한 번·앵커 0 모두 맞다. 27개 테스트도 전부 통과한다.
**결함은 브리프의 계약에 있었지 네 구현에 있지 않다** — 내가 쓴 브리프가 이 셋을 요구하지
않았다. 그러니 이건 네 판단에 대한 지적이 아니다.

다만 결과는 심각하다. 재현된 것들이고 **전부 `status="success"`로 나온다**:

```
baseline:                        protected=[(8000,40000),(56000,88000)]
vad_positive=nan            →    protected=[(8000,40000)]      VAD 창이 전부 사라진다
vad_positive=1.5            →    protected=[(8000,40000)]
guard_padding_seconds=0     →    protected=[(16000,32000),(64000,80000)]
guard_padding_seconds=-1/2  →    protected=[]                  ★ 보호 구간이 통째로 빈다
window prob=nan             →    protected=[(8000,40000)]
windows가 [0,2s)만 덮음      →    나머지가 비speech 취급
stale origin reference      →    프레임이 12→63 에서 37→88 로 조용히 밀린다
```

**`protected=[]`인데 `status="success"`는 이 시스템에서 최악의 실패다.** guard는 말을
자르지 않기 위해 존재한다 — 비면 전부 잘라도 된다는 뜻이 된다. 음수 패딩은 VAD뿐 아니라
**whisper 세그먼트 자체를 줄인다**. 그 순간 합집합은 합집합이 아니게 된다.

세 결함의 계보:
- **P5-05b와 같은 모양**: `policy.py`에는 `_require_config`가 있는데 `guard.py`에는 없다.
  같은 base에서 **여섯 커밋 전에** 닫은 교훈이다.
- **P4-04와 같은 모양**: 타이밍을 정하는 값이 이음매를 건너오지 않는다. 그때는 닻이었고
  이번엔 PCM 세대(generation)와 origin이다.

## Baseline and ownership

- 같은 worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-guard`, branch
  `Ssojux2/fcp-p5-guard`. **기존 `f2a798f` 위에 커밋한다.** rebase·reset·squash·amend 금지.
- 허용 파일 (항목 4 때문에 **P5-02를 다시 연다 — 승인한다**):
  `vlogsrt/project/vad/guard.py`, `tests/project/test_guard.py`,
  `vlogsrt/project/vad/inference.py`, `tests/project/test_vad_inference.py`.
  `VadResult`의 소비자는 `guard.py`와 자기 테스트뿐임을 확인했다(blast radius 확인 완료).
- 금지: 그 밖의 모든 파일, fixtures, `golden/`, `.env`, `.env.example`.
  새 의존성·live 호출·`--online`·`VLOGSRT_OFFLINE=0` 금지.
- **기존 27개 테스트는 손대지 마라.** 하나라도 진짜로 충돌하면 고치지 말고 보고하라.

## 고칠 것 — Fable의 최소 수정 명세 그대로

1. **`guard.py` `_require_config(cfg)`** — `build_source_guard`의 **첫 관문**.
   `vad_positive`는 `bool`이 아닌 실수/정수 + `math.isfinite` + `[0,1]`.
   `guard_padding_seconds`는 `Fraction` + **`> 0`**.
   어기면 키와 값을 대고 `ValueError`. **설정 결함은 호출자 잘못이므로 예외로 닫는다** —
   레일 값으로 조용히 덮지 말고 `keep_all`로 세탁하지도 마라(P5-05b가 그은 선).
   이미 있는 `_finite`/`_bad_count` idiom을 재사용하라.

2. **VAD 입력 검증 → `keep_all` + review** (입력 결함은 **fail-closed지 예외가 아니다**).
   창마다 `bool` 아닌 int 끝점으로 `0 <= start < end <= sample_count`, 확률은 유한한 `[0,1]`.
   그리고 **모든 채널 창의 합집합이 `[0, sample_count)`를 정확히 타일링**해야 한다.
   지금의 맨 `not vad.windows` 검사를 이걸로 바꾼다.
   항목 1과 2의 차이를 헷갈리지 마라 — 설정은 raise, 입력은 keep_all이다.

3. **`SourceGuard`에 `analysis_key: str`과 `pcm_sample0_file_time: Fraction` 추가.**
   `build_source_guard`와 `_keep_all` **양쪽에서** 참조로부터 복사한다.
   `project_guard`는 둘이 참조의 값과 같은지 요구한다. 회귀: stale origin 참조(probe f)가
   반드시 닫혀야 한다.

4. **`VadResult`에 `analysis_key: str` 추가** (`inference.py`).
   `infer_vad`가 **성공·실패 양쪽 경로에서** `reference.analysis_key`로 채운다(참조가 손에 있다).
   `_require_vad_linkage`가 비교한다.

5. **`_slice_frames`**: `keep_all`이 inaudible과 같은 **whole-slice 경로**를 쓴다.
   지금은 keep_all이 `PCM ∩ slice`만 보호해서, PCM 끝을 넘어가는 슬라이스 꼬리가
   keep_all인데도 잘릴 수 있다(probe e: 225..250 대 225..300).

6. **`InstanceGuard`에 `status: Literal["success","keep_all","inaudible"]`과 `source_id: str` 추가.**
   P6가 한국어 산문을 파싱하거나 슬라이스 전체 길이로 추측하지 않게 한다.
   (P6-01/P6-02 브리프 갱신은 **내가 한다** — 너는 타입만 낸다.)

7. **`repairs` 매개변수**: `trace.repairs`와 중복이고 **선언된 시계가 없다**.
   `SourceTrace.repairs`는 디코드 오디오 국소이고 `BoundaryTranscript.repairs`는
   남긴 범위 국소다 — 섞이면 엉뚱한 자리에 놓인다. **떨어뜨리거나**, 남긴다면
   시그니처와 docstring에 시계를 명시하라. 어느 쪽을 골랐는지 보고에 적어라.

## 테스트

위 probe 줄마다 회귀 하나씩. 특히 `guard_padding_seconds=-1/2`가 `protected=[]`를 내지
않고 닫히는 것, stale origin이 닫히는 것, key 없는 `VadResult`가 닫히는 것.
**적대적 재현과 함께 정상 경로가 여전히 동작하는지도 증명하라** — 그게 없으면
"전부 막혔다"가 "모듈이 통째로 망가졌다"와 구별되지 않는다.

## Acceptance

worktree 루트에서 `/Users/ssojux2/Working/vlogsrt/.venv/bin/python`으로:

```
python -m pytest tests/project/test_guard.py
python -m pytest tests/project/test_vad_inference.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

불변식은 요약을 믿지 말고 **`[OK]` 줄을 세어** 숫자로 보고하라 — **19개**여야 한다.
전체 스위트는 `f2a798f`의 2,085개 + 새 테스트 이상이어야 한다.
`git diff --stat f2a798f..HEAD`는 허용된 네 파일만 나열해야 한다.

## Failure route

정확한 설정값·입력·기대/실제 `protected`를 exact `Fraction`/샘플 번호로 보고하라.
레일을 느슨하게 하지 말고, 기존 27개 테스트를 고쳐서 맞추지 마라.
같은 실패 2회면 멈추고 코디네이터(Opus 5)에게 상승하라.

## Report

`worker_done` 전에 새 SHA, `git diff --stat f2a798f..HEAD`, 항목 1–7 각각의 처리 결과
(7번은 고른 쪽), red-first 증거, 모든 acceptance 명령의 초록 출력과 테스트 수·불변식 센 값,
네트워크 호출 0 확인을 보내라. 그다음 `worker_done`을 **정확히 한 번** 보낸다.
