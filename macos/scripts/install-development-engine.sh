#!/bin/zsh -f
# -f(시작 파일 억제): 이 스크립트와 아래 생성되는 래퍼는 사용자·시스템 zsh
# 설정을 읽지 않고 돈다 — 시작 파일이 자식 환경을 바꾸는 통로를 닫는다(S2A-2
# 시험 격리). 남는 경계는 하나뿐이다: zsh은 -f여도 /etc/zshenv은 항상 읽는다.
# 그 파일은 시스템 소유이므로 **밝혀두되 들여다보지 않는다**(검사 금지 계약).
set -eu

script_dir=${0:A:h}
default_repo=${script_dir:h:h}
repo_root=${1:-$default_repo}
destination=${2:-"$HOME/Library/Application Scripts/com.ssojux2.vlogsrt"}
python_path="$repo_root/.venv/bin/python"

if [[ ! -x "$python_path" ]]; then
  git_common=$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [[ -n "$git_common" && -x "${git_common:h}/.venv/bin/python" ]]; then
    python_path="${git_common:h}/.venv/bin/python"
  else
    print -u2 "개발용 Python이 없습니다: $python_path"
    exit 2
  fi
fi
if [[ ! -f "$repo_root/pyproject.toml" ]]; then
  print -u2 "vlogsrt 저장소가 아닙니다: $repo_root"
  exit 2
fi

mkdir -p "$destination"
wrapper="$destination/vlogsrt-engine"
escaped_repo=${repo_root//\/\\}
escaped_repo=${escaped_repo//\"/\\\"}
{
  # 생성되는 래퍼도 -f로 — 시작 파일 억제는 설치 스크립트만의 성질이 아니다.
  print '#!/bin/zsh -f'
  print 'set -eu'
  print "repo_root=\"$escaped_repo\""
  escaped_python=${python_path//\/\\}
  escaped_python=${escaped_python//\"/\\\"}
  print "python_path=\"$escaped_python\""
  print '# 번들 엔진과 같은 CLI 계약을 공유 파이썬 진입점에 위임한다 — 흐름을'
  print '# 두 벌로 유지하면 스테이징·취소·결과 동기화가 어긋난다(S1A).'
  print 'operation=${1:?operation required}'
  print 'job_id=${2:?job id required}'
  print 'job_dir=${3:?job directory required}'
  print '[[ "$job_id" == */* || "$job_id" == *..* ]] && exit 2'
  print 'cd "$repo_root"'
  print 'export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"'
  # 넷째 인자는 개발 실행기의 상태 기록 경로다(선택) — 있을 때만 앞으로 넘긴다.
  # 다섯째 인자는 stdin 봉투 마커다(선택, 비밀 아님) — 실행기가 자격 증명을
  # 사적 stdin 통로로 운반할 때만 붙는다(S2A-2).
  print '"$python_path" "$repo_root/macos/scripts/vlogsrt-engine.py" "$operation" "$job_id" "$job_dir" ${4:+"$4"} ${5:+"$5"}'
} > "$wrapper"
chmod 700 "$wrapper"
print "$wrapper"
