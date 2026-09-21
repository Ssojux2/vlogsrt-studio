#!/bin/bash
# macOS Xcode packaging 검사 (Task X-01A).
#
# unsigned(CODE_SIGNING_ALLOWED=NO) 빌드만 검사한다. 서명·공증은 이 스크립트의
# 범위 밖이다. 실패 로그는 /private/tmp/vlogsrt-x01-*.log 에 남긴다.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPEC="$ROOT/macos/project.yml"
PROJ="$ROOT/macos/FinalCutAgent.xcodeproj"
PBXPROJ="$PROJ/project.pbxproj"
DERIVED=/private/tmp/vlogsrt-x01-derived
APP="$DERIVED/Build/Products/Debug/FinalCutAgent.app"
APPEX="$APP/Contents/PlugIns/FinalCutAgentWorkflowExtension.appex"
APP_EXEC="$APP/Contents/MacOS/FinalCutAgent"
APPEX_EXEC="$APPEX/Contents/MacOS/FinalCutAgentWorkflowExtension"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "PASS  $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL  $1"; }

expect_file() { # expect_file <설명> <경로>
  if [[ -f "$2" ]]; then pass "$1"; else fail "$1 ($2 없음)"; fi
}

expect_bundle() { # expect_bundle <설명> <경로> — bundle은 디렉터리다
  if [[ -d "$2" ]]; then pass "$1"; else fail "$1 ($2 없음)"; fi
}

expect_value() { # expect_value <설명> <실제값> <기대값>
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (실제: '$2' / 기대: '$3')"; fi
}

expect_contains() { # expect_contains <설명> <본문> <부분문자열>
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1 ('$3' 없음)"; fi
}

# ─── 1. 정본과 생성물 ──────────────────────────────────────────

expect_file "project.yml 정본 존재" "$SPEC"
if [[ ! -f "$PBXPROJ" ]]; then
  fail ".xcodeproj 생성됨 ($PROJ 없음)"
  echo
  echo "총 $((PASS + FAIL))건: PASS $PASS / FAIL $FAIL"
  exit 1
fi
pass ".xcodeproj 생성됨"

# ─── 2. 생성 결정성 ────────────────────────────────────────────

BEFORE="$(shasum -a 256 "$PBXPROJ" | cut -d' ' -f1)"
if ! xcodegen generate --spec "$SPEC" --project "$ROOT/macos" \
  > /private/tmp/vlogsrt-x01-xcodegen.log 2>&1; then
  fail "xcodegen generate 2회차 (로그: /private/tmp/vlogsrt-x01-xcodegen.log)"
else
  AFTER="$(shasum -a 256 "$PBXPROJ" | cut -d' ' -f1)"
  expect_value "재생성 결정성 (pbxproj 동일)" "$AFTER" "$BEFORE"
  pass "xcodegen generate 2회차"
fi

if (cd "$ROOT" && git diff --exit-code -- macos/FinalCutAgent.xcodeproj \
  > /dev/null 2>&1); then
  pass "재생성 뒤 git diff 없음 (.xcodeproj)"
else
  fail "재생성 뒤 git diff 없음 (.xcodeproj)"
fi

# ─── 3. plist · entitlements 문법 ──────────────────────────────

for plist in \
  "$ROOT/macos/Resources/App-Info.plist" \
  "$ROOT/macos/Resources/WorkflowExtension-Info.plist" \
  "$ROOT/macos/Resources/FinalCutAgent.entitlements" \
  "$ROOT/macos/Resources/WorkflowExtension.entitlements"; do
  if plutil -lint "$plist" > /dev/null 2>&1; then
    pass "plutil -lint $(basename "$plist")"
  else
    fail "plutil -lint $(basename "$plist")"
  fi
done

expect_contains \
  "소스 plist bundle id 변수 사용 (app)" \
  "$(cat "$ROOT/macos/Resources/App-Info.plist" 2>/dev/null)" \
  '$(PRODUCT_BUNDLE_IDENTIFIER)'
