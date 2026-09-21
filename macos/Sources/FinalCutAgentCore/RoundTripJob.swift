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
  public var requestURL: URL { directoryURL.appendingPathComponent(Self.requestFileName) }
  public var outputURL: URL { directoryURL.appendingPathComponent(Self.outputFileName) }
  public var resultURL: URL { directoryURL.appendingPathComponent(Self.resultFileName) }

  public func writeInput(_ data: Data, fileManager: FileManager = .default) throws {
    try Self.writeNew(data, to: inputURL, fileManager: fileManager)
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
}

public enum RoundTripJobError: Error, Equatable {
  case destinationExists(URL)
}
