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
  public static let approvedCandidatesFileName = "approved-candidates.txt"
  public static let requestFileName = "roundtrip-request.jsonl"
  public static let outputFileName = "roundtrip.fcpxml"
  public static let resultFileName = "roundtrip-result.jsonl"

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
    try readOne(
      AnalysisEstimateMessage.self,
      type: "AnalysisEstimate",
      at: analysisEstimateURL
    )
  }

  public func readCandidates() throws -> [CutCandidateMessage] {
    let text = try String(contentsOf: candidatesURL, encoding: .utf8)
    return try text.split(separator: "\n").map { line in
      try ProtocolCodec.decode(
        CutCandidateMessage.self,
        type: "CutCandidate",
        version: ProtocolCodec.analysisVersion,
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
    return try ProtocolCodec.decode(
      payload,
      type: type,
      version: ProtocolCodec.analysisVersion,
      from: line
    )
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
