# 원본/파생 프로젝트 계약 (PROJECT_CONTRACTS)

원본 소스 엔진(`vlogsrt/*.py`, `ui/**`)과 파생 계층(`vlogsrt/project/` — Final Cut
프로젝트) 사이의 경계를 정의한다. 기계 검사는 `scripts/check_invariants.py`가,
시험은 `tests/test_invariants.py`의 계약 블록이 맡는다.

모든 예외는 **정확 상대 경로**로 걸려 있다. 파일 **이름**으로 판정하지 않는다 —
이름으로 걸면 `vlogsrt/project/transcribe.py`가 openai를, `vlogsrt/project/preprocess.py`가
subprocess를 물려받는다. import·호출 판정은 AST로 한다. 정규식은 별명
(`import subprocess as sp`)과 상대 import를 놓친다.

근거: `docs/plans/2026-09-08-final-cut-project.md` "Source contracts and architecture"
＋ bootstrap 처분 C9(exact-path·AST, basename 예외 폐지).

## 1. 방향 — project → 레거시 공개 경계만

| 쪽 | 무엇 | 가져올 수 있는 것 |
|---|---|---|
| 원본 엔진 `vlogsrt/*.py` | whisper 파이프라인 전부 | 레거시끼리 자유롭게. **`vlogsrt.project`는 불가** |
| `ui/**` | 웹 UI | `vlogsrt` 공개 경계. **`vlogsrt.project`는 불가** |
| 파생 `vlogsrt/project/**` | Final Cut 프로젝트 계층 | **레거시 공개 경계만** (project → 레거시 단방향) |

레거시가 project를 import하는 형태는 전부 금지다 — 절대
(`import vlogsrt.project`, `from vlogsrt.project import x`, `from vlogsrt import project`)와
상대(`from .project import x`, `from . import project`).

**유일한 예외**: `vlogsrt/cli.py` (정확 경로). 문서화된 CLI 조립 진입점이며,
가져올 수 있는 대상은 **정확히 `vlogsrt.project.commands` 모듈 하나**다 — 계획된
`commands.py`를 가리키는 절대 형태(`import vlogsrt.project.commands`, `from
vlogsrt.project import commands`, `from vlogsrt.project.commands import …`)와 그
상대 등가(`from .project import commands`, `from .project.commands import …`)가
같은 좁은 경계로 귀결한다. **그 하위 모듈은 예외가 아니다** — `import
vlogsrt.project.commands.parser`와 `from …commands.parser import …`도 거부한다.
`import vlogsrt.project`(패키지 자체), `from vlogsrt import project`, 그리고
commands 외 하위 모듈(config 등)도 **CLI에서 거부**한다. 이 예외는 다른 어떤
레거시 모듈에도 확장되지 않는다 — 소스 엔진(`transcribe`, `preprocess`, `repair`,
`repetition`, `refine`, `output`, `layout`, `polish`, `pipeline`, `models`,
`config`, `env`, `log`, `cutpoints`, `hangul`, `keywords`, `evaluate`)과
`verify.py`, `ui/**`는 project 전체(commands를 포함해)를 import할 수 없다.

## 2. 원본 기록은 불변이다

원본의 `Trace`·`spine`·큐·SRT·`trace.json`은 project 연산에 대해 **불변**이다.
project에서 자막을 지우거나·편집하거나·다시 투영해도 원본 기록은 한 글자·한
밀리초도 바뀌지 않는다. 파생물(EDIT/SOURCE SRT, `project_trace.json` 등)은 언제나
**새 파일**로 쓰고, 원본은 읽기만 한다. 이 보호는 검증(verify)의 on/off와 무관하게
항상 선다 — 검증은 표시만 하고(규칙 5), 원본 보호가 검증에 의존하지 않는다.

## 3. 시각 — 원본은 whisper 그대로, 파생은 원본의 투영

- **원본 시각**: whisper 응답 `segments[]`의 시각 ＋ 조각 오프셋. 원본 오디오
  타임라인 그대로다(규칙 1).
