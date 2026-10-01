#!/bin/zsh -f
# STAB-S4B 프로토타입 데모 — Final Cut 없이 자막 세로 흐름을 앱으로 돌아본다.
#
# 하는 일:
#   1. 합성 최종 내보내기 한 쌍(48kHz PCM 무음 WAV + uid 있는 FCPXML)을 만든다.
#   2. 앱의 원본 지도(origin-map.jsonl)에 데모 UID 매핑을 심는다 — 실제로는
#      확장의 "가져오기 완료로 표시"가 남기는 기록이다(S2C).
#   3. 개발 엔진 래퍼가 없으면 설치하고, 앱으로 WAV+FCPXML을 연다(open) —
#      앱의 공유 받기가 job을 만들고 caption-estimate를 자동으로 돌린다.
#
# 이 뒤는 사람이 한다: 앱 창에서 "자막 승인·실행" 클릭 → caption-run(유료,
# 4초 무음 기준 약 $0.01) → 결과·경고·"SRT Finder에 보이기".
#
# 쓰임: prototype-caption-demo.sh [앱 경로] [길이(초, 기본 4)]
set -eu

script_dir=${0:A:h}
repo_root=${script_dir:h:h}
app_path=${1:-"/private/tmp/vlogsrt-x01-derived/Build/Products/Debug/FinalCutAgent.app"}
seconds=${2:-4}
uid="PROTOTYPE-CAPTION-UID-1"
group_root="$HOME/Library/Group Containers/5955379R8P.com.ssojux2.vlogsrt"

if [[ ! -d "$app_path" ]]; then
  print -u2 "앱이 없다: $app_path"
  print -u2 "먼저 빌드: scripts/check_macos_xcode.sh 또는 README의 xcodebuild 명령"
  exit 2
fi

# 개발 엔진 래퍼 — 없으면 설치한다(멱등).
wrapper="$HOME/Library/Application Scripts/com.ssojux2.vlogsrt/vlogsrt-engine"
if [[ ! -x "$wrapper" ]]; then
  "$repo_root/macos/scripts/install-development-engine.sh" "$repo_root" >/dev/null
fi

# 합성 재료 — 무음이라 caption-run은 '발화 없음(성공)'으로 끝난다.
stage=$(mktemp -d "${TMPDIR:-/tmp}/vlogsrt-caption-demo.XXXXXX")
"$repo_root/.venv/bin/python" - "$stage" "$seconds" "$uid" <<'PY'
import sys
import wave
from pathlib import Path

stage, seconds, uid = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
frames = 48_000 * seconds
with wave.open(str(stage / "mix.wav"), "wb") as stream:
    stream.setnchannels(1)
    stream.setsampwidth(2)
    stream.setframerate(48_000)
    stream.writeframes(b"\0\0" * frames)
(stage / "edit.fcpxml").write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
    '<fcpxml version="1.14"><resources/>'
    '<library><event name="데모"><project name="프로토타입 자막" uid="%s">'
    '<sequence format="r1" duration="%ds" tcStart="0s"><spine/>'
    "</sequence></project></event></library></fcpxml>\n" % (uid, seconds),
    encoding="utf-8",
)
PY

# 원본 지도에 데모 매핑을 심는다(같은 원본이면 중복 기록은 갱신으로 읽힌다).
mkdir -p "$group_root"
printf '%s\n' \
  "{\"origin_job_id\":\"prototype-roughcut\",\"origin_revision\":\"$(printf 'a%.0s' {1..64})\",\"project_uid\":\"$uid\",\"recorded_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
  >> "$group_root/origin-map.jsonl"

print "준비 완료:"
print "  재료: $stage/mix.wav · $stage/edit.fcpxml"
print "  원본 지도: $group_root/origin-map.jsonl (UID $uid)"
print "앱을 열어 공유 받기를 시작합니다 — 이어서 앱 창에서 승인하세요."
open -a "$app_path" "$stage/mix.wav" "$stage/edit.fcpxml"
