import Foundation

// ─────────────────────────────────────────────────────────────
//  호스트 동작 우편함 — ExtensionContext.open의 URL이 사라지는 결함 대응
//
//  확장의 extensionContext.open(url)은 컨테이너 앱을 깨우는 신호로는
//  확실하지만 URL 자체가 호스트에 도착한다는 보장이 없다 — 찬·따뜻한
//  시작 모두에서 동작이 사라지고 권한 선택기가 뜨지 않는 결함이 실측됐다.
//  권위를 URL에서 우편함으로 옮긴다:
//
//  - 확장은 열기 **전에** 동작을 App Group 우편함에 남긴다(enqueue).
//  - 호스트는 시작·URL 깨움마다 우편함을 소비한다(drain). URL은 깨우는
//    신호일 뿐 권위가 아니다.
//  - 소비는 배타적이다 — 한 번 닫은 요청(id)은 다시 배달되지 않는다.
// ─────────────────────────────────────────────────────────────

/// 확장이 호스트에 맡긴 동작 하나 — id는 요청마다 새로 뽑는 UUID다.
/// 같은 URL도 요청마다 새 id로 다시 남는다(재시도는 중복이 아니다).
public struct HostActionEnvelope: Codable, Equatable, Sendable {
  public let id: String
  public let requestedAt: String
  public let url: String

  public init(id: String, requestedAt: String, url: String) {
    self.id = id
    self.requestedAt = requestedAt
    self.url = url
  }

  enum CodingKeys: String, CodingKey {
    case id
    case requestedAt = "requested_at"
    case url
  }
}

/// 우편함 항목이 닫힌 이유 — 배달·낡음·깨짐·이미 닫힘.
public enum HostActionCloseReason: String, Codable, Sendable {
  case delivered
  case stale
  case malformed
  case duplicate
}

/// 배달되지 않고 닫힌 항목 — 폐기도 조용히 사라지지 않고 결과에 남는다.
public struct HostActionDiscard: Equatable, Sendable {
  public let id: String
  public let reason: HostActionCloseReason

  public init(id: String, reason: HostActionCloseReason) {
    self.id = id
    self.reason = reason
  }
}

/// 소비 한 번의 결과 — 배달된 동작과 폐기된 항목이 함께 돌아온다.
public struct HostActionDrain: Equatable, Sendable {
  public let delivered: [HostActionEnvelope]
  public let discarded: [HostActionDiscard]

  public init(delivered: [HostActionEnvelope], discarded: [HostActionDiscard]) {
    self.delivered = delivered
    self.discarded = discarded
  }
}

/// 닫은 내역의 한 줄 — `consumed.jsonl`에 append-only로 쌓인다. 같은 id가
/// 다시 배달되지 않는다는 증거이자, 배달하지 않은 이유의 기록이다.
struct HostActionLedgerLine: Codable, Equatable, Sendable {
  let id: String
  let url: String?
  let reason: HostActionCloseReason
  let closedAt: String

  enum CodingKeys: String, CodingKey {
    case id
    case url
    case reason
    case closedAt = "closed_at"
  }
}

public enum HostActionMailboxError: Error, Equatable, LocalizedError {
  case unsafeActionID(String)

  public var errorDescription: String? {
    switch self {
    case .unsafeActionID(let id):
      "동작 요청 ID가 안전하지 않습니다: \(id)"
    }
  }
}

public enum HostActionMailbox {
  public static let directoryName = "HostActionMailbox"
  public static let pendingDirectoryName = "pending"
  public static let ledgerFileName = "consumed.jsonl"
  /// 열리지 못한 요청을 닫는 나이(초). 찬 시작에서 URL이 사라져도 배달은
  /// 시작 직후 일어나므로 실배달은 초 단위다 — 이 한도는 재시동 뒤 등
  /// 뒤늦게 살아난 옛 요청이 새 세션을 건드리지 않게 하는 안전선이다.
  public static let maxAgeSeconds: TimeInterval = 900

  // ─── 넣기 — 확장이 extensionContext.open 앞에서 부른다 ──────────