- **애셋별 캐시 원본 SRT**: **불변의 애셋 로컬 시각**이다. 프로젝트 상태와
  무관하게 애셋 하나의 시각 그대로 저장되며, 어떤 이후 연산도 이 파일을
  다시 쓰지 않는다.
- **패키징되는 SOURCE SRT**: 그 큐들을 입력 프로젝트의 **경과 타임라인**에 투영해
  쓴다 — 자동 컷 이전 기준이고, 같은 애셋이 여러 번 쓰여도 인스턴스마다 투영한다.
- **EDIT SRT**: 편집된 타임라인에 투영한다.
- **파생 시각**: XML 시간은 `Fraction`으로 다루고, 편집 시각은 원본 시각의
  **투영**으로만 계산한다. 파생 시각을 원본 기록에 다시 쓰는 방향은 없다.
- 같은 경계는 같은 값을 갖는다. 최종 직렬화의 정확 반올림(유리수 half-up)은
  파생 export를 소유한 작업이 정착한다.

## 4. 네트워크 — openai는 정확히 한 파일

openai import의 허용 지점은 **정확 경로 `vlogsrt/transcribe.py` 하나**다. 그 외
모든 파일 — `vlogsrt/project/**`를 포함한다 — 에서 금지다. project 쪽에 vision
전송이 필요해도 직접 import하지 않는다; `vlogsrt/transcribe.py` 안에 좁은 전송을
더하는 방향만 있다.

## 5. subprocess — 정확히 두 파일

| 정확 경로 | 부를 수 있는 것 |
|---|---|
| `vlogsrt/preprocess.py` | ffmpeg / ffprobe |
| `vlogsrt/project/xml_validation.py` | **xmllint 단독** (아래 계약) |

`vlogsrt/project/xml_validation.py`의 xmllint 계약:

- **subprocess API는 `subprocess.run` 하나로 얼어 있다.** 참 subprocess 모듈
  별명(`import subprocess as sp`)의 `.run`과 참 함수 별명(`from subprocess import
  run as launch`)만 인정한다. `Popen`·`check_call`·`check_output`·`call`은
  모듈 속성이든 from-import든 거부하고, 다른 모듈에서 온 `run`·출처를 정적으로
  입증 못 한 `run`·한 번이라도 덮어쓰인(shadow) 별명도 보수적으로 거부한다.
- argv는 **정확히 두 형태**의 리터럴 리스트/튜플이다:
  `[exe, "--nonet", "--noout", path]` 또는
  `[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, path]`.
  깃발의 집합·순서가 이와 다르면(빠짐·끼워넣기·뒤집힘) 거부한다. 마지막
  칸들(path·dtd_uri)만 동적 표현식을 쓸 수 있고, 원소를 f-string·문자열
  연결·starred로 조립하지 않는다.
- 실행파일은 `shutil.which("xmllint")` 결과로만 얻는다. `which`에 다른 이름을
  묻는 것도, 상수·변수로 실행파일을 박는 것도 금지다.
- `which`의 **출처까지 본다**. `import shutil [as …]`(모듈 별명)과
  `from shutil import which [as …]`(함수 별명)만 인정하고, 그 밖의 객체에 붙은
  `.which`나 덮어쓰인(shadow) `which`는 출처 불명으로 거부한다.
- argv는 **유일한 위치 인자**다. `run`은 남는 위치 인자를 `Popen`에 그대로 넘기므로
  뒤쪽 슬롯(bufsize·`executable`)으로 실행파일이 흘러드는 것을 이 규칙으로 끊는다.
  `executable=` 키워드 오버라이드와 `**` 전개도 금지다.
- 셸은 금지다 — `shell` 키워드는 생략하거나 리터럴 `False`만.
- `from subprocess import *`와 `from shutil import *`는 호출·출처 추적을
  무력화하므로 금지다.

