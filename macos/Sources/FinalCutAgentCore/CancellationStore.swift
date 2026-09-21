import CryptoKit
import Foundation

/// 앱·엔진 사이의 취소 계약 — 한 요청이 두 곳에 같은 바이트로 기록된다.
///
/// 엔진(Python)은 API·전사·ffmpeg 단계 사이에 `cancellation.jsonl`을 확인하고
/// 같은 시도의 요청을 만나면 남은 호출을 하지 않는다. App Group 작업 폴더와
/// 외부 작업 폴더 양쪽에 남기는 이유는 개발 실행기(NSUserUnixTask)가 App Group
/// 밖에서만 돌 수 있기 때문이다 — 어느 쪽에서 돌아도 같은 취소를 본다.
public struct CancellationStore: Sendable {
  public static let fileName = "cancellation.jsonl"

  public let appGroupJob: URL
  public let externalJob: URL?

  public init(appGroupJob: URL, externalJob: URL?) {
    self.appGroupJob = appGroupJob
    self.externalJob = externalJob
  }

  /// 취소를 기록한다 — 이미 같은 리비전의 기록이 있으면 덮어쓰지 않는다.
  public func record(
    _ request: CancellationRequestMessage,
    fileManager: FileManager = .default
  ) throws {
    let line = try ProtocolCodec.encode(
      request,
      type: "CancellationRequest",
      version: ProtocolCodec.analysisVersion
    )
    for base in targets(fileManager: fileManager) {
      let destination = base.appendingPathComponent(Self.fileName)
      try Data(line.utf8).write(to: destination, options: [.atomic])
    }
  }

  /// 외부 작업 폴더의 취소를 App Group 쪽으로 가져온다(동기화 시점용).
  public func pullExternal(fileManager: FileManager = .default) throws {
    guard let externalJob else { return }
    let source = externalJob.appendingPathComponent(Self.fileName)
    let destination = appGroupJob.appendingPathComponent(Self.fileName)
    guard fileManager.fileExists(atPath: source.path),
      !fileManager.fileExists(atPath: destination.path)
    else { return }
    try fileManager.copyItem(at: source, to: destination)
  }

  private func targets(fileManager: FileManager) -> [URL] {
    var urls = [appGroupJob]
    if let externalJob, !isSameDirectory(externalJob, appGroupJob, fileManager: fileManager) {
      urls.append(externalJob)
    }
    return urls
  }

  private func isSameDirectory(
    _ left: URL, _ right: URL, fileManager: FileManager
  ) -> Bool {
    left.standardizedFileURL.path == right.standardizedFileURL.path
  }
}

public extension CancellationStore {
  /// 이 작업이 취소됐는지 — 새 엔진 호출·결과 동기화·import 앞에서 확인한다.
  /// 캐시는 보존한다: 취소는 지금 실행만 멈추는 것이지 기록을 지우지 않는다.
  static func isCancelled(jobURL: URL, fileManager: FileManager = .default) -> Bool {
    fileManager.fileExists(atPath: jobURL.appendingPathComponent(fileName).path)
  }
}

public enum CancellationCodec {
  /// Python `CancellationRequest.create`와 같은 정준 JSON SHA-256 리비전.
  public static func revision(
    inputRevision: String, attemptID: String, requestedAt: String, reason: String
  ) -> String {
    let payload = CancellationDigestPayload(
      attemptID: attemptID,
      inputRevision: inputRevision,
      reason: reason,
      requestedAt: requestedAt
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    guard let data = try? encoder.encode(payload) else {
      preconditionFailure("CancellationDigestPayload은 항상 인코딩된다")
    }
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }

  /// 허용된 취소 사유 — 그 밖의 값은 엔진이 거부한다.
  public static let allowedReasons: Set<String> = ["user", "active_project_changed"]
}

private struct CancellationDigestPayload: Codable {
  let attemptID: String
  let inputRevision: String
  let reason: String
  let requestedAt: String

  enum CodingKeys: String, CodingKey {
    case attemptID = "attempt_id"
    case inputRevision = "input_revision"
    case reason
    case requestedAt = "requested_at"
  }
}
