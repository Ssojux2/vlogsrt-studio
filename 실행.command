#!/bin/zsh

PROJECT_DIR="${0:A:h}"
if ! cd -- "$PROJECT_DIR"; then
  print -u2 -- "프로젝트 폴더로 이동하지 못했습니다."
  if [[ -t 0 ]]; then
    print -- "Enter 키를 누르면 창이 닫힙니다."
    IFS= read -r
  fi
  exit 1
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 2>/dev/null)"
  if [[ -z "$PYTHON" ]]; then
    print -u2 -- "Python 3를 찾을 수 없습니다. .venv를 만들거나 Python 3를 설치해 주세요."
    if [[ -t 0 ]]; then
      print -- "Enter 키를 누르면 창이 닫힙니다."
      IFS= read -r
    fi
    exit 127
  fi
fi

print -- "브이로그 자동화를 시작합니다."
print -- "종료하려면 Ctrl+C를 누르세요."

"$PYTHON" -u -m ui.app "$@"
app_exit_code=$?
if (( app_exit_code != 0 )); then
  print -u2 -- "프로그램이 오류 코드 ${app_exit_code}(으)로 종료되었습니다."
  if [[ -t 0 ]]; then
    print -- "Enter 키를 누르면 창이 닫힙니다."
    IFS= read -r
  fi
fi
exit "$app_exit_code"