경계 밖의 그 어떤 파일도 subprocess를 import할 수 없다. DTD URI 변환·경로
이스케이프·오류 처리·실제 XML 검증은 **P1-04의 행동 테스트**가 소유한다 —
이 검사는 위의 정적 형태만 본다.

검사가 인정하는 정적 형태는 정해져 있다: 참 shutil 묶음(별명 포함)의
`which("xmllint")`로 얻은 실행파일, 정적으로 풀린 `subprocess.run`, 두 argv
형태, 유일한 위치 인자, 생략되거나 리터럴 `False`인 `shell`. 이 기계 검사는
**우발적 드리프트를 막는 난간**이지 일반적인 파이썬 보안 샌드박스가 아니다 —
결심한 우회까지 막지는 못한다.

## 6. 선택 의존성 — `vlogsrt/project/vad/` 아래에서만

`[project]` dependencies는 정확히 `openai`, `tomli-w`, `rich` 셋이다(불변 규칙 7).
선택 extra `project = ["numpy>=2.0", "onnxruntime>=1.19"]`는 로컬 VAD의 런타임
이고, **import는 `vlogsrt/project/vad/` 디렉터리 아래에서만** 허용된다 — 비전
(`project/vision*`)을 포함한 project의 다른 자리도 안 된다. 레거시 `vlogsrt/*.py`와
`ui/**`에서는 별명 형태(`import numpy as np`, `from numpy import ndarray`,
`import numpy.typing`)까지 전부 금지다. 그 외 금지 의존성(`jamo`, `konlpy`,
`pysrt`, `ffmpeg`, `pydantic`, …)은 project 안을 포함한 어디서도 허용되지 않는다.

## 7. 모델 이름 — 리터럴은 정확한 config 경로에만

엔진 모델 리터럴(`whisper-1`, `gpt-*transcribe*`)은 **정확 경로
`vlogsrt/config.py`와 `vlogsrt/project/config.py`**에만 있을 수 있다. 파생 계층의
비전 모델 리터럴(`glm-*`, `gemini-*`)은 **정확 경로 `vlogsrt/project/config.py`
하나**에만 있을 수 있다 — 새 튜너블은 전부 프로젝트 config에 살기 때문에 원본
`vlogsrt/config.py`는 비전 모델 예외를 물려받지 않는다. 중첩 경로의 같은 이름 파일
(`vlogsrt/project/tuning/config.py` 등)도 예외를 물려받지 못한다.

**공급자를 바꿔도 접두사 목록은 좁히지 않는다.** 2026-09-13에 기본 비전 공급자가
z.ai(`glm-5.3-flash`)에서 Google(`gemini-3.8-flash`)로 바뀌었지만 `glm-*`은 금지
목록에 그대로 남아 있다(D34). 쓰지 않게 된 접두사를 지우면 되돌리기가 조용히
커밋된다 — 이 검사는 "지금 쓰는 모델"이 아니라 "코드에 박힌 모델 이름"을 막는다.

## 8. 파생 데이터클래스 — frozen, 정확 경로에서

정확 경로 `vlogsrt/project/models.py`의 모든 데이터클래스는 `frozen=True`를
리터럴로 선언해야 한다. 장식자의 평범한 별명 — `import dataclasses as dc` 뒤의
`@dc.dataclass`, `from dataclasses import dataclass as dc` 뒤의 `@dc` — 도 같은
dataclass 선언으로 알아본다. `@dataclass`(인자 없음)와 `frozen=False`는 가변이므로
거부하고, `frozen=변수` 같은 비(非)리터럴과 한 번이라도 덮어쓰인 장식자 별명도
보수적으로 거부한다. **파일이 아직 없는 동안은 이 검사가 통과** — P0-04가 파일을
만들 때 이 선이 먼저 서 있다. frozen은 선언의 **얕은** 불변이다: 필드가 리스트 같은
가변 컨테이너를 들면 그 내용까지는 못 지킨다. 깊은 불변은 행동 테스트가 증명하는
몫이다.

## 9. 두 번째 모델 호출 — 소스·경계 전사마다 하나 (D39)

