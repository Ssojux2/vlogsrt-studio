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
  print 'operation=${1:?operation required}'
  print 'job_id=${2:?job id required}'
  print 'job_dir=${3:?job directory required}'
  print '[[ "$job_id" == */* || "$job_id" == *..* ]] && exit 2'
  print 'workspace="$job_dir/workspace"'
  print 'cd "$repo_root"'
  print 'if [[ ! -f "$workspace/workflow.jsonl" ]]; then'
  print '  "$python_path" -m vlogsrt project receive "$job_dir/input.fcpxml" --workspace "$workspace"'
  print 'fi'
  print 'case "$operation" in'
  print '  analyze)'
  print '    if [[ ! -f "$workspace/analysis-estimate.jsonl" ]]; then'
  print '      "$python_path" -m vlogsrt project inspect "$workspace"'
  print '    fi'
  print '    if [[ -f "$job_dir/analysis-authorization.jsonl" ]]; then'
  print '      "$python_path" -m vlogsrt project analyze "$workspace" --online --authorization "$job_dir/analysis-authorization.jsonl"'
  print '    fi'
  print '    ;;'
  print '  roughcut)'
  print '    [[ -f "$job_dir/edit-plan.jsonl" ]] && cp "$job_dir/edit-plan.jsonl" "$workspace/edit-plan.jsonl"'
  print '    "$python_path" -m vlogsrt project roughcut "$workspace" -o "$job_dir/roughcut.fcpxml"'
  print '    ;;'
  print '  *) exit 2 ;;'
  print 'esac'
  print 'for name in analysis-estimate.jsonl analysis-attempts.jsonl candidates.jsonl vision-decisions.jsonl; do'
  print '  [[ -f "$workspace/$name" ]] && cp "$workspace/$name" "$job_dir/$name"'
  print 'done'
} > "$wrapper"
chmod 700 "$wrapper"
print "$wrapper"
