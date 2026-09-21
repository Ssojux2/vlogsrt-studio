import Foundation

/// 1.1 시대 작업의 읽기 전용 보존 — 마이그레이션은 복사만 하고 원본도 사본도 고치지 않는다.
///
/// 프로토콜이 1.2로 오르며 1.1 작업 기록은 더 이상 해석 대상이 아니다. 지우면
/// 사용자의 승인 이력이 사라지므로 `LegacyJobs/`로 **복사**해 보존하고, 어떤
/// 새 실행도 이 디렉터리를 읽지 않는다(감사·표시만 한다).
public struct LegacyJobStore: Sendable {
  public static let directoryName = "LegacyJobs"

  public let rootURL: URL

  public init(workspace: AppGroupWorkspace) {
    self.rootURL = workspace.rootURL.appendingPathComponent(
      Self.directoryName, isDirectory: true
    )
  }

  /// 남아 있는 1.1 작업의 job ID 목록 — 이미 보존된 것은 다시 세지 않는다.
  ///
  /// 분류는 **파일 존재가 아니라 기록의 protocol_version**으로 한다: estimate
  /// JSONL의 첫 줄 envelope을 실제로 파싱해 `1.1`일 때만 legacy다. 1.2 작업을
  /// 오탐하지 않는다.
  public func pendingLegacyJobIDs(
    in jobsRoot: URL, fileManager: FileManager = .default
  ) throws -> [String] {
    guard let names = try? fileManager.contentsOfDirectory(atPath: jobsRoot.path) else {
      return []
    }
    return try names
      .filter { AppGroupWorkspace.isSafeJobID($0) }
      .filter { name in
        let job = jobsRoot.appendingPathComponent(name, isDirectory: true)
        guard Self.isLegacyProtocolVersion(job, fileManager: fileManager) else { return false }
        return !(try isPreserved(jobID: name, fileManager: fileManager))
      }
      .sorted()
  }

  /// 작업의 estimate 기록이 1.1인가 — envelope을 파싱해 판정한다.
  public static func isLegacyProtocolVersion(
    _ job: URL, fileManager: FileManager = .default
  ) -> Bool {
    let marker = job.appendingPathComponent("analysis-estimate.jsonl")
    guard fileManager.fileExists(atPath: marker.path),
      let line = try? String(contentsOf: marker, encoding: .utf8)
    else { return false }
    // JSONL 첫 줄만 본다 — 분류이지 해석이 아니다.
    guard let firstLine = line.split(separator: "\n").first,
      let data = String(firstLine).data(using: .utf8),
      let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return false }
    return (envelope["protocol_version"] as? String) == "1.1"
  }

  /// 1.1 작업을 LegacyJobs로 복사해 보존한다 — 원본 작업 폴더는 지우지 않는다.
  @discardableResult
  public func preserve(
    jobID: String, from jobsRoot: URL, fileManager: FileManager = .default
  ) throws -> URL {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw WorkspaceError.invalidJobID(jobID)
    }
    let source = jobsRoot.appendingPathComponent(jobID, isDirectory: true)
    let destination = rootURL.appendingPathComponent(jobID, isDirectory: true)
    if fileManager.fileExists(atPath: destination.path) {
      throw LegacyJobError.alreadyPreserved(jobID)
    }
    try fileManager.createDirectory(at: rootURL, withIntermediateDirectories: true)
    try fileManager.copyItem(at: source, to: destination)
    try fileManager.setAttributes([.immutable: 1], ofItemAtPath: destination.path)
    return destination
  }

  public func preservedJobIDs(fileManager: FileManager = .default) -> [String] {
    guard let names = try? fileManager.contentsOfDirectory(atPath: rootURL.path) else {
      return []
    }
    return names.filter { AppGroupWorkspace.isSafeJobID($0) }.sorted()
  }

  private func isPreserved(jobID: String, fileManager: FileManager) throws -> Bool {
    let destination = rootURL.appendingPathComponent(jobID, isDirectory: true)
    return fileManager.fileExists(atPath: destination.path)
  }
}

public enum LegacyJobError: Error, Equatable {
  case alreadyPreserved(String)

  public var localizedDescription: String {
    switch self {
    case .alreadyPreserved(let jobID):
      "이미 보존된 1.1 작업입니다: \(jobID)"
    }
  }
}
