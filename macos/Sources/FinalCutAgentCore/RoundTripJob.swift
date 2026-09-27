import Foundation

/// 왕복 상태 어휘. received/roundtrip_requested는 extension UI가,
/// roundtrip_ready/failed는 Result가 사용한다.
public enum RoundTripStatus: String, Codable, Sendable {
  case received
  case roundtripRequested = "roundtrip_requested"
  case roundtripReady = "roundtrip_ready"
  case failed
}

/// App Group job `<root>/Jobs/<job-id>/`의 왕복 파일 네 결로.
/// 모든 쓰기는 이미 있는 파일을 조용히 덮어쓰지 않는다.
public struct RoundTripJob: Equatable, Sendable {
  public static let inputFileName = "input.fcpxml"
  public static let projectSelectionFileName = "project-selection.jsonl"
  public static let analysisAuthorizationFileName = "analysis-authorization.jsonl"
  public static let candidatesFileName = "candidates.jsonl"
  public static let analysisEstimateFileName = "analysis-estimate.jsonl"
  public static let analysisResultFileName = "analysis-result.jsonl"
  public static let analysisAttemptsFileName = "analysis-attempts.jsonl"
  public static let approvedCandidatesFileName = "approved-candidates.txt"
  public static let requestFileName = "roundtrip-request.jsonl"
  public static let outputFileName = "roundtrip.fcpxml"
  public static let resultFileName = "roundtrip-result.jsonl"
  /// 엔진 진입점이 job 루트에 남기는 러프컷 산출물(S2A-3 관측 계약).
  public static let roughcutFileName = "roughcut.fcpxml"
  /// 시도의 진행 스냅샷 — 파이썬 발행기가 원자적으로 남기는 한 장(S2A-3).
  public static let progressFileName = "analysis-progress.jsonl"

  public let id: String
  public let directoryURL: URL

  public init(workspace: AppGroupWorkspace, id: String) throws {
    guard AppGroupWorkspace.isSafeJobID(id) else {
      throw WorkspaceError.invalidJobID(id)
    }
    self.id = id
    self.directoryURL = workspace.rootURL
      .appendingPathComponent("Jobs", isDirectory: true)
      .appendingPathComponent(id, isDirectory: true)
  }

  public var inputURL: URL { directoryURL.appendingPathComponent(Self.inputFileName) }
  public var projectSelectionURL: URL {
    directoryURL.appendingPathComponent(Self.projectSelectionFileName)
  }
  public var analysisAuthorizationURL: URL {
    directoryURL.appendingPathComponent(Self.analysisAuthorizationFileName)
  }
  public var candidatesURL: URL { directoryURL.appendingPathComponent(Self.candidatesFileName) }
  public var analysisEstimateURL: URL {
    directoryURL.appendingPathComponent(Self.analysisEstimateFileName)
  }
  public var analysisResultURL: URL {
    directoryURL.appendingPathComponent(Self.analysisResultFileName)
  }
  public var approvedCandidatesURL: URL {
    directoryURL.appendingPathComponent(Self.approvedCandidatesFileName)
  }
  public var requestURL: URL { directoryURL.appendingPathComponent(Self.requestFileName) }
  public var outputURL: URL { directoryURL.appendingPathComponent(Self.outputFileName) }
  public var resultURL: URL { directoryURL.appendingPathComponent(Self.resultFileName) }

  public func writeInput(_ data: Data, fileManager: FileManager = .default) throws {
    try Self.writeNew(data, to: inputURL, fileManager: fileManager)
  }

  public func writeProjectSelection(
    _ selection: ActiveProjectSelection,
    fileManager: FileManager = .default
  ) throws {
    let message = try ProjectSelectionMessage.create(selection)
    let line = try ProtocolCodec.encode(
      message,
      type: "ProjectSelection",
      version: ProtocolCodec.analysisVersion
    )
    try Self.writeNew(Data(line.utf8), to: projectSelectionURL, fileManager: fileManager)
  }

  public func writeAnalysisAuthorization(
    _ authorization: AnalysisAuthorizationMessage,
    fileManager: FileManager = .default
  ) throws {
    let line = try ProtocolCodec.encode(
      authorization,
      type: "AnalysisAuthorization",
      version: ProtocolCodec.analysisVersion
    )
    try Self.writeNew(Data(line.utf8), to: analysisAuthorizationURL, fileManager: fileManager)
  }

  public func writeApprovedCandidateIDs(
    _ identifiers: [String],
    fileManager: FileManager = .default
  ) throws {
    let unique = Array(Set(identifiers)).sorted()
    guard unique.allSatisfy(Self.isSHA256) else {
      throw RoundTripJobError.invalidCandidateID
    }
    let text = unique.isEmpty ? "" : unique.joined(separator: "\n") + "\n"
    try Self.writeNew(Data(text.utf8), to: approvedCandidatesURL, fileManager: fileManager)
  }

