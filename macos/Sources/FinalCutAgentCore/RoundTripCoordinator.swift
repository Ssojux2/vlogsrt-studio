import Foundation

/// 컨테이너 앱 custom URL: `vlogsrt-agent://roundtrip/<job-id>`.
/// job ID는 path에서만 받고 안전한 파일명 조각이어야 한다.
public enum AgentURL {
  public static let scheme = "vlogsrt-agent"
  public static let roundTripHost = "roundtrip"
  public static let analyzeHost = "analyze"
  public static let roughcutHost = "roughcut"
  public static let importHost = "import"
  public static let captionEstimateHost = "caption-estimate"
  public static let captionRunHost = "caption-run"
  /// 첫 실행 권한 흐름 — 라이브러리 부모 폴더의 쓰기 권한을 호스트가 받는다.
  public static let libraryGrantHost = "library-grant"
  /// SRT를 라이브러리 번들 옆에 놓는 배달 — 자동 가져오기는 없다.
  public static let captionDeliverHost = "caption-deliver"

  public static func roundTripURL(jobID: String) throws -> URL {
    try operationURL(host: roundTripHost, jobID: jobID)
  }

  public static func analyzeURL(jobID: String) throws -> URL {
    try operationURL(host: analyzeHost, jobID: jobID)
  }

  public static func roughcutURL(jobID: String) throws -> URL {
    try operationURL(host: roughcutHost, jobID: jobID)
  }

  /// 러프컷 가져오기 재시도(S2C) — 같은 검증된 출력을 다시 Final Cut으로 보낸다.
  public static func importURL(jobID: String) throws -> URL {
    try operationURL(host: importHost, jobID: jobID)
  }

  /// 자막 추정(무료·무키) — 패널의 명시적 동작만 부른다(S4B 패널 계약).
  public static func captionEstimateURL(jobID: String) throws -> URL {
    try operationURL(host: captionEstimateHost, jobID: jobID)
  }

  /// 자막 실행(유료) — 승인 문서가 스테이징된 job만 통과한다.
  public static func captionRunURL(jobID: String) throws -> URL {
    try operationURL(host: captionRunHost, jobID: jobID)
  }

  /// 라이브러리 부모 권한 요청 — 호스트가 폴더 선택을 받아 북마크로 남긴다.
  public static func libraryGrantURL() throws -> URL {
    guard let url = URL(string: "\(scheme)://\(libraryGrantHost)/grant") else {
      throw AgentURLError.invalidHost(libraryGrantHost)
    }
    return url
  }

  /// SRT 배달 요청 — job의 성공 SRT를 번들 옆에 원자적으로 놓는다.
  public static func captionDeliverURL(jobID: String) throws -> URL {
    try operationURL(host: captionDeliverHost, jobID: jobID)
  }

  private static func operationURL(host: String, jobID: String) throws -> URL {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw AgentURLError.invalidJobID(jobID)
    }
    guard let url = URL(string: "\(scheme)://\(host)/\(jobID)") else {
      throw AgentURLError.invalidJobID(jobID)
    }
    return url
  }

  public static func roundTripJobID(_ url: URL) throws -> String {
    guard case .roundtrip(let jobID) = try request(url) else {
      throw AgentURLError.invalidHost(url.host ?? "")
    }
    return jobID
  }

  public static func request(_ url: URL) throws -> AgentRequest {
    guard url.scheme?.lowercased() == scheme else {
      throw AgentURLError.notAgentURL(url)
    }
    guard url.query == nil, url.fragment == nil else {
      throw AgentURLError.unexpectedQueryOrFragment
    }
    let jobID = String(url.path.dropFirst())
    guard !jobID.isEmpty, !jobID.contains("/"), AppGroupWorkspace.isSafeJobID(jobID) else {
      throw AgentURLError.invalidJobID(jobID)
    }
    switch url.host?.lowercased() {
    case roundTripHost: return .roundtrip(jobID)
    case analyzeHost: return .analyze(jobID)
    case roughcutHost: return .roughcut(jobID)
    case importHost: return .importJob(jobID)
    case captionEstimateHost: return .captionEstimate(jobID)
    case captionRunHost: return .captionRun(jobID)
    case captionDeliverHost: return .captionDeliver(jobID)
    case libraryGrantHost:
      // 권한 요청은 job에 묶이지 않는다 — 세션 단위 동작이다(경로는 고정 글자).
      guard jobID == "grant" else { throw AgentURLError.invalidJobID(jobID) }
      return .libraryGrant
    default: throw AgentURLError.invalidHost(url.host ?? "")
    }
  }
}

