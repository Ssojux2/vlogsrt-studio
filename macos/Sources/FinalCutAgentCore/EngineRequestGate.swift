import Foundation

/// 같은 job 디렉터리의 동시 실행 요청을 하나로 묶는 앱 게이트(S2A-1).
///
/// 프로세스를 건너뛰는 중복은 엔진 진입점의 OS 잠금(flock)이 막는다 — 이
/// 게이트는 앱 안의 URL·UI 요청 전체(스테이징·상태 쓰기·실행·동기화·import)를
/// 소유권 아래 둔다. 청구는 동기적이고 원자적이다(MainActor 직렬화). 서로 다른
/// job 디렉터리는 독립이라 함께 돌 수 있다 — 전역 잠금이 아니다. 상태는 전부
/// MainActor 안에 갇혀 있어 Sendable이다.
@MainActor
public final class EngineRequestGate: Sendable {
  /// job 디렉터리의 정준 키 — symlink·`/var`·`/private/var` 같은 별칭 표기는
  /// 같은 키가 된다. 글자가 같은 job_id도 디렉터리가 다르면 다른 job이다.
  public static func ownershipKey(for jobURL: URL) -> String {
    jobURL.resolvingSymlinksInPath().standardizedFileURL.path
  }

  private var heldKeys: Set<String> = []

  public init() {}

  public func isHeld(jobURL: URL) -> Bool {
    heldKeys.contains(Self.ownershipKey(for: jobURL))
  }

  /// 정상 청구면 키를 돌려준다. 이미 잡고 있으면 nil — 첫 소유권을 건드리지 않는다.
  public func tryClaim(jobURL: URL) -> String? {
    let key = Self.ownershipKey(for: jobURL)
    guard !heldKeys.contains(key) else { return nil }
    heldKeys.insert(key)
    return key
  }

  public func release(_ key: String) {
    heldKeys.remove(key)
  }

  /// 요청 전체를 소유권 아래 실행한다 — 같은 job의 동시 요청은 본문을 시작조차
  /// 하지 않고 alreadyRunning으로 물러난다. 해제는 성공·실행 실패·취소·던지는
  /// 오류 어떤 끝에서든 놓치지 않는다(defer). 본문은 항상 MainActor에서 돈다 —
  /// main.swift의 요청 본문이 그 액터의 상태(UI 표시)를 다루기 때문이다.
  public func perform(
    jobURL: URL,
    _ request: @MainActor () async throws -> EngineSessionOutcome
  ) async rethrows -> EngineSessionOutcome {
    guard let key = tryClaim(jobURL: jobURL) else { return .alreadyRunning }
    defer { release(key) }
    return try await request()
  }
}

/// 실행 중 엔진 job의 대시보드 — job별 취소 문맥과 '현재' 지시자(S2A-1).
///
/// 서로 다른 job은 함께 돌 수 있으므로(게이트가 전역이 아닌 이상) 한 job의
/// 종료가 다른 job의 취소 문맥을 치워선 안 된다 — 끝난 job만 제거하고, 취소
/// 대상(현재 작업)은 가장 최근에 시작한 실행 중 job 그대로다. 같은 job의 중복
/// 요청은 게이트에서 물러나므로 이 판에 아무것도 올리지 않는다.
@MainActor
public final class EngineJobBoard: Sendable {
  public struct Context: Equatable, Sendable {
    public let jobID: String
    public let jobURL: URL
    public let runningJob: URL
    public let externalJob: URL?

    public init(
      jobID: String, jobURL: URL, runningJob: URL, externalJob: URL?
    ) {
      self.jobID = jobID
      self.jobURL = jobURL
      self.runningJob = runningJob
      self.externalJob = externalJob
    }
  }

  private var contexts: [String: Context] = [:]
  private var order: [String] = []

  public init() {}

  /// 취소 대상 — 가장 최근에 시작한 실행 중 job.
  public var current: Context? {
    order.last.flatMap { contexts[$0] }
  }

  public func begin(key: String, context: Context) {
    order.removeAll { $0 == key }
    order.append(key)
    contexts[key] = context
  }

  public func end(key: String) {
    order.removeAll { $0 == key }
    contexts.removeValue(forKey: key)
  }
}