  public func readProjectSelection() throws -> ProjectSelectionMessage {
    try readOne(
      ProjectSelectionMessage.self,
      type: "ProjectSelection",
      at: projectSelectionURL
    )
  }

  public func readAnalysisEstimate() throws -> AnalysisEstimateMessage {
    try Self.readAnalysisEstimateAt(directoryURL: directoryURL)
  }

  /// 같은 규율을 job 폴더 경로만으로 — 새로고침 관측이 쓰는 자리다(S2A-3).
  public static func readAnalysisEstimateAt(directoryURL: URL) throws -> AnalysisEstimateMessage {
    try Self.decodeSingleLine(
      AnalysisEstimateMessage.self,
      type: "AnalysisEstimate",
      from: try String(
        contentsOf: directoryURL.appendingPathComponent(Self.analysisEstimateFileName),
        encoding: .utf8)
    )
  }

  /// 해석 가능한 추정이 있을 때만 — 파일 존재가 아니라 메시지가 근거다(S2A-3).
  public static func readAnalysisEstimateIfPresent(
    directoryURL: URL, fileManager: FileManager = .default
  ) -> AnalysisEstimateMessage? {
    guard
      fileManager.fileExists(
        atPath: directoryURL.appendingPathComponent(Self.analysisEstimateFileName).path)
    else { return nil }
    return try? Self.readAnalysisEstimateAt(directoryURL: directoryURL)
  }

  /// 마지막 시도의 신원 — 진행 파일이 없는 낡은 job의 baseline 재료(S2A-3).
  /// 한 줄이라도 해석에 실패하면 nil이다.
  public static func readLatestAttemptID(
    directoryURL: URL, fileManager: FileManager = .default
  ) -> String? {
    Self.readAnalysisAttempts(
      at: directoryURL.appendingPathComponent(Self.analysisAttemptsFileName)
    )?.last?.attemptID
  }

  /// job 기록 상태 — 새 작업(fresh)과 모호한 기록(unknown)을 갈라 돌려준다(S1B-R1).
  /// 묶음에는 **읽을 수 있는 현재 추정**이 필수다(없으면 옛 유효 기록으로 넘어가지
  /// 않는다): 입력·추정 리비전이 job의 기록과 같고, 마지막 시도가 집계의 시도와
  /// 신원이 같고, 상태가 집계와 일치해야 한다(success·partial은 succeeded,
  /// error는 failed). 낡은 집계·진행 중·취소 시도·깨진 기록은 전부 unknown이다.
  public func readAnalysisRecord() -> AnalysisRecordState {
    Self.readAnalysisRecord(directoryURL: directoryURL)
  }

  /// 같은 판정을 job 폴더 경로만으로 — 확장의 새로고침 관측이 인스턴스 없이
  /// 쓰는 자리다(S2A-3). 모든 읽기는 이 디렉터리 안에서만 일어난다.
  public static func readAnalysisRecord(
    directoryURL: URL, fileManager: FileManager = .default
  ) -> AnalysisRecordState {
    let attemptsURL = directoryURL.appendingPathComponent(analysisAttemptsFileName)
    let resultURL = directoryURL.appendingPathComponent(analysisResultFileName)
    let candidatesURL = directoryURL.appendingPathComponent(candidatesFileName)
    let estimateURL = directoryURL.appendingPathComponent(analysisEstimateFileName)
    let hasHistory =
      fileManager.fileExists(atPath: resultURL.path)
      || fileManager.fileExists(atPath: attemptsURL.path)
      || fileManager.fileExists(atPath: candidatesURL.path)
    guard hasHistory else { return .fresh }
    guard let result = Self.decodeResultLine(at: resultURL) else { return .unknown }
    guard
      let line = try? String(contentsOf: estimateURL, encoding: .utf8),
      let estimate = try? Self.decodeSingleLine(
        AnalysisEstimateMessage.self, type: "AnalysisEstimate", from: line)
    else { return .unknown }
    guard estimate.inputRevision == result.inputRevision,
      estimate.estimateRevision == result.estimateRevision
    else { return .unknown }
    guard let latest = Self.readAnalysisAttempts(at: attemptsURL)?.last else {
      return .unknown
    }
    guard latest.attemptID == result.attemptID,
      latest.inputRevision == result.inputRevision,
      latest.estimateRevision == result.estimateRevision
    else { return .unknown }
    let requiredStatus = result.status == "error" ? "failed" : "succeeded"
    guard latest.status == requiredStatus else { return .unknown }
    return .valid(result)
  }

