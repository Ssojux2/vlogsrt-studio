import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  호스트 동작 우편함 — ExtensionContext.open의 URL이 사라져도
//  동작이 정확히 한 번 배달되는지
// ─────────────────────────────────────────────────────────────

private func _workspace() throws -> AppGroupWorkspace {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  return AppGroupWorkspace(rootURL: root)
}

private func _pendingDirectory(of workspace: AppGroupWorkspace) -> URL {
  workspace.rootURL
    .appendingPathComponent(HostActionMailbox.directoryName, isDirectory: true)
    .appendingPathComponent(HostActionMailbox.pendingDirectoryName, isDirectory: true)
}

private func _pendingFileNames(_ workspace: AppGroupWorkspace) -> [String] {
  (try? FileManager.default.contentsOfDirectory(atPath: _pendingDirectory(of: workspace).path))
    ?? []
}

private let _base = Date(timeIntervalSince1970: 1_800_000_000)

// ─── 배달 — 찬·따뜻한 시작 모두 정확히 한 번 ──────────────────────

@Test func coldLaunchDeliversEnqueuedActionExactlyOnce() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  try HostActionMailbox.enqueue(
    url: AgentURL.libraryGrantURL(), workspace: workspace, id: "grant-1", now: _base)

  // 찬 시작 — URL이 사라져 앱만 띄워도 시작 소비가 배달한다.
  let first = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(10))
  #expect(first.delivered.map(\.id) == ["grant-1"])
  #expect(first.discarded.isEmpty)

  // 늦게 도착한 깨움(URL 재배달)은 같은 요청을 다시 배달하지 않는다.
  let second = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(11))
  #expect(second.delivered.isEmpty)
  #expect(second.discarded.isEmpty)
  #expect(_pendingFileNames(workspace).isEmpty)
}

@Test func drainDeliversEveryRequestOnceInRequestOrder() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "action-a",
    now: _base)
  try HostActionMailbox.enqueue(
    url: AgentURL.roughcutURL(jobID: "job-1"), workspace: workspace, id: "action-b",
    now: _base.addingTimeInterval(1))
  // 같은 URL의 재요청 — 요청마다 새 id이므로 중복이 아니다.
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "action-c",
    now: _base.addingTimeInterval(2))

  let drain = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(3))
  #expect(drain.delivered.map(\.id) == ["action-a", "action-b", "action-c"])
  #expect(drain.discarded.isEmpty)

  // 재시도 — 같은 URL, 새 요청: 다시 배달된다.
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "action-d",
    now: _base.addingTimeInterval(4))
  let retry = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(5))
  #expect(retry.delivered.map(\.id) == ["action-d"])
}

@Test func emptyMailboxDrainsToNothing() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  let drain = try HostActionMailbox.drain(workspace: workspace, now: _base)
  #expect(drain == HostActionDrain(delivered: [], discarded: []))
}

// ─── 길 보존 — 권한·엔진·자막 동작이 그대로 기존 해석으로 간다 ────

@Test func libraryGrantActionSurvivesTheMailboxRoundTrip() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  let url = try AgentURL.libraryGrantURL()
  try HostActionMailbox.enqueue(
    url: url, workspace: workspace, id: "grant-2", now: _base)

  let drain = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(1))
  #expect(drain.delivered.count == 1)
  let deliveredURL = try #require(URL(string: drain.delivered[0].url))
  #expect(deliveredURL == url)
  #expect(try AgentURL.request(deliveredURL) == .libraryGrant)
}

@Test func engineAndCaptionActionRoutesArePreserved() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  let routes: [(URL, AgentRequest)] = try [
    (AgentURL.analyzeURL(jobID: "job-1"), .analyze("job-1")),
    (AgentURL.roughcutURL(jobID: "job-1"), .roughcut("job-1")),
    (AgentURL.importURL(jobID: "job-1"), .importJob("job-1")),
    (AgentURL.captionEstimateURL(jobID: "caption-1"), .captionEstimate("caption-1")),
    (AgentURL.captionRunURL(jobID: "caption-1"), .captionRun("caption-1")),
    (AgentURL.captionDeliverURL(jobID: "caption-1"), .captionDeliver("caption-1")),
  ]
  for (index, (url, _)) in routes.enumerated() {
    try HostActionMailbox.enqueue(
      url: url, workspace: workspace, id: "route-\(index)",
      now: _base.addingTimeInterval(Double(index)))
  }

  let drain = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(60))
  #expect(drain.delivered.count == routes.count)
  for (envelope, (_, expected)) in zip(drain.delivered, routes) {
    let url = try #require(URL(string: envelope.url))
    #expect(try AgentURL.request(url) == expected)
  }
}

// ─── 안전한 닫기 — 낡음·깨짐·중복이 소비를 멈추지 않는다 ──────────