expect_contains \
  "소스 plist bundle id 변수 사용 (appex)" \
  "$(cat "$ROOT/macos/Resources/WorkflowExtension-Info.plist" 2>/dev/null)" \
  '$(PRODUCT_BUNDLE_IDENTIFIER)'
# XcodeGen의 entitlements 재생성이 내용을 비우는 사고(실제로 한 번 발생)를
# 재생성 뒤에 잡는다. App Group은 계약값이다.
expect_contains \
  "app entitlements App Group 보존" \
  "$(cat "$ROOT/macos/Resources/FinalCutAgent.entitlements" 2>/dev/null)" \
  "group.com.ssojux2.vlogsrt"
expect_contains \
  "appex entitlements App Group 보존" \
  "$(cat "$ROOT/macos/Resources/WorkflowExtension.entitlements" 2>/dev/null)" \
  "group.com.ssojux2.vlogsrt"
# extension은 XML 변환도 Python 엔진 실행도 하지 않는다(X-01B).
PROCESS_HITS=$(grep -cE "Process\(|EngineProcess" \
  "$ROOT/macos/Sources/WorkflowExtension/WorkflowViewController.swift" 2>/dev/null || true)
expect_value "extension source에 Python Process 실행 없음" "${PROCESS_HITS:-0}" "0"

# ─── 4. unsigned 빌드 ──────────────────────────────────────────

if xcodebuild -project "$PROJ" \
  -scheme FinalCutAgent -configuration Debug \
  -derivedDataPath "$DERIVED" \
  CODE_SIGNING_ALLOWED=NO clean build \
  > /private/tmp/vlogsrt-x01-build.log 2>&1; then
  pass "xcodebuild clean build (unsigned)"
else
  fail "xcodebuild clean build (unsigned) (마지막 80줄 ↓)"
  tail -n 80 /private/tmp/vlogsrt-x01-build.log
fi

# compiler 진단은 "file:line:col: warning:", linker 진단은 "ld: warning:" 형태다.
# appintentsmetadataprocessor의 "Metadata extraction skipped"는 둘 다 아니며
# 끄는 build setting도 없다 — 계약의 "compiler/linker warning 0건"에서 제외한다.
WARNINGS=$(grep -cE ": warning:|^ld: warning:" /private/tmp/vlogsrt-x01-build.log 2>/dev/null || true)
expect_value "compiler/linker warning 0건" "${WARNINGS:-0}" "0"

# ─── 5. bundle 검사 ────────────────────────────────────────────

expect_bundle "FinalCutAgent.app 존재" "$APP"
expect_file "app executable 존재" "$APP_EXEC"
if [[ -f "$APP_EXEC" ]] && file "$APP_EXEC" | grep -q "Mach-O"; then
  pass "app executable Mach-O"
elif [[ -f "$APP_EXEC" ]]; then
  fail "app executable Mach-O ($(file "$APP_EXEC"))"
else
  fail "app executable Mach-O (파일 없음)"
fi

expect_bundle "PlugIns/FinalCutAgentWorkflowExtension.appex embed" "$APPEX"
expect_file "appex executable 존재" "$APPEX_EXEC"
if [[ -f "$APPEX_EXEC" ]] && file "$APPEX_EXEC" | grep -q "Mach-O"; then
  pass "appex executable Mach-O"
elif [[ -f "$APPEX_EXEC" ]]; then
  fail "appex executable Mach-O ($(file "$APPEX_EXEC"))"
else
  fail "appex executable Mach-O (파일 없음)"
fi

APP_PLIST="$APP/Contents/Info.plist"
APPEX_PLIST="$APPEX/Contents/Info.plist"
if [[ -f "$APP_PLIST" ]]; then
  expect_value "app plist bundle id" \
    "$(plutil -extract CFBundleIdentifier raw "$APP_PLIST" 2>/dev/null)" \
    "com.ssojux2.vlogsrt"