  /// 집계 결과 — 지금 job에 묶인 것만 돌아온다. 그 밖은 nil(unknown 취급).
  public func readAnalysisResult() -> AnalysisResultMessage? {
    if case .valid(let result) = readAnalysisRecord() { return result }
    return nil
  }

  /// 사용자에게 보여줄 분석 결과 — 기록이 없거나 지금 job에 묶이지 않으면 unknown이다.
  public func readAnalysisOutcome() -> AnalysisOutcome {
    AnalysisOutcome.make(readAnalysisResult())
  }

  private static func decodeResultLine(at url: URL) -> AnalysisResultMessage? {
    guard let line = try? String(contentsOf: url, encoding: .utf8) else {
      return nil
    }
    var trimmed = line
    if trimmed.hasSuffix("\n") { trimmed.removeLast() }
    guard !trimmed.isEmpty else { return nil }
    return try? ProtocolCodec.decode(
      AnalysisResultMessage.self,
      type: "AnalysisResult",
      version: ProtocolCodec.analysisVersion,
      from: trimmed
    )
  }

  /// 시도 기록 — 한 줄이라도 해석에 실패하면 nil이다. 깨진 원장의 유효한 마지막
  /// 줄로 묶음을 증명하게 두지 않는다.
  private static func readAnalysisAttempts(at url: URL) -> [AnalysisAttemptMessage]? {
    guard let text = try? String(contentsOf: url, encoding: .utf8), !text.isEmpty else {
      return nil
    }
    let lines = text.split(separator: "\n").map(String.init)
    guard !lines.isEmpty else { return nil }
    var attempts: [AnalysisAttemptMessage] = []
    for line in lines {
      guard
        let attempt = try? ProtocolCodec.decodeAnalysis(
          AnalysisAttemptMessage.self,
          type: "AnalysisAttempt",
          from: line
        )
      else { return nil }
      attempts.append(attempt)
    }
    return attempts
  }

  public func readCandidates() throws -> [CutCandidateMessage] {
    let text = try String(contentsOf: candidatesURL, encoding: .utf8)
    return try text.split(separator: "\n").map { line in
      try ProtocolCodec.decodeAnalysis(
        CutCandidateMessage.self,
        type: "CutCandidate",
        from: String(line)
      )
    }
  }

  public func writeRequest(
    _ request: RoundTripRequest,
    fileManager: FileManager = .default
  ) throws {
    let line = try ProtocolCodec.encode(request, type: "RoundTripRequest")
    try Self.writeNew(Data(line.utf8), to: requestURL, fileManager: fileManager)
  }

  public func writeResult(
    _ result: RoundTripResult,
    fileManager: FileManager = .default
  ) throws {
    let line = try ProtocolCodec.encode(result, type: "RoundTripResult")
    try Self.writeNew(Data(line.utf8), to: resultURL, fileManager: fileManager)
  }

  static func writeNew(_ data: Data, to url: URL, fileManager: FileManager) throws {
    guard !fileManager.fileExists(atPath: url.path) else {
      throw RoundTripJobError.destinationExists(url)
    }
    try data.write(to: url, options: .atomic)
  }

  private func readOne<Payload: Decodable>(
    _ payload: Payload.Type,
    type: String,
    at url: URL
  ) throws -> Payload {
    var line = try String(contentsOf: url, encoding: .utf8)
    if line.hasSuffix("\n") { line.removeLast() }
    guard !line.isEmpty, !line.contains("\n") else {
      throw RoundTripJobError.notSingleJSONLine(url)
    }
    return try ProtocolCodec.decodeAnalysis(
      payload,
      type: type,
      from: line
    )
  }

  /// 한 줄짜리 JSONL을 typed payload로 — 정적 관측 경로가 같은 규율을 쓴다(S2A-3).
  private static func decodeSingleLine<Payload: Decodable>(
    _ payload: Payload.Type, type: String, from line: String
  ) throws -> Payload {
    var trimmed = line
    if trimmed.hasSuffix("\n") { trimmed.removeLast() }
    guard !trimmed.isEmpty, !trimmed.contains("\n") else {
      throw RoundTripJobError.notSingleJSONLine(URL(fileURLWithPath: type))
    }
    return try ProtocolCodec.decodeAnalysis(payload, type: type, from: trimmed)
  }

  private static func isSHA256(_ value: String) -> Bool {
    value.count == 64 && value.allSatisfy { $0.isHexDigit && !$0.isUppercase }
  }
}

public enum RoundTripJobError: Error, Equatable {
  case destinationExists(URL)
  case invalidCandidateID
  case notSingleJSONLine(URL)
}