파생 계층의 전사는 글자 교정(D37)을 켠다. **소스 분석 하나·경계 요청 하나마다
gpt-transcribe 호출이 하나씩 더해진다** — 1차 전사와 같은 오디오, 같은 조각 경계를
다시 보므로 전사 호출 비용이 대략 두 배가 된다(분당 과금, 조각 수는 그대로). 되풀이
재전사·심판 호출은 되풀이가 있을 때만 붙지만, 이 호출은 되풀이가 없어도 **항상**
붙는다. `retext_enabled=false`면 부르지 않는다. 호출은 엔진 경계 안에서만 일어난다
— `speech.py`·`boundary.py`는 `TranscriptionJob`을 만들어 주입된 전사기에게 넘기고
직접 네트워크를 치지 않는다(§4).

호출 실패는 error 기록이 아니라 `RetextRecord(status="unavailable")` + WARN이다 —
whisper 글자로 완성물이 나간다. 재전사·심판·커터 실패가 타입 있는 error로 끝나는
것과 다른 선이다(그 셋은 골격 신뢰를 깨뜨린다).

`SourceTrace`·`BoundaryTranscript`의 `retext` 필드는 **선택**이다(`None` = 그 단계에
도달하지 못했음). 경계 완성물 `.BOUNDARY.json`에 기록이 실리므로, 같은 캐시 키의
재실행에서 교정 결과가 다르면 독점 출판 충돌(오류)로 끝난다 — 분석 키 도메인은 이
때문에 v2로 올라갔다. 비용 추정기(`inspect`)는 아직 없다 — P7-03의 몫이다.

## 10. 비전 판정 재생 — 출처는 계약 기록물에 남는다 (D40, V1)

비전 판정의 주입 경계는 `transcribe.VisionResponder`다(`LiveVisionResponder`·
`FixtureVisionResponder`·`ClosedVisionResponder` — 유료 경로는 실제 구현체 하나뿐).
`--vision-fixtures` 재생으로 나온 판정은 **진짜 판정이 아니고 그 사실이 산출물에
남아야 한다**:

- stdout 한 줄이 재생을 먼저 밝힌다.
- 재생 판정의 `reason`은 `재생 판정(픽스처) — ` 접두를 달고 `project_trace.json`의
  `decisions[].reason`에 실린다 — 이 접두는 **선택 필드가 아니라** 모양이 멀쩡한
  재생 답변마다 반드시 붙는다. `review.json`에는 재생 여부 필드를 두지 않는다:
  그 파일의 바이트는 `export.py`가 홀로 정하는데, 필드를 더하는 것은 곧
  `export.py` 수정이라 이 경로를 열려면 이 계약의 작성자 경계를 먼저 고쳐야 한다.
- 블록마다 `artifacts/vision/<block_id>.request.json` 덤프가 남는다 — base64 blob은
  프레임 설명으로 치환되고 보내는 페이로드는 건드리지 않는다.
- 재생 픽스처가 없으면 블록 오류 판정으로 세탁되지 않고 실행 실패로 닫힌다
  (`MissingVisionFixture` — 전사 픽스처 부재와 같은 선).

## 집행

`scripts/check_invariants.py`가 이 계약의 **구문 경계** — import 방향과 CLI 예외의
폭(§1), openai·subprocess 경로 예외(§4·§5), xmllint의 run 고정·두 argv 형태(§5),
선택 의존성의 vad 한정(§6), 모델 리터럴 경로(§7), 파생 데이터클래스 frozen(§8) —
을 AST로 기계 검사한다(19군). 이 검사는 **열거된 구문 경계만** 집행한다. 원본
기록 불변(§2)과 시각 투영(§3)은 구문 검사로 증명되지 않는다 — 그 둘은 행동
테스트가 지킨다. 일반적인 정적 분석 엔진이나 파이썬 보안 샌드박스를 주장하는
것도 아니다. 계약 위반 의심은 코드를 고치지 말고 **보고**한다 — 예외를 넓히는
수정은 되돌려진다.
