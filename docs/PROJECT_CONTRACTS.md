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

**유일한 예외**: `vlogsrt/cli.py` (정확 경로). 문서화된 CLI 조립 진입점으로서
project 패키지를 import할 수 있다. 이 예외는 다른 어떤 레거시 모듈에도 확장되지
않는다. 소스 엔진(`transcribe`, `preprocess`, `repair`, `repetition`, `refine`,
`output`, `layout`, `polish`, `pipeline`, `models`, `config`, `env`, `log`,
`cutpoints`, `hangul`, `keywords`, `evaluate`)과 `verify.py`는 project를 import할 수 없다.

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
  무관하게 애셋 하나의 시각 그대로 저장되고, 다시 쓰이지 않는다.
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

- 실행파일은 `shutil.which("xmllint")` 결과로만 얻는다. `which`에 다른 이름을
  묻는 것도, 상수·변수로 실행파일을 박는 것도 금지다.
- `which`의 **출처까지 본다**. `import shutil [as …]`(모듈 별명)과
  `from shutil import which [as …]`(함수 별명)만 인정하고, 그 밖의 객체에 붙은
  `.which`나 덮어쓰인(shadow) `which`는 출처 불명으로 거부한다.
- argv는 **리터럴 리스트/튜플**로 쓴다. 첫 항목은 그 실행파일이고, 원소를
  f-string·문자열 연결·starred로 조립하지 않는다.
- argv는 **유일한 위치 인자**다. `run`은 남는 위치 인자를 `Popen`에 그대로 넘기므로
  뒤쪽 슬롯(bufsize·`executable`)으로 실행파일이 흘러드는 것을 이 규칙으로 끊는다.
  `executable=` 키워드 오버라이드와 `**` 전개도 금지다.
- 셸은 금지다 — `shell` 키워드는 생략하거나 리터럴 `False`만.
- `from subprocess import *`와 `from shutil import *`는 호출·출처 추적을
  무력화하므로 금지다.

별명 우회(`import subprocess as sp; sp.run(...)`, `from subprocess import run, Popen,
check_output`)도 잡는다. 경계 밖의 그 어떤 파일도 subprocess를 import할 수 없다.

검사가 인정하는 정적 형태는 정해져 있다: 참 shutil 묶음(별명 포함)의
`which("xmllint")`로 얻은 실행파일, 리터럴 argv, 유일한 위치 인자, 생략되거나
리터럴 `False`인 `shell`. 이 기계 검사는 **우발적 드리프트를 막는 난간**이지
일반적인 파이썬 보안 샌드박스가 아니다 — 결심한 우회까지 막지는 못한다.

## 6. 선택 의존성 — project 아래에서만

`[project]` dependencies는 정확히 `openai`, `tomli-w`, `rich` 셋이다(불변 규칙 7).
선택 extra `project = ["numpy>=2.0", "onnxruntime>=1.19"]`는 파생 계층의 런타임
(VAD 등)이고, **import는 `vlogsrt/project/` 아래에서만** 허용된다. 레거시
`vlogsrt/*.py`와 `ui/**`에서는 별명 형태(`import numpy as np`,
`from numpy import ndarray`, `import numpy.typing`)까지 전부 금지다. 그 외 금지
의존성(`jamo`, `konlpy`, `pysrt`, `ffmpeg`, `pydantic`, …)은 project 안을 포함한
어디서도 허용되지 않는다.

## 집행

`scripts/check_invariants.py`가 이 계약의 **구문 경계** — import 방향(§1), 경로
예외(§4·§5), 의존성 범위(§6), xmllint 호출 형태(§5) — 를 AST로 기계 검사한다(18군).
원본 기록 불변(§2)과 시각 투영(§3)은 구문 검사로 증명되지 않는다 — 그 둘은
행동 테스트가 지킨다. 계약 위반 의심은 코드를 고치지 말고 **보고**한다 —
예외를 넓히는 수정은 되돌려진다.