public enum AgentRequest: Equatable, Sendable {
  case roundtrip(String)
  case analyze(String)
  case roughcut(String)
  case importJob(String)
  case captionEstimate(String)
  case captionRun(String)
  case captionDeliver(String)
  case libraryGrant
}

public enum AgentURLError: Error, Equatable {
  case notAgentURL(URL)
  case invalidHost(String)
  case invalidJobID(String)
  case unexpectedQueryOrFragment
}

/// Final Cut로의 전송 경계. 테스트는 가짜로 대체한다.
@MainActor
public protocol RoundTripImporting {
  func sendOpenDocument(_ fcpxml: URL) async throws
}

extension FinalCutImporter: RoundTripImporting {}

/// custom URL을 받아 job을 완성하는 컨테이너 측 조율자.
/// URL/Job 검증 실패만 밖으로 던지고, job이 확정된 뒤의 모든 실패는
/// failed Result로 기록한다 — import 실패를 성공으로 세탁하지 않는다.
@MainActor
public final class RoundTripCoordinator {
  private let workspace: AppGroupWorkspace
  private let importer: any RoundTripImporting
  private let service = IdentityRoundTripService()

  public init(workspace: AppGroupWorkspace, importer: any RoundTripImporting) {
    self.workspace = workspace
    self.importer = importer
  }

  public func handle(url: URL, fileManager: FileManager = .default) async throws -> RoundTripResult {
    let jobID = try AgentURL.roundTripJobID(url)
    let job = try RoundTripJob(workspace: workspace, id: jobID)
    let result: RoundTripResult
    do {
      _ = try Self.validatedRequest(job: job, jobID: jobID)
      try Self.requireCurrentSelection(job)
      try service.roundTrip(inputURL: job.inputURL, outputURL: job.outputURL, fileManager: fileManager)
      try Self.requireCurrentSelection(job)
      try await importer.sendOpenDocument(job.outputURL)
      result = try RoundTripResult(
        jobID: jobID,
        status: .roundtripReady,
        outputFileName: RoundTripJob.outputFileName,
        error: nil
      )
    } catch {
      result = try RoundTripResult(
        jobID: jobID,
        status: .failed,
        outputFileName: nil,
        error: String(describing: error)
      )
    }
    try job.writeResult(result, fileManager: fileManager)
    return result
  }

  private static func requireCurrentSelection(_ job: RoundTripJob) throws {
    guard FileManager.default.fileExists(atPath: job.projectSelectionURL.path) else { return }
    let selection = try job.readProjectSelection()
    try ActiveSelectionRegistry.live().requireCurrent(selection.selectionRevision)
  }

  /// extension이 남긴 request가 계약과 정확히 일치할 때만 변환을 시작한다.
  private static func validatedRequest(job: RoundTripJob, jobID: String) throws -> RoundTripRequest {
    let raw = try String(contentsOf: job.requestURL, encoding: .utf8)
    var line = raw
    if line.hasSuffix("\n") { line.removeLast() }
    guard !line.isEmpty, !line.contains("\n") else {
      throw RoundTripRequestError.notASingleLine
    }
    let request = try ProtocolCodec.decode(
      RoundTripRequest.self,
      type: "RoundTripRequest",
      from: line
    )
    guard
      request.jobID == jobID,
      request.inputFileName == RoundTripJob.inputFileName,
      request.outputFileName == RoundTripJob.outputFileName
    else {
      throw RoundTripRequestError.mismatch(request)
    }
    return request
  }
}

public enum RoundTripRequestError: Error, Equatable {
  case notASingleLine
  case mismatch(RoundTripRequest)
}
