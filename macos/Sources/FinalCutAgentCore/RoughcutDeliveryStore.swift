import CryptoKit
import Foundation

// ─────────────────────────────────────────────────────────────
//  STAB-S2C — 러프컷 배달 수명주기와 원본 매핑
//
//  엔진은 러프컷 XML을 쓰는 순간 `roughcut_generated`로 멈춘다(가져오기는
//  엔진의 일이 아니다). 이 파일은 그 뒤의 사실을 기록으로 남긴다:
//
//  - `RoughcutDeliveryStore` — job 안의 배달 기록. 생성·요청·전송·실패·확인이
//    줄로 쌓이고 마지막 줄이 현재 상태다. 전송 성공은 확인이 아니다.
//  - `OriginMapStore` — 확정 시점에 관찰된 결과 프로젝트 UID를 원본 러프컷
//    job/리비전에 잇는 App Group 공유 지도(S4B의 조회처).
// ─────────────────────────────────────────────────────────────

/// 러프컷 배달의 상태 — 전송 성공(sending)과 확인(confirmed)은 다른 사실이다.
public enum RoughcutDeliveryState: String, Codable, Sendable {
  case generated
  case importRequested = "import_requested"
  case importSent = "import_sent"
  case importFailed = "import_failed"
  case importConfirmed = "import_confirmed"
}

/// job 루트 `roughcut-delivery.jsonl`의 한 줄 — append-only, 마지막 줄이 현재.
public struct RoughcutDeliveryRecord: Codable, Equatable, Sendable {
  public let jobID: String
  public let inputRevision: String
  public let outputSHA256: String
  public let state: RoughcutDeliveryState
  /// 확인에서만 실린다 — 가져오기 뒤 Final Cut이 관찰한 결과 프로젝트의 UID.
  public let projectUID: String?
  public let recordedAt: String
  public let error: String?

  public init(
    jobID: String,
    inputRevision: String,
    outputSHA256: String,
    state: RoughcutDeliveryState,
    projectUID: String? = nil,
    recordedAt: String,
    error: String? = nil
  ) {
    self.jobID = jobID
    self.inputRevision = inputRevision
    self.outputSHA256 = outputSHA256
    self.state = state
    self.projectUID = projectUID
    self.recordedAt = recordedAt
    self.error = error
  }

  enum CodingKeys: String, CodingKey {
    case jobID = "job_id"
    case inputRevision = "input_revision"
    case outputSHA256 = "output_sha256"
    case state
    case projectUID = "project_uid"
    case recordedAt = "recorded_at"
    case error
  }
}

public enum RoughcutDeliveryStore {
  public static let fileName = "roughcut-delivery.jsonl"

  /// 이 job의 현재 배달 상태 — 마지막 줄. 깨진 줄이 하나라도 있으면 nil이다.
  public static func current(jobURL: URL, fileManager: FileManager = .default) ->
    RoughcutDeliveryRecord?
  {
    let path = jobURL.appendingPathComponent(fileName)
    guard let text = try? String(contentsOf: path, encoding: .utf8), !text.isEmpty else {
      return nil
    }
    let decoder = JSONDecoder()
    var last: RoughcutDeliveryRecord?
    for line in text.split(separator: "\n") {
      guard let data = String(line).data(using: .utf8),
        let record = try? decoder.decode(RoughcutDeliveryRecord.self, from: data)
      else { return nil }
      last = record
    }
    return last
  }

  /// 한 줄을 배타로 더한다 — 이미 같은 상태 줄이 마지막이면 멱등으로 넘어간다.
  public static func append(
    _ record: RoughcutDeliveryRecord,
    to jobURL: URL,
    fileManager: FileManager = .default
  ) throws {
    if let last = current(jobURL: jobURL, fileManager: fileManager),
      last.state == record.state, last.outputSHA256 == record.outputSHA256
    {
      return
    }
    let encoder = JSONEncoder()
    let data = try encoder.encode(record)
    var line = data
    line.append(0x0A)
    let path = jobURL.appendingPathComponent(fileName)
    if !fileManager.fileExists(atPath: path.path) {
      try line.write(to: path, options: [.atomic])
      return
    }
    let handle = try FileHandle(forWritingTo: path)
    defer { try? handle.close() }
    _ = try handle.seekToEnd()
    try handle.write(contentsOf: line)
  }

