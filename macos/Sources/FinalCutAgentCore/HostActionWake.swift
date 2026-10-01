import AppKit
import Foundation

// ─────────────────────────────────────────────────────────────
//  호스트 깨움 운반 — 우편함 동작을 호스트에 알리는 길
//
//  SDK 호스트(ProExtensionRequestHandling)는 extension 문맥을 주 클래스
//  NSViewController에 주입하지 않는다 — controller.extensionContext는 nil이고,
//  그것을 문지기로 삼던 열기는 우편함에 아무것도 남기지 못한 채 동기 실패로
//  닫혔다(권한 요청 실패가 누름과 함께 떴다). 권위는 이미 우편함에 있으므로
//  깨움만 문지기와 무관하면 된다. NSWorkspace URL 열기는
//  APPLICATION_EXTENSION_API_ONLY 아래에서도 쓸 수 있는 AppKit 운반이다.
// ─────────────────────────────────────────────────────────────

/// 깨움 운반의 주입점 — 시험은 가짜로 갈아끼워 호출·순서·결과를 기록한다.
public struct HostWakeTransport: Sendable {
  public let open: @MainActor (URL, @escaping @Sendable (Bool) -> Void) -> Void

  public init(
    open: @escaping @MainActor (URL, @escaping @Sendable (Bool) -> Void) -> Void
  ) {
    self.open = open
  }

  /// 기본 운반 — NSWorkspace로 호스트 URL을 연다(확장 프로세스에서 쓴다).
  /// 결과는 열기 성공 여부뿐이다 — 무엇이 배달되는지는 우편함이 정한다.
  public static func workspace() -> HostWakeTransport {
    HostWakeTransport { url, completion in
      NSWorkspace.shared.open(
        url, configuration: NSWorkspace.OpenConfiguration()
      ) { _, error in
        completion(error == nil)
      }
    }
  }
}

/// 확장이 호스트 동작을 맡기는 문 — 넣기가 먼저, 깨움은 나중.
///
/// 순서가 계약이다: 우편함에 남기는 데 실패하면 깨우지 않는다(배달할 동작
/// 없는 깨움은 헛된 시작이다). 반대로 깨움이 실패해도 동작은 우편함에 남는다
/// — 호스트는 시작·깨움마다 우편함을 소비하므로 늦게라도 온다.
public enum HostActionOpener {
  @discardableResult
  @MainActor
  public static func open(
    url: URL,
    workspace: AppGroupWorkspace,
    wake: HostWakeTransport = .workspace(),
    completion: (@Sendable (Bool) -> Void)? = nil
  ) throws -> HostActionEnvelope {
    let envelope = try HostActionMailbox.enqueue(url: url, workspace: workspace)
    wake.open(url) { opened in
      completion?(opened)
    }
    return envelope
  }
}
