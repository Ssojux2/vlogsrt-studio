import Foundation
import Testing

@testable import FinalCutAgentCore

/// App Group 식별자는 macOS Personal Team 형식(`<TeamID>.<group name>`)이어야 한다.
/// iOS식 `group.` 접두사는 서명 entitlement와 무관하게 containermanagerd가
/// TCC 보호 컨테이너 요청으로 거부한다 — 이 값이 곧 배포 계약이다.
@Test func appGroupIdentifierUsesMacOSTeamPrefixedForm() {
  #expect(AppGroupWorkspace.identifier == "5955379R8P.com.ssojux2.vlogsrt")
}

/// 소스 상수와 두 entitlements가 같은 정본 식별자를 말해야 한다 —
/// 어느 한쪽만 바뀌면 서명은 되지만 컨테이너 조회가 서로 다른 디렉터리를 본다.
@Test func entitlementsMatchCanonicalAppGroupIdentifier() throws {
  let macosRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent() // FinalCutAgentCoreTests
    .deletingLastPathComponent() // Tests
    .deletingLastPathComponent() // macos
  for resource in ["Resources/FinalCutAgent.entitlements", "Resources/WorkflowExtension.entitlements"] {
    let plist = try String(contentsOf: macosRoot.appendingPathComponent(resource), encoding: .utf8)
    #expect(
      plist.contains(AppGroupWorkspace.identifier),
      "\(resource)에 소스와 같은 App Group 식별자가 없다"
    )
    #expect(
      !plist.contains("group.com.ssojux2.vlogsrt"),
      "\(resource)에 거부되는 iOS식 group. 식별자가 남아 있다"
    )
  }
}