  /// 결과를 다시 보내도 되는가 — 마지막 기록이 요청했던·보낸·실패한 상태여야 하고
  /// 출력 해시가 지금 파일과 같아야 한다(S2C: 바뀐 출력의 자동 재전송 금지).
  public static func retryable(
    jobURL: URL, outputSHA256: String, fileManager: FileManager = .default
  ) -> Bool {
    guard let last = current(jobURL: jobURL, fileManager: fileManager) else { return false }
    guard last.outputSHA256 == outputSHA256 else { return false }
    switch last.state {
    case .generated, .importRequested, .importSent, .importFailed: return true
    case .importConfirmed: return false
    }
  }
}

/// 결과 프로젝트 UID → 원본 러프컷 job/리비전의 한 줄.
public struct OriginMapping: Codable, Equatable, Sendable {
  public let projectUID: String
  public let originJobID: String
  public let originRevision: String
  public let recordedAt: String

  public init(projectUID: String, originJobID: String, originRevision: String, recordedAt: String) {
    self.projectUID = projectUID
    self.originJobID = originJobID
    self.originRevision = originRevision
    self.recordedAt = recordedAt
  }

  enum CodingKeys: String, CodingKey {
    case projectUID = "project_uid"
    case originJobID = "origin_job_id"
    case originRevision = "origin_revision"
    case recordedAt = "recorded_at"
  }
}

public enum OriginMapError: Error, Equatable, LocalizedError {
  case ambiguous(projectUID: String)

  public var errorDescription: String? {
    switch self {
    case .ambiguous(let uid):
      "프로젝트 \(uid)가 여러 러프컷 job과 연결돼 있다 — 어느 쪽의 자막인지 말할 수 없다"
    }
  }
}

/// App Group 뿌리의 `origin-map.jsonl` — 여러 job·재시동을 건너 살아 있는 지도.
public struct OriginMapStore {
  public static let fileName = "origin-map.jsonl"
  public let fileURL: URL

  public init(workspace: AppGroupWorkspace) {
    self.fileURL = workspace.rootURL.appendingPathComponent(Self.fileName)
  }

  public init(fileURL: URL) {
    self.fileURL = fileURL
  }

  /// 지도의 모든 줄 — 해석에 실패하면 그 자체가 오류다(조용한 무시 금지).
  public func entries() throws -> [OriginMapping] {
    guard let text = try? String(contentsOf: fileURL, encoding: .utf8), !text.isEmpty else {
      return []
    }
    let decoder = JSONDecoder()
    var mappings: [OriginMapping] = []
    for line in text.split(separator: "\n") {
      guard let data = String(line).data(using: .utf8),
        let mapping = try? decoder.decode(OriginMapping.self, from: data)
      else { throw CocoaError(.fileReadCorruptFile) }
      mappings.append(mapping)
    }
    return mappings
  }

  /// UID의 연결 — 마지막 확정이 정본이다. 같은 UID가 서로 다른 원본을 가리키면
  /// 모호해서 실패한다(마지막 것으로 조용히 덮지 않는다).
  public func lookup(projectUID: String) throws -> OriginMapping? {
    let matches = try entries().filter { $0.projectUID == projectUID }
    guard let latest = matches.last else { return nil }
    if matches.contains(where: { $0.originJobID != latest.originJobID }) {
      throw OriginMapError.ambiguous(projectUID: projectUID)
    }
    return latest
  }

  public func record(_ mapping: OriginMapping) throws {
    let encoder = JSONEncoder()
    var data = try encoder.encode(mapping)
    data.append(0x0A)
    if !FileManager.default.fileExists(atPath: fileURL.path) {
      try data.write(to: fileURL, options: [.atomic])
      return
    }
    let handle = try FileHandle(forWritingTo: fileURL)
    defer { try? handle.close() }
    _ = try handle.seekToEnd()
    try handle.write(contentsOf: data)
  }
}

/// 배달 한 번의 경계 — 요청 기록 → 전송 → 결과 기록을 한 문으로(S2C).
///
/// 전송 성공은 `import_sent`일 뿐 확인이 아니다. 해시는 요청 직전의 파일에서
/// 직접 잰다 — 마지막 기록의 해시와 다르면 바뀐 출력이므로 보내지 않는다.
public struct RoughcutImportSession {
  public let jobID: String
  public let jobURL: URL
  public let now: @Sendable () -> String

