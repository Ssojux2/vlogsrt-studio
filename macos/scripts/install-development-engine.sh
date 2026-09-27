#!/bin/zsh
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
  print '#!/bin/zsh'
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
  print '"$python_path" "$repo_root/macos/scripts/vlogsrt-engine.py" "$operation" "$job_id" "$job_dir"'
} > "$wrapper"
chmod 700 "$wrapper"
print "$wrapper"