  /// 동작을 우편함에 남긴다 — 대기 파일 이름의 13자리 고정폭 밀리초 접두사가
  /// 곧 대기 순서다. URL은 호스트 동작이어야 한다(넣는 단계에서 계약을 검사한다).
  @discardableResult
  public static func enqueue(
    url: URL,
    workspace: AppGroupWorkspace,
    id: String = UUID().uuidString,
    now: Date = Date(),
    fileManager: FileManager = .default
  ) throws -> HostActionEnvelope {
    guard AppGroupWorkspace.isSafeJobID(id) else {
      throw HostActionMailboxError.unsafeActionID(id)
    }
    _ = try AgentURL.request(url)
    let envelope = HostActionEnvelope(
      id: id,
      requestedAt: timestamp(now),
      url: url.absoluteString)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(envelope)
    let directory = pendingDirectory(workspace: workspace, fileManager: fileManager)
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
    let millis = Int64((now.timeIntervalSince1970 * 1000).rounded())
    try data.write(
      to: directory
        .appendingPathComponent(String(format: "%013ld-%@", millis, id))
        .appendingPathExtension("json"),
      options: .atomic)
    return envelope
  }

  // ─── 소비 — 호스트가 시작·URL 깨움마다 부른다 ────────────────────

  /// 대기 항목을 배타적으로 꺼낸다. 항목마다 **먼저** 원장에 닫음을 남기고
  /// 파일을 지운다 — 그 순서면 어느 지점에서 죽어도 같은 요청이 두 번
  /// 배달되지 않는다(지우기 전에 죽으면 다음 소비가 이미 닫힌 id로 폐기한다).
  /// 원장을 못 읽으면 소비 자체를 하지 않는다(모르는 채 배달하면 중복이다).
  public static func drain(
    workspace: AppGroupWorkspace,
    fileManager: FileManager = .default,
    now: Date = Date()
  ) throws -> HostActionDrain {
    let pending = pendingDirectory(workspace: workspace, fileManager: fileManager)
    guard fileManager.fileExists(atPath: pending.path) else {
      return HostActionDrain(delivered: [], discarded: [])
    }
    let closed = try closedIDs(workspace: workspace, fileManager: fileManager)
    var delivered: [HostActionEnvelope] = []
    var discarded: [HostActionDiscard] = []
    let files = try fileManager
      .contentsOfDirectory(at: pending, includingPropertiesForKeys: nil)
      .filter { $0.pathExtension == "json" }
      .sorted { $0.lastPathComponent < $1.lastPathComponent }
    for file in files {
      let stem = file.deletingPathExtension().lastPathComponent
      guard let data = try? Data(contentsOf: file),
        let envelope = try? JSONDecoder().decode(HostActionEnvelope.self, from: data),
        valid(envelope)
      else {
        // 깨진 항목도 조용히 사라지지 않는다 — 파일 이름을 신원 삼아 폐기로
        // 남긴다. 소비가 멈추지 않게 하는 것이 우선이다.
        try appendLedgerLine(
          HostActionLedgerLine(
            id: stem, url: nil, reason: .malformed,
            closedAt: timestamp(now)),
          workspace: workspace, fileManager: fileManager)
        try fileManager.removeItem(at: file)
        discarded.append(HostActionDiscard(id: stem, reason: .malformed))
        continue
      }
      if closed.contains(envelope.id) {
        // 원장에 이미 닫혔다 — 배달하지 않고 지운다(원장이 그 기록이다).
        try fileManager.removeItem(at: file)
        discarded.append(HostActionDiscard(id: envelope.id, reason: .duplicate))
        continue
      }
      if let requestedAt = parseTimestamp(envelope.requestedAt),
        now.timeIntervalSince(requestedAt) > maxAgeSeconds
      {
        try close(envelope, in: file, reason: .stale, at: now, workspace: workspace,
          fileManager: fileManager)
        discarded.append(HostActionDiscard(id: envelope.id, reason: .stale))
        continue
      }
      try close(envelope, in: file, reason: .delivered, at: now, workspace: workspace,
        fileManager: fileManager)
      delivered.append(envelope)
    }
    return HostActionDrain(delivered: delivered, discarded: discarded)
  }