  public init(jobID: String, jobURL: URL, now: @escaping @Sendable () -> String = {
    ISO8601DateFormatter().string(from: Date())
  }) {
    self.jobID = jobID
    self.jobURL = jobURL
    self.now = now
  }

  /// 러프컷이 생성됐다는 첫 기록 — 엔진 종료 0 직후, 가져오기 전에 남긴다.
  public func recordGenerated(inputRevision: String) throws {
    let output = jobURL.appendingPathComponent(RoundTripJob.roughcutFileName)
    let hash = try Self.sha256Hex(of: output)
    try RoughcutDeliveryStore.append(
      RoughcutDeliveryRecord(
        jobID: jobID, inputRevision: inputRevision, outputSHA256: hash,
        state: .generated, recordedAt: now()),
      to: jobURL)
  }

  /// 이미 생성된 출력을 Final Cut으로 보낸다 — 요청·전송·실패를 기록과 함께.
  @discardableResult
  public func send(
    inputRevision: String,
    sender: @Sendable (URL) async throws -> Void
  ) async throws -> Bool {
    let output = jobURL.appendingPathComponent(RoundTripJob.roughcutFileName)
    guard FileManager.default.fileExists(atPath: output.path) else { return false }
    let hash = try Self.sha256Hex(of: output)
    guard
      RoughcutDeliveryStore.retryable(jobURL: jobURL, outputSHA256: hash)
    else { return false }
    func record(_ state: RoughcutDeliveryState, _ error: String? = nil) throws {
      try RoughcutDeliveryStore.append(
        RoughcutDeliveryRecord(
          jobID: jobID, inputRevision: inputRevision, outputSHA256: hash,
          state: state, recordedAt: now(), error: error),
        to: jobURL)
    }
    try record(.importRequested)
    do {
      try await sender(output)
    } catch {
      // 실패도 기록으로 남긴다 — 단 원인 예외는 그대로 올려 사용자에게 보인다.
      try? record(.importFailed, String(describing: error))
      throw error
    }
    try record(.importSent)
    return true
  }

  static func sha256Hex(of url: URL) throws -> String {
    let data = try Data(contentsOf: url)
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }
}

/// 가져오기 확정의 자격과 기록(S2C) — 전송 성공은 확인이 아니다.
///
/// 확정은 **관찰된 결과 프로젝트 신원**으로만 한다: 활성 프로젝트 UID가 있고
/// 그것이 이 job의 원본(입력) 프로젝트가 아니면, 사용자가 방금 가져온 러프컷
/// 결과를 열고 있다는 뜻이다. 확정과 함께 원본 지도(UID → job/리비전)를
/// App Group에 남긴다 — 자막 공유 받기(S4B)의 조회처다.
public enum RoughcutConfirmation {
  /// 확정 자격 — 활성 프로젝트가 관찰되고 원본 프로젝트와 다른 UID여야 한다.
  public static func eligible(activeProjectUID: String?, sourceProjectUID: String?) -> Bool {
    guard
      let active = activeProjectUID, !active.isEmpty,
      let source = sourceProjectUID, !source.isEmpty
    else { return false }
    return active != source
  }

  /// 확정 기록 + 원본 지도 등록 — 지도에 모호함이 있으면 확정도 실패한다.
  public static func confirm(
    jobID: String,
    jobURL: URL,
    activeProjectUID: String,
    originMap: OriginMapStore,
    now: String = ISO8601DateFormatter().string(from: Date())
  ) throws {
    guard let last = RoughcutDeliveryStore.current(jobURL: jobURL) else {
      throw CocoaError(.fileNoSuchFile)
    }
    let record = RoughcutDeliveryRecord(
      jobID: jobID,
      inputRevision: last.inputRevision,
      outputSHA256: last.outputSHA256,
      state: .importConfirmed,
      projectUID: activeProjectUID,
      recordedAt: now)
    try RoughcutDeliveryStore.append(record, to: jobURL)
    try originMap.record(
      OriginMapping(
        projectUID: activeProjectUID,
        originJobID: jobID,
        originRevision: last.inputRevision,
        recordedAt: now))
  }
}
