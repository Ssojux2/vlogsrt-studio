-- 사용자 지정 공유 대상 — Apple event 생명주기 smoke.
--
-- Apple 문서 "Responding to Apple Events from Final Cut Pro"의 시험 스크립트를
-- 이 앱 계약에 맞춘 것이다. Final Cut이 실제로 흘려보내는 그 순서를 그대로
-- 누른다: make(자리표시자) → location info · library info · metadata ·
-- data options(답). open(파일 수령)은 미디어 내보내기가 필요하니 smoke에서
-- 뺀다.
--
-- 실행 요건(2026-10 실측, macOS 26.6):
--   * 대상 앱은 **제대로 서명돼 설치된** 상태여야 한다 — 로컬 unsigned/개발
--     서명 번들은 이 시스템이 scripting terminology(sdef)를 실어주지 않는다
--     (옛날 빌드도 같이 실패한다; 이 게이트는 이번 변경과 무관하다).
--   * 처음 실행하면 "…이(가) …을(를) 제어하려 합니다" Automation 동의창이
--     뜬다 — 허용해야 이벤트 답이 돌아온다(동의 전엔 osascript가 멈춘다).
--
--   osascript macos/scripts/apple-event-smoke.applescript
--
-- 기대 결과: XML 전용 내보내기 답 — location info에 has media:false ·
-- has description:true, library info에 두 플래그 모두 false, metadata에
-- share.id, data options에 metadataSet "None"과 descriptionVersion "1.14".

on run
  -- 1) 친숙한 용어 형태 — terminology가 실리는 환경(설치본)에서 쓴다.
  try
    tell application id "com.ssojux2.vlogsrt"
      make new asset with properties {name:"SmokeAsset", metadata:{|com.apple.proapps.share.episodeID|:"SmokeEpisode"}, data options:{|availableMetadataSets|:{"Camera View", "None"}, |availableVersions|:{"1.13", "1.14", "1.15"}}}
      set newAsset to result
      set theLocation to location info of newAsset
      set theLibraryLocation to library info of newAsset
      set theMetadata to metadata of newAsset
      set theDataOptions to data options of newAsset
      return {theLocation, theLibraryLocation, theMetadata, theDataOptions}
    end tell
  on error friendlyError number friendlyNumber
    -- 2) 원시 이벤트 형태 — terminology 없이도 Apple event 계약 자체를 잡는다.
    --    파라미터·레코드 라벨은 4자 코드(kocl·prdt·pnam·meta·dopt)로 적는다.
    try
      tell application id "com.ssojux2.vlogsrt"
        «event corecrel» «class kocl» «class aset» «class prdt» {«class pnam»"SmokeAsset", «class meta»{|com.apple.proapps.share.episodeID|:"SmokeEpisode"}, «class dopt»{|availableMetadataSets|:{"Camera View", "None"}, |availableVersions|:{"1.13", "1.14", "1.15"}}}
        set assetRef to result
        «event coregetd» {«class want»«class prop», «class form»«class prop», «class seld»«class locn», «class from»assetRef}
      end tell
    on error rawError number rawNumber
      error "smoke 실패 — 용어 형태: " & friendlyNumber & " " & friendlyError & " / 원시 형태: " & rawNumber & " " & rawError
    end try
  end try
end run