  // ─── 내부 — 시각·자리·원장·검증 ─────────────────────────────────

  /// ISO8601DateFormatter는 Sendable이 아니어서 공유 정적값으로 쓸 수
  /// 없다(Swift 6 격리). 항목 수는 손가락에 꼽히므로 자리에서 만들어 쓴다.
  private static func timestamp(_ date: Date) -> String {
    ISO8601DateFormatter().string(from: date)
  }

  private static func parseTimestamp(_ text: String) -> Date? {
    ISO8601DateFormatter().date(from: text)
  }

  private static func pendingDirectory(
    workspace: AppGroupWorkspace, fileManager: FileManager
  ) -> URL {
    workspace.rootURL
      .appendingPathComponent(directoryName, isDirectory: true)
      .appendingPathComponent(pendingDirectoryName, isDirectory: true)
  }

  private static func ledgerFile(
    workspace: AppGroupWorkspace, fileManager: FileManager
  ) -> URL {
    workspace.rootURL
      .appendingPathComponent(directoryName, isDirectory: true)
      .appendingPathComponent(ledgerFileName)
  }

  /// 항목을 닫는다 — 원장 줄이 먼저, 대기 파일 삭제가 나중.
  private static func close(
    _ envelope: HostActionEnvelope,
    in file: URL,
    reason: HostActionCloseReason,
    at now: Date,
    workspace: AppGroupWorkspace,
    fileManager: FileManager
  ) throws {
    try appendLedgerLine(
      HostActionLedgerLine(
        id: envelope.id, url: envelope.url, reason: reason,
        closedAt: timestamp(now)),
      workspace: workspace, fileManager: fileManager)
    try fileManager.removeItem(at: file)
  }

  /// 원장에 이미 닫힌 id 전체 — 한 줄이라도 해석에 실패하면 읽기 자체가
  /// 실패다. 실패를 삼키면 모르는 채 배달하게 되므로 소비를 막는다.
  private static func closedIDs(
    workspace: AppGroupWorkspace, fileManager: FileManager
  ) throws -> Set<String> {
    let path = ledgerFile(workspace: workspace, fileManager: fileManager)
    guard let text = try? String(contentsOf: path, encoding: .utf8), !text.isEmpty else {
      return []
    }
    var ids: Set<String> = []
    for line in text.split(separator: "\n") {
      guard let data = String(line).data(using: .utf8),
        let record = try? JSONDecoder().decode(HostActionLedgerLine.self, from: data)
      else { throw CocoaError(.fileReadCorruptFile) }
      ids.insert(record.id)
    }
    return ids
  }

  private static func appendLedgerLine(
    _ line: HostActionLedgerLine,
    workspace: AppGroupWorkspace,
    fileManager: FileManager
  ) throws {
    let data = try JSONEncoder().encode(line)
    var newline = data
    newline.append(0x0A)
    let path = ledgerFile(workspace: workspace, fileManager: fileManager)
    try? fileManager.createDirectory(
      at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
    if !fileManager.fileExists(atPath: path.path) {
      try newline.write(to: path, options: [.atomic])
      return
    }
    let handle = try FileHandle(forWritingTo: path)
    defer { try? handle.close() }
    _ = try handle.seekToEnd()
    try handle.write(contentsOf: newline)
  }

  /// 항목 본문이 계약과 같은가 — id·시각·URL이 모두 읽을 수 있고 URL이
  /// 호스트 동작으로 해석되어야 배달한다.
  private static func valid(_ envelope: HostActionEnvelope) -> Bool {
    guard !envelope.id.isEmpty,
      parseTimestamp(envelope.requestedAt) != nil,
      let url = URL(string: envelope.url)
    else { return false }
    do {
      _ = try AgentURL.request(url)
      return true
    } catch {
      return false
    }
  }
}