@Test func staleActionIsClosedSafelyWithoutDelivery() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "stale-1",
    now: Date(timeIntervalSince1970: 0))

  let now = Date(timeIntervalSince1970: HostActionMailbox.maxAgeSeconds + 1)
  let drain = try HostActionMailbox.drain(workspace: workspace, now: now)
  #expect(drain.delivered.isEmpty)
  #expect(drain.discarded == [HostActionDiscard(id: "stale-1", reason: .stale)])
  #expect(_pendingFileNames(workspace).isEmpty)

  // 닫힌 항목은 다시 나오지 않는다.
  let again = try HostActionMailbox.drain(workspace: workspace, now: now)
  #expect(again.delivered.isEmpty)
  #expect(again.discarded.isEmpty)
}

@Test func malformedPendingActionIsClosedSafely() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  let pending = _pendingDirectory(of: workspace)
  try FileManager.default.createDirectory(at: pending, withIntermediateDirectories: true)
  // 깨진 JSON.
  try Data("not json".utf8).write(
    to: pending.appendingPathComponent("0000000000001-broken.json"))
  // 호스트 동작이 아닌 URL — 계약 밖이라 배달할 수 없다.
  let foreign = HostActionEnvelope(
    id: "foreign-1", requestedAt: "2026-10-01T00:00:00Z", url: "https://example.com/x")
  try JSONEncoder().encode(foreign).write(
    to: pending.appendingPathComponent("0000000000002-foreign-1.json"))
  // 시각을 읽을 수 없는 항목 — 낡음을 잴 수 없으면 배달하지 않는다.
  let undated = HostActionEnvelope(
    id: "undated-1", requestedAt: "not-a-date",
    url: try AgentURL.analyzeURL(jobID: "job-1").absoluteString)
  try JSONEncoder().encode(undated).write(
    to: pending.appendingPathComponent("0000000000003-undated-1.json"))
  // 정상 항목도 함께 — 폐기가 소비를 멈추지 않는다.
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-2"), workspace: workspace, id: "healthy-1",
    now: _base)

  let drain = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(1))
  #expect(drain.delivered.map(\.id) == ["healthy-1"])
  #expect(
    drain.discarded.map(\.id)
      == ["0000000000001-broken", "0000000000002-foreign-1", "0000000000003-undated-1"])
  #expect(drain.discarded.allSatisfy { $0.reason == .malformed })
  #expect(_pendingFileNames(workspace).isEmpty)
}

@Test func pendingFileSurvivingTheConsumeCrashWindowIsClosedAsDuplicate() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "crash-1",
    now: _base)
  let first = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(1))
  #expect(first.delivered.map(\.id) == ["crash-1"])

  // 원장 줄은 남았는데 대기 파일이 살아남은 모양(쓰기와 삭제 사이에 죽은
  // 창) — 같은 id의 파일이 다시 나타나도 두 번 배달하지 않는다.
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "crash-1",
    now: _base.addingTimeInterval(2))
  let second = try HostActionMailbox.drain(
    workspace: workspace, now: _base.addingTimeInterval(3))
  #expect(second.delivered.isEmpty)
  #expect(second.discarded == [HostActionDiscard(id: "crash-1", reason: .duplicate)])
  #expect(_pendingFileNames(workspace).isEmpty)
}

// ─── 넣기·원장의 실패 닫힘 ───────────────────────────────────────

@Test func enqueueRejectsURLsOutsideTheAgentContract() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  #expect(throws: AgentURLError.self) {
    try HostActionMailbox.enqueue(
      url: URL(string: "https://example.com")!, workspace: workspace)
  }
  #expect(_pendingFileNames(workspace).isEmpty)
}

@Test func unreadableLedgerBlocksDeliveryInsteadOfDuplicating() throws {
  let workspace = try _workspace()
  defer { try? FileManager.default.removeItem(at: workspace.rootURL) }
  try HostActionMailbox.enqueue(
    url: AgentURL.analyzeURL(jobID: "job-1"), workspace: workspace, id: "blocked-1",
    now: _base)
  // 원장이 깨졌다 — 닫힌 내역을 모르는 채 배달하면 중복 배달이 될 수 있으므로
  // 소비 자체가 실패로 닫힌다(대기 파일은 그대로 남는다).
  let ledger = workspace.rootURL
    .appendingPathComponent(HostActionMailbox.directoryName, isDirectory: true)
    .appendingPathComponent(HostActionMailbox.ledgerFileName)
  try Data("corrupt".utf8).write(to: ledger)

  #expect(throws: CocoaError.self) {
    _ = try HostActionMailbox.drain(workspace: workspace, now: _base.addingTimeInterval(1))
  }
  #expect(_pendingFileNames(workspace).count == 1)
}
