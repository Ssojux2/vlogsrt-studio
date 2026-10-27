#!/bin/zsh
# 맥에서 더블클릭으로 작업대를 띄운다(D100 §8). 처음이면 .venv를 만들고 꾸러미를 깐다.
# 판단(설치 표지·꾸러미·ffmpeg 확인)은 scripts/launch_check.py가 한다 — 실행.bat과 같은 판단이다.

pause_and_exit() {
  local code=$1
  if [[ -t 0 ]]; then
    print -- "Enter 키를 누르면 창이 닫힙니다."
    IFS= read -r
  fi
  exit "$code"
}

fail() {
  print -u2 -- "$1"
  pause_and_exit "${2:-1}"
}

PROJECT_DIR="${0:A:h}"
cd -- "$PROJECT_DIR" || fail "프로젝트 폴더로 이동하지 못했습니다."

# Finder에서 연 창은 로그인 셸 PATH를 다 받지 못한다 — 있는 Homebrew 자리만 앞에 붙인다.
export PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
typeset -U path
for brew_bin in /usr/local/bin /opt/homebrew/bin; do
  [[ -d "$brew_bin" ]] && path=("$brew_bin" $path)
done

python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_base_python() {
  local name found
  for name in python3.13 python3.12 python3.11 python3; do
    found="$(command -v "$name" 2>/dev/null)" || continue
    if [[ -n "$found" ]] && python_ok "$found"; then
      print -r -- "$found"
      return 0
    fi
  done
  return 1
}

print -- "브이로그 작업대를 준비합니다."

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ -x "$PYTHON" ]]; then
  python_ok "$PYTHON" || fail ".venv의 Python이 3.11보다 오래됐습니다. .venv 폴더를 지운 뒤 다시 실행하세요."
else
  BASE_PYTHON="$(find_base_python)" \
    || fail "Python 3.11 이상이 필요합니다: brew install python@3.12" 127
  print -- "처음 실행이라 실행 환경(.venv)을 만듭니다. 몇 분 걸릴 수 있습니다."
  "$BASE_PYTHON" -m venv "$PROJECT_DIR/.venv" || fail "실행 환경(.venv)을 만들지 못했습니다."
fi

if ! "$PYTHON" scripts/launch_check.py stamp-ok; then
  print -- "필요한 꾸러미를 설치합니다. 인터넷 연결이 필요하고 몇 분 걸릴 수 있습니다."
  "$PYTHON" -m pip install --upgrade pip || print -u2 -- "pip 업그레이드에 실패했습니다. 지금 pip로 계속합니다."
  if "$PYTHON" -m pip install -e ".[ui,project]"; then
    "$PYTHON" scripts/launch_check.py write-stamp
    print -- "설치를 마쳤습니다."
  elif "$PYTHON" scripts/launch_check.py importable; then
    # 오프라인 등으로 다시 설치만 실패했다 — 이미 깔린 꾸러미로 띄우고 다음 실행에서 다시 시도한다.
    print -u2 -- "꾸러미 설치에 실패했지만 이미 깔린 것으로 계속합니다. 다음 실행에서 다시 시도합니다."
  else
    fail "꾸러미 설치에 실패했습니다. 위 오류와 인터넷 연결을 확인하세요."
  fi
fi

"$PYTHON" scripts/launch_check.py tools || pause_and_exit 1

print -- "브이로그 작업대를 시작합니다. 잠시 뒤 브라우저가 열립니다."
print -- "종료하려면 Ctrl+C를 누르세요."

"$PYTHON" -u -m ui.app "$@"
app_exit_code=$?
if (( app_exit_code != 0 )); then
  print -u2 -- "프로그램이 오류 코드 ${app_exit_code}(으)로 종료되었습니다."
  pause_and_exit "$app_exit_code"
fi
exit "$app_exit_code"