else
  fail "app plist bundle id (plist 없음)"
fi
if [[ -f "$APPEX_PLIST" ]]; then
  expect_value "appex plist bundle id" \
    "$(plutil -extract CFBundleIdentifier raw "$APPEX_PLIST" 2>/dev/null)" \
    "com.ssojux2.vlogsrt.workflow"
  expect_value "appex extension point" \
    "$(plutil -extract NSExtension.NSExtensionPointIdentifier raw "$APPEX_PLIST" 2>/dev/null)" \
    "com.apple.FinalCut.WorkflowExtension"
  expect_value "appex principal class" \
    "$(plutil -extract NSExtension.ProExtensionPrincipalViewControllerClass raw "$APPEX_PLIST" 2>/dev/null)" \
    "FinalCutAgentWorkflowExtension.WorkflowViewController"
else
  fail "appex plist bundle id (plist 없음)"
  fail "appex extension point (plist 없음)"
  fail "appex principal class (plist 없음)"
fi
if [[ -f "$APP_PLIST" ]]; then
  # 주의: plutil -extract <keypath> json <file>은 -o가 없으면 파일을 제자리에서
  # 추출 결과로 덮어쓴다. 반드시 -o - (stdout)으로 읽는다.
  URL_SCHEMES=$(plutil -convert json -o - "$APP_PLIST" 2>/dev/null \
    | grep -c '"vlogsrt-agent"' || true)
  expect_value "app plist vlogsrt-agent URL scheme 1개" "${URL_SCHEMES:-0}" "1"
else
  fail "app plist vlogsrt-agent URL scheme 1개 (plist 없음)"
fi

# ─── 6. build setting 계약 ─────────────────────────────────────

show_setting() { # show_setting <target> <키>
  xcodebuild -project "$PROJ" -target "$1" -configuration Debug -showBuildSettings \
    2> /dev/null | awk -v key="    $2 = " -v name="$2" \
    'index($0, key) == 1 { sub("^    " name " = ", ""); print; exit }'
}

expect_value "app PRODUCT_BUNDLE_IDENTIFIER" \
  "$(show_setting FinalCutAgent PRODUCT_BUNDLE_IDENTIFIER)" \
  "com.ssojux2.vlogsrt"
expect_value "app MACOSX_DEPLOYMENT_TARGET" \
  "$(show_setting FinalCutAgent MACOSX_DEPLOYMENT_TARGET)" \
  "14.0"
expect_value "appex PRODUCT_BUNDLE_IDENTIFIER" \
  "$(show_setting FinalCutAgentWorkflowExtension PRODUCT_BUNDLE_IDENTIFIER)" \
  "com.ssojux2.vlogsrt.workflow"
expect_value "appex ADDITIONAL_SDKS" \
  "$(show_setting FinalCutAgentWorkflowExtension ADDITIONAL_SDKS)" \
  "/Library/Developer/SDKs/WorkflowExtensionSDK.sdk"
expect_value "appex LD_ENTRY_POINT" \
  "$(show_setting FinalCutAgentWorkflowExtension LD_ENTRY_POINT)" \
  "_ProExtensionMain"
expect_contains "appex OTHER_LDFLAGS -fapplication-extension" \
  "$(show_setting FinalCutAgentWorkflowExtension OTHER_LDFLAGS)" \
  "-fapplication-extension"
expect_contains "appex OTHER_LDFLAGS -lProExtension" \
  "$(show_setting FinalCutAgentWorkflowExtension OTHER_LDFLAGS)" \
  "-lProExtension"
expect_value "appex APPLICATION_EXTENSION_API_ONLY" \
  "$(show_setting FinalCutAgentWorkflowExtension APPLICATION_EXTENSION_API_ONLY)" \
  "YES"

echo
echo "총 $((PASS + FAIL))건: PASS $PASS / FAIL $FAIL"
[[ $FAIL -eq 0 ]]
