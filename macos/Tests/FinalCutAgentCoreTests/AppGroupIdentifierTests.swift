import Foundation
import Testing

@testable import FinalCutAgentCore

/// App Group 식별자는 macOS Personal Team 형식(`<TeamID>.<group name>`)이다.
/// 프로비저닝된 `group.` 식별자도 macOS에서 지원되지만, 이 Personal Team 빌드의
/// embedded provisioning profile에는 com.apple.security.application-groups가
/// 없어 containermanagerd가 그 group을 거부했다 — 이 값이 곧 배포 계약이다.
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
      "\(resource)에 이전 iOS식 group. 식별자가 남아 있다"
    )
  }
}

@Test func workflowExtensionCanReadUserSelectedFCPXML() throws {
  let macosRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
  let entitlements = try String(
    contentsOf: macosRoot.appendingPathComponent("Resources/WorkflowExtension.entitlements"),
    encoding: .utf8
  )
  #expect(entitlements.contains("com.apple.security.files.user-selected.read-only"))
  #expect(entitlements.contains("<true/>"))
}
