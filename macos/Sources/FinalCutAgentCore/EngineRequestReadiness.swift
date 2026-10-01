import Foundation

/// 읽기 전용 readiness 자식의 보고서 — 실제 선택된 실행길이 **스스로** 남긴 한 줄.
///
/// 공개 데이터만 담는다: 파이썬 실행 파일·버전, 실제 자식 환경에서 풀린
/// ffmpeg·ffprobe. 환경 통째로·자격 증명 재료는 계약 자체가 없다(진입점의
/// `_readiness_payload`와 같은 모양). 못 찾은 도구는 null이다 — 빈 문자열이
/// '있다'는 말을 하는 길이 없다.
public struct EngineReadinessReport: Equatable, Sendable, Decodable {
  public let engine: String?
  public let readinessVersion: Int?
  public let pythonExecutable: String?
  public let pythonVersion: String?
  public let ffmpeg: String?
  public let ffprobe: String?

  public init(
    engine: String?, readinessVersion: Int?, pythonExecutable: String?, pythonVersion: String?,
    ffmpeg: String?, ffprobe: String?
  ) {
    self.engine = engine
    self.readinessVersion = readinessVersion
    self.pythonExecutable = pythonExecutable
    self.pythonVersion = pythonVersion
    self.ffmpeg = ffmpeg
    self.ffprobe = ffprobe
  }

  private enum CodingKeys: String, CodingKey {
    case engine
    case readinessVersion = "readiness_version"
    case python
    case tools
  }

  private enum PythonKeys: String, CodingKey {
    case executable
    case version
  }

  private enum ToolKeys: String, CodingKey {
    case ffmpeg
    case ffprobe
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    self.engine = try container.decodeIfPresent(String.self, forKey: .engine)
    self.readinessVersion = try container.decodeIfPresent(Int.self, forKey: .readinessVersion)
    let python = try? container.nestedContainer(keyedBy: PythonKeys.self, forKey: .python)
    self.pythonExecutable = try python?.decodeIfPresent(String.self, forKey: .executable)
    self.pythonVersion = try python?.decodeIfPresent(String.self, forKey: .version)
    let tools = try? container.nestedContainer(keyedBy: ToolKeys.self, forKey: .tools)
    self.ffmpeg = try tools?.decodeIfPresent(String.self, forKey: .ffmpeg)
    self.ffprobe = try tools?.decodeIfPresent(String.self, forKey: .ffprobe)
  }
}

/// readiness 요청의 재료 — 같은 선택 실행길, 같은 운반자, 같은 실제 환경.
public struct EngineReadinessRequest: Sendable {
  public let mode: EngineLaunchMode
  public let jobID: String
  public let appGroupJob: URL
  public let applicationScriptsDirectory: URL?
  public let engineRoot: URL?
  public let credentials: EngineRoleCredentials

  public init(
    mode: EngineLaunchMode,
    jobID: String,
    appGroupJob: URL,
    applicationScriptsDirectory: URL?,
    engineRoot: URL?,
    credentials: EngineRoleCredentials
  ) {
    self.mode = mode
    self.jobID = jobID
    self.appGroupJob = appGroupJob
    self.applicationScriptsDirectory = applicationScriptsDirectory
    self.engineRoot = engineRoot
    self.credentials = credentials
  }
}

/// 앱 엔진 요청의 준비 실패 — 문면은 정해진 이유와 교정 동작뿐이다.
///
/// 어떤 경우에도 자격 증명 값이나 원본 환경 내용이 문면에 오지 않는다. 실패는
/// 실행기·제공자·import를 전부 막는다(조정자가 던지면 세션은 시작조차 안 한다).
public enum EnginePreflightError: Error, Equatable, LocalizedError {
  case missingInput
  case unreadableInput
  case missingProjectSelection
  case missingDevelopmentWorkspace
  case engineUnavailable(String)
  case visionEndpointInvalid(String)
  case scopeStartFailed(String)
  case grantInvalid(String)
  case unreadableMedia([String])
  case credentialStorageUnavailable
  case paidCredentialMissing(role: String)
  case workspaceNotWritable(String)
  case destinationNotWritable(String)
  case readinessFailed(String)
  /// 자막 job(S4A/S4B) — 스테이징된 최종 내보내기 재료가 없다.
  case missingCaptionInput(String)
  case missingCaptionAuthorization
  case captionSTTKeyMissing

  public var errorDescription: String? {
    switch self {
    case .missingInput:
      "작업 폴더에 input.fcpxml이 없습니다 — Final Cut에서 프로젝트를 다시 공유하세요"
    case .missingCaptionInput(let name):
      "자막 작업 폴더에 \(name)이 없습니다 — 공유 받기를 다시 하세요"
    case .missingCaptionAuthorization:
      "자막 승인 문서(caption-authorization.jsonl)가 없습니다 — 추정을 승인한 뒤 실행하세요"
    case .captionSTTKeyMissing:
      "승인된 자막 실행에는 STT API 키가 필요합니다 — 키를 저장한 뒤 다시 실행하세요"
    case .unreadableInput:
      "input.fcpxml을 읽을 수 없습니다 — Final Cut에서 프로젝트를 다시 공유하세요"
    case .missingProjectSelection:
      "활성 프로젝트 선택 기록이 없습니다 — Final Cut에서 타임라인을 연 뒤 다시 시도하세요"
    case .missingDevelopmentWorkspace:
      "개발 작업 폴더가 선택되어 있지 않습니다 — '개발용 작업 폴더 선택'으로 먼저 지정하세요"
    case .engineUnavailable(let reason):
      reason
    case .visionEndpointInvalid(let message):
      message
    case .scopeStartFailed(let path):
      "미디어 접근 권한(security scope)을 시작하지 못했습니다: \(path)"
        + " — 미디어 루트를 다시 추가하세요"
    case .grantInvalid(let reason):
      reason
    case .unreadableMedia(let paths):
      "참조된 원본 미디어를 읽을 수 없습니다: \(paths.joined(separator: ", "))"
        + " — 미디어 루트 아래 원본이 그 자리에 있는지 확인하세요"
    case .credentialStorageUnavailable:
      EngineRoleSnapshot.storageFailedMessage
    case .paidCredentialMissing(let role):
      switch role {
      case "stt":
        "승인된 유료 분석에는 STT API 키가 필요합니다 — 키를 저장한 뒤 다시 실행하세요"
      case "vision":
        "승인된 유료 분석에는 비전 API 키가 필요합니다 — 키를 저장한 뒤 다시 실행하세요"
      default:
        "승인된 유료 분석에는 비전 API base URL이 필요합니다 — 엔드포인트를 저장한 뒤 다시 실행하세요"
      }
    case .workspaceNotWritable(let path):
      "작업 폴더에 쓸 수 없습니다: \(path) — 위치를 확인하거나 다른 폴더를 선택하세요"
    case .destinationNotWritable(let path):
      "결과를 쓸 자리가 이미 읽기 전용이거나 무효합니다: \(path)"
        + " — 해당 파일을 지우거나 쓰기 가능하게 만든 뒤 다시 실행하세요"
    case .readinessFailed(let reason):
      reason
    }
  }
}

/// readiness 보고서의 판정 — 모양·도구·번들 도구의 문이 전부 닫힌 문이다.
public enum EngineReadiness {
  /// 진입점의 readiness 연산 이름 — 두 실행기 공장이 같은 상수를 쓴다.
  public static let operationName = "readiness"
  /// 보고서 한 줄의 상한 — 진입점의 `_READINESS_MAX_BYTES`와 같은 값.
  public static let reportMaximumBytes = 4096

  /// 보고서를 요청의 실행길에 견준다 — 통과면 돌아가고, 문제면 정해진 글로 닫는다.
  ///
  /// 도구 요구는 연산에 맞게만 붙는다(roughcut은 분석 전용 도구를 요구하지
  /// 않는다). 번들길이 도구를 요구하는 순간, 보고의 경로는 **번들 자리 그 자신**과
  /// 같아야 한다 — 알려진 이상 경로의 부정 목록이 아니라 정준 경로의 동일성으로
  /// 막는다(Homebrew는 물론 그 밖의 모든 번들 밖 자리를 함께 닫는다).
  public static func evaluate(
    report: EngineReadinessReport,
    mode: EngineLaunchMode,
    engineRoot: URL?,
    requiresTools: Bool,
    fileManager: FileManager = .default
  ) throws {
    guard report.engine == "vlogsrt-engine", report.readinessVersion == 1,
      let python = report.pythonExecutable, !python.isEmpty,
      let version = report.pythonVersion, !version.isEmpty
    else {
      throw EnginePreflightError.readinessFailed(
        "엔진이 이해할 수 있는 준비 보고를 남기지 않았습니다 — 개발 설치"
          + "(install-development-engine.sh) 또는 번들을 다시 만드세요")
    }
    guard requiresTools else { return }
    for (name, resolved) in [("ffmpeg", report.ffmpeg), ("ffprobe", report.ffprobe)] {
      guard let resolved, !resolved.isEmpty else {
        throw EnginePreflightError.readinessFailed(
          "엔진 환경에서 \(name)를 찾지 못했습니다 — 번들이면 번들 bin을,"
            + " 개발이면 ffmpeg가 설치된 환경에서 설치 스크립트를 다시 돌리세요")
      }
    }
    if mode == .bundled {
      try requireBundledTools(engineRoot: engineRoot, report: report, fileManager: fileManager)
    }
  }

  /// 번들 실행의 도구는 번들 bin이어야 한다 — 보고가 말하는 경로가 번들 자리와
  /// 정준으로 같지 않으면 준비가 아니라 번들 파손의 증거다.
  private static func requireBundledTools(
    engineRoot: URL?, report: EngineReadinessReport, fileManager: FileManager
  ) throws {
    guard let engineRoot else {
      throw EnginePreflightError.readinessFailed(
        "번들 엔진 뿌리를 알 수 없습니다 — 앱 번들이 깨졌습니다")
    }
    let bin = engineRoot.appendingPathComponent("bin", isDirectory: true)
    for tool in ["ffmpeg", "ffprobe"] {
      let bundled = bin.appendingPathComponent(tool)
      let canonicalBundled = bundled.resolvingSymlinksInPath().standardizedFileURL.path
      guard fileManager.isExecutableFile(atPath: bundled.path) else {
        throw EnginePreflightError.readinessFailed(
          "번들 엔진에 \(tool)가 없습니다: \(bundled.path) — 앱 번들을 다시 만드세요")
      }
      let resolved = tool == "ffmpeg" ? report.ffmpeg : report.ffprobe
      guard let resolved, !resolved.isEmpty else { continue }
      let canonicalResolved = URL(fileURLWithPath: resolved)
        .resolvingSymlinksInPath().standardizedFileURL.path
      guard canonicalResolved == canonicalBundled else {
        throw EnginePreflightError.readinessFailed(
          "번들 실행이 번들 밖의 \(tool)(\(resolved))로 준비를 말하고 있습니다"
            + " — 앱 번들의 bin을 다시 만드세요")
      }
    }
  }
}

/// 실제 readiness 자식을 띄우는 탐침 — 같은 실행기·같은 운반자·같은 환경.
public enum EngineReadinessProber {
  /// 요청의 실행길로 readiness 자식을 돌리고 보고서를 해석해 돌려준다.
  ///
  /// 실패는 전부 닫힌다: 시작 실패·비0 종료·보고서 부재·상한 초과·해석 불가.
  /// 어떤 실패 문면에도 운반자 값이 오지 않는다(진입점이 지운 뒤의 정해진 글).
  public static func probe(
    _ request: EngineReadinessRequest, fileManager: FileManager = .default
  ) async throws -> EngineReadinessReport {
    switch request.mode {
    case .development:
      guard let scripts = request.applicationScriptsDirectory else {
        throw EnginePreflightError.readinessFailed(
          "개발 실행 스크립트 위치를 알 수 없습니다 — 설치 스크립트를 다시 돌리세요")
      }
      let invocation = try DevelopmentEngineInvocation.createReadiness(
        jobID: request.jobID,
        appGroupJob: request.appGroupJob,
        applicationScriptsDirectory: scripts,
        credentials: request.credentials)
      return try await DevelopmentEngineLauncher().runReadiness(invocation)
    case .bundled:
      guard let engineRoot = request.engineRoot else {
        throw EnginePreflightError.readinessFailed(
          "번들 엔진 뿌리를 알 수 없습니다 — 앱 번들이 깨졌습니다")
      }
      let token = UUID().uuidString
      let directory = fileManager.temporaryDirectory
        .appendingPathComponent("vlogsrt-readiness-\(token)", isDirectory: true)
      try fileManager.createDirectory(at: directory, withIntermediateDirectories: false)
      defer { try? fileManager.removeItem(at: directory) }
      let reportURL = directory.appendingPathComponent("readiness.json")
      let invocation = try BundledEngineInvocation.createReadiness(
        jobID: request.jobID,
        jobDirectory: request.appGroupJob,
        engineRoot: engineRoot,
        reportURL: reportURL,
        credentials: request.credentials)
      let launcher = BundledEngineLauncher()
      let status: Int32
      do {
        status = try await Task.detached(priority: .userInitiated) {
          try launcher.run(invocation)
        }.value
      } catch {
        // 시작 실패(없는 실행기·권한 없음)도 정해진 글로 닫는다 — 원본 오류의
        // 문면이 부모 환경을 흘리지 않게 한다.
        throw EnginePreflightError.readinessFailed(
          "번들 엔진을 띄우지 못했습니다 — 앱 번들을 다시 만드세요")
      }
      guard status == 0 else {
        throw EnginePreflightError.readinessFailed(
          "번들 엔진 준비 확인이 실패로 끝났습니다(종료 코드 \(status)) — 앱 번들을 다시 만드세요")
      }
      return try decodeBoundedReport(at: reportURL)
    }
  }

  /// 상한 안에서만 읽는다 — 큰 기록은 해석되기 전에 실패다. 읽기가 던져도
  /// 서술자는 닫는다(defer).
  static func decodeBoundedReport(at url: URL) throws -> EngineReadinessReport {
    guard let handle = FileHandle(forReadingAtPath: url.path) else {
      throw EnginePreflightError.readinessFailed(
        "엔진이 준비 보고를 남기지 않았습니다 — 실행기 설치 또는 번들을 다시 만드세요")
    }
    defer { try? handle.close() }
    let head = try handle.read(upToCount: EngineReadiness.reportMaximumBytes + 1)
    guard let head,
      head.count <= EngineReadiness.reportMaximumBytes,
      let report = try? JSONDecoder().decode(EngineReadinessReport.self, from: head)
    else {
      throw EnginePreflightError.readinessFailed(
        "엔진의 준비 보고를 해석할 수 없습니다 — 실행기 설치 또는 번들을 다시 만드세요")
    }
    return report
  }
}

/// 걸린 scope의 명단 — 부분 실패의 해제와 정상 종료의 해제가 같은 길을 쓴다.
///
/// 정체는 정준 경로로만 잰다(개발 작업폴더가 미디어 루트와 같으면 scope는
/// 하나다). 시작·해제는 북마크가 풀어준 URL 그대로 부른다 — 정준화는 중복
/// 판별에만 쓴다. 해제는 **정확히 한 번**: `releaseAll`이 명단을 비우므로 실패
/// 길의 즉시 해제와 호출자의 defer가 겹쳐도 두 번 풀리지 않는다.
struct AcquiredScopes {
  private let start: @Sendable (URL) -> Bool
  private let stop: @Sendable (URL) -> Void
  private var acquired: [(identity: String, url: URL)] = []

  init(start: @escaping @Sendable (URL) -> Bool, stop: @escaping @Sendable (URL) -> Void) {
    self.start = start
    self.stop = stop
  }

  /// 하나씩 건다 — 정준 중복은 건너뛰고, 실패하면 걸었던 것을 전부 풀고 올린다.
  mutating func acquire(_ urls: [URL]) throws {
    for url in urls {
      let identity = url.resolvingSymlinksInPath().standardizedFileURL.path
      guard !acquired.contains(where: { $0.identity == identity }) else { continue }
      guard start(url) else {
        releaseAll()
        throw EnginePreflightError.scopeStartFailed(identity)
      }
      acquired.append((identity, url))
    }
  }

  mutating func releaseAll() {
    for entry in acquired.reversed() {
      stop(entry.url)
    }
    acquired.removeAll()
  }

  var startedCount: Int { acquired.count }
}

/// 확정된 실행길의 재료 — 모드와 실행기 자리를 한 덩어리로. 개발 작업폴더는
/// scope 안에서 정착되므로 여기 없다(조정자가 따로 흘린다).
struct LauncherSelection: Sendable {
  let mode: EngineLaunchMode
  let engineRoot: URL?
  let applicationScriptsDirectory: URL?
}

/// 앱 엔진 요청의 준비 조정자 — main.swift의 요청 본문을 core로 옮긴 것.
///
/// 순서가 계약이다(S2A-2 마지막 단위):
/// 1. 잡 아이디·입력·선택 검증(읽기만)
/// 2. 취소·활성 리비전 확인(아무것도 쓰지 않고 물러나는 두 길)
/// 3. 자격 증명 스냅숏과 역할 게이트(유료만) — 키 없는 추정·러프컷은 묻지 않는다
/// 4. 실행길 검증 — 래퍼·번들 실행기의 실행 가능성, Release 무폴백, 운반자 부호화
/// 5. scope 확보(미디어 루트 전부 + 선택된 개발 작업폴더) — 부분 실패는 해제
/// 6. 권한 검사·원본 가독성(읽기만) — 문서는 아직 안 쓴다
/// 7. 쓰기 증명(변형 전: 잡·작업공간·외부 자리·roughcut 자리 양쪽)
/// 8. 읽기 전용 readiness 자식 → 통과 뒤에만: 권한 문서 기록 → 개발 스테이징 →
///    스테이징된 실제 자리 재증명 → 최종 호출 조립
/// 9. 세션 실행 — scope는 실행·동기화가 끝날 때까지 산다
/// 어떤 끝에서든 scope는 전부 풀린다(defer, 정확히 한 번). 세션은 이 조정자가
/// 아니면 시작되지 않으므로, 실패한 준비 뒤에는 실행기·제공자·import가 없다.
@MainActor
public final class EngineRequestCoordinator {
  /// 주입 경계 — 시험은 scope·자격 증명·readiness·실행기를 갈아끼우고 순서와
  /// 수명을 관찰한다. 실제 배선은 `live`가 한다. 북마크는 두 단계로 나뉜다:
  /// `peek*`는 보호받는 읽기 없이 URL만 복원하고(순수 계산), `settle*`은 scope가
  /// 걸린 뒤에만 검증·갱신한다 — 앱 경로가 scope 없이 보호받는 경로를
  /// 읽거나 저장을 남기는 일이 없다(S2A-2 마지막 단위).
  public struct Dependencies: Sendable {
    public let peekRoots: @Sendable () throws -> [SecurityScopedBookmarkStore.PeekedBookmark]
    public let settleRoots: @Sendable ([SecurityScopedBookmarkStore.PeekedBookmark]) throws -> [URL]
    public let peekDevelopmentWorkspace: @Sendable () throws ->
      SecurityScopedBookmarkStore.PeekedBookmark?
    public let settleDevelopmentWorkspace: @Sendable (
      SecurityScopedBookmarkStore.PeekedBookmark?
    ) throws -> URL?
    public let loadCredentials: @Sendable () -> EngineRoleSnapshot
    public let startScope: @Sendable (URL) -> Bool
    public let stopScope: @Sendable (URL) -> Void
    public let readiness: @Sendable (EngineReadinessRequest) async throws -> EngineReadinessReport
    public let stageDevelopmentJob: @Sendable (
      _ jobID: String, _ appGroupJob: URL, _ externalRoot: URL
    ) throws -> URL
    public let executor: any EngineExecuting
    public let syncResults: (@Sendable (URL, URL, Bool) throws -> Void)?
    public let importRoughcut: (@Sendable (URL) async throws -> Void)?
    public let onSessionStart: (@MainActor (EngineSessionContext) -> Void)?

    public init(
      peekRoots: @escaping @Sendable () throws -> [SecurityScopedBookmarkStore.PeekedBookmark],
      settleRoots: @escaping @Sendable ([SecurityScopedBookmarkStore.PeekedBookmark]) throws -> [URL],
      peekDevelopmentWorkspace: @escaping @Sendable () throws ->
        SecurityScopedBookmarkStore.PeekedBookmark?,
      settleDevelopmentWorkspace: @escaping @Sendable (
        SecurityScopedBookmarkStore.PeekedBookmark?
      ) throws -> URL?,
      loadCredentials: @escaping @Sendable () -> EngineRoleSnapshot,
      startScope: @escaping @Sendable (URL) -> Bool,
      stopScope: @escaping @Sendable (URL) -> Void,
      readiness: @escaping @Sendable (
        EngineReadinessRequest
      ) async throws -> EngineReadinessReport,
      stageDevelopmentJob: @escaping @Sendable (
        _ jobID: String, _ appGroupJob: URL, _ externalRoot: URL
      ) throws -> URL,
      executor: any EngineExecuting,
      syncResults: (@Sendable (URL, URL, Bool) throws -> Void)? = nil,
      importRoughcut: (@Sendable (URL) async throws -> Void)? = nil,
      onSessionStart: (@MainActor (EngineSessionContext) -> Void)? = nil
    ) {
      self.peekRoots = peekRoots
      self.settleRoots = settleRoots
      self.peekDevelopmentWorkspace = peekDevelopmentWorkspace
      self.settleDevelopmentWorkspace = settleDevelopmentWorkspace
      self.loadCredentials = loadCredentials
      self.startScope = startScope
      self.stopScope = stopScope
      self.readiness = readiness
      self.stageDevelopmentJob = stageDevelopmentJob
      self.executor = executor
      self.syncResults = syncResults
      self.importRoughcut = importRoughcut
      self.onSessionStart = onSessionStart
    }
  }

  private let deps: Dependencies

  public init(dependencies: Dependencies) {
    self.deps = dependencies
  }

  public func run(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    configuration: EngineBuildConfiguration,
    engineRoot: URL?,
    applicationScriptsDirectory: URL?,
    registry: ActiveSelectionRegistry,
    fileManager: FileManager = .default
  ) async throws -> EngineSessionOutcome {
    let selection = try validateBasics(job: job, fileManager: fileManager)
    if let early = try earlyExit(job: job, registry: registry, revision: selection.selectionRevision)
    {
      return early
    }
    let credentials = try loadAndGateCredentials(operation: operation, job: job)
    // 스냅숏: 루트와 실행길은 한 번만 읽는다 — scope가 권한 검사·스테이징과
    // 항상 같은 값을 보게 하는 유일한 방법이다. 북마크는 **1단계 복원만** 한다
    // (보호받는 읽기·갱신 저장 없음) — 검증·갱신은 scope가 걸린 뒤 settle에서.
    // 번들 선택은 개발 작업폴더 북마크를 **peek조차 하지 않는다**.
    let peekedRoots = try deps.peekRoots()
    let mode = EngineLaunchMode.resolve(bundleURL: engineRoot, fileManager: fileManager)
    if mode == .development, configuration != .debug {
      // Release는 개발 폴백이 없다 — workspace 북마크를 peek조차 하지 않는다.
      throw EnginePreflightError.engineUnavailable(
        "Release 빌드는 번들 엔진으로만 실행한다 — Resources/Engine이 없다(개발 폴백 없음)")
    }
    let peekedWorkspace =
      mode == .development ? try deps.peekDevelopmentWorkspace() : nil
    let launcher = try validateLauncher(
      operation: operation, job: job, configuration: configuration, engineRoot: engineRoot,
      applicationScriptsDirectory: applicationScriptsDirectory, credentials: credentials,
      peekedWorkspace: peekedWorkspace, fileManager: fileManager)
    var scopes = AcquiredScopes(start: deps.startScope, stop: deps.stopScope)
    defer { scopes.releaseAll() }
    try scopes.acquire(scopeTargets(roots: peekedRoots, devWorkspace: peekedWorkspace))
    // scope 안에서 북마크를 정착시킨다 — 검증(symlink)·갱신 저장이 이제부터다.
    let roots = try deps.settleRoots(peekedRoots)
    let devWorkspace = try deps.settleDevelopmentWorkspace(peekedWorkspace)
    // 읽기 전용 검사가 변형보다 먼저다: 권한·가독성·쓰기 증명·readiness가 통과한
    // 뒤에만 권한 문서와 스테이징이 남는다 — 실패한 준비가 일을 바꾸지 않는다.
    // 아직 없는 자리(스테이징 전 외부 job)는 가장 가까운 존재 조상에서 증명한다.
    let grant = try validateGrant(job: job, roots: roots, fileManager: fileManager)
    let stagedFuture = stagedJobFuture(job: job, devWorkspace: devWorkspace)
    try verifyWritableSurfaces(
      operation: operation, job: job, externalJob: stagedFuture, fileManager: fileManager)
    try await probeReadiness(
      operation: operation, job: job, launcher: launcher, credentials: credentials,
      fileManager: fileManager)
    try grant.write(to: job.directoryURL, fileManager: fileManager)
    let externalJob = try stageDevelopmentJob(job: job, devWorkspace: devWorkspace)
    // 스테이징이 만든 실제 자리를 다시 잰다 — 미리 보던 자리와 같다는 보장이
    // 아니라 그 자리의 사실이다(외부 job 안의 workspace 자리 포함).
    try verifyStagedSurfaces(operation: operation, job: job, externalJob: externalJob)
    let plan = try buildFinalPlan(
      operation: operation, job: job, launcher: launcher, externalJob: externalJob,
      credentials: credentials)
    return try await runSession(
      operation: operation, job: job, plan: plan, externalJob: externalJob,
      selection: selection.selectionRevision, registry: registry)
  }

  // ─── 단계들 — 각자 던지는 오류가 곧 교정 글이다 ───────────────────

  /// 1단계: 잡의 기본 재료 — 잡 아이디는 RoundTripJob이 이미 지켰다.
  private func validateBasics(
    job: RoundTripJob, fileManager: FileManager
  ) throws -> ProjectSelectionMessage {
    let input = job.inputURL
    guard fileManager.fileExists(atPath: input.path) else {
      throw EnginePreflightError.missingInput
    }
    guard fileManager.isReadableFile(atPath: input.path) else {
      throw EnginePreflightError.unreadableInput
    }
    guard let selection = try? job.readProjectSelection() else {
      throw EnginePreflightError.missingProjectSelection
    }
    return selection
  }

  /// 2단계: 물러날 두 길 — 취소는 기록 없이, 드리프트는 취소 기록을 남기고.
  private func earlyExit(
    job: RoundTripJob, registry: ActiveSelectionRegistry, revision: String
  ) throws -> EngineSessionOutcome? {
    if CancellationStore.isCancelled(jobURL: job.directoryURL) {
      return .cancelledBeforeRun
    }
    do {
      try registry.requireCurrent(revision)
    } catch {
      // 스테이징 전이므로 실행 루트는 App Group 하나다 — 엔진은 job 뿌리에서
      // 같은 기록을 읽는다(S1A-R1의 묶음 규칙 그대로).
      try RunningJobCancellation(
        appGroupJob: job.directoryURL, externalJob: nil, runningJob: job.directoryURL
      ).record(
        reason: "active_project_changed",
        requestedAt: ISO8601DateFormatter().string(from: Date()))
      return .staleSelection
    }
    return nil
  }

  /// 3단계: 스냅숏 한 덩어리 + 역할 게이트 — 유료 analyze만 세 값을 요구한다.
  private func loadAndGateCredentials(
    operation: DevelopmentEngineOperation, job: RoundTripJob
  ) throws -> EngineRoleCredentials {
    let snapshot = deps.loadCredentials()
    guard case .ready(let credentials) = snapshot else {
      throw EnginePreflightError.credentialStorageUnavailable
    }
    if let blocker = EngineRoleCredentialStore.launchBlocker(for: credentials) {
      throw EnginePreflightError.visionEndpointInvalid(blocker)
    }
    guard operation == .analyze,
      FileManager.default.fileExists(atPath: job.analysisAuthorizationURL.path)
    else { return credentials }
    // 공백만 남은 값은 부재다 — 저장 문면을 그대로 믿지 않고 '실효 값'을 잰다.
    let effective = (
      stt: credentials.sttKey.trimmingCharacters(in: .whitespacesAndNewlines),
      vision: credentials.visionKey.trimmingCharacters(in: .whitespacesAndNewlines),
      endpoint: credentials.visionBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
    )
    for role in ["stt", "vision", "endpoint"] {
      let present: Bool
      switch role {
      case "stt": present = !effective.stt.isEmpty
      case "vision": present = !effective.vision.isEmpty
      default: present = !effective.endpoint.isEmpty
      }
      guard present else { throw EnginePreflightError.paidCredentialMissing(role: role) }
    }
    return credentials
  }

  /// 4단계: 실행길 검증 — 모드·실행 가능성·Release 무폴백·운반자 부호화가
  /// scope 확보보다 먼저 끝난다. 개발 자리는 아직 뿌리다(스테이징은 나중).
  /// 북마크는 1단계 복원(peek) 값만 본다 — 검증·갱신은 scope 뒤 settle에서.
  private func validateLauncher(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    configuration: EngineBuildConfiguration,
    engineRoot: URL?,
    applicationScriptsDirectory: URL?,
    credentials: EngineRoleCredentials,
    peekedWorkspace: SecurityScopedBookmarkStore.PeekedBookmark?,
    fileManager: FileManager
  ) throws -> LauncherSelection {
    // 번들이면 개발 작업폴더 북마크를 아예 풀지 않는다. Release 무폴백 검사는
    // 호출자(run)가 workspace를 peek하기 전에 이미 닫았다 — 여기는 debug 개발길만.
    let mode = EngineLaunchMode.resolve(bundleURL: engineRoot, fileManager: fileManager)
    var devWorkspace: URL? = nil
    if mode == .development {
      devWorkspace = peekedWorkspace?.url
      guard devWorkspace != nil else {
        throw EnginePreflightError.missingDevelopmentWorkspace
      }
    }
    // 검증 계획(throwing)으로 호출 조립까지 잰다 — 결과 계획은 버린다(개발 자리가
    // 뿌리라서). 그르른 운반자·없는 래퍼는 이 자리에서 정해진 글로 죽는다.
    let probe = try EngineRuntime.resolvePlanValidated(
      configuration: configuration,
      operation: operation,
      jobID: job.id,
      appGroupJob: job.directoryURL,
      engineRoot: engineRoot,
      applicationScriptsDirectory: applicationScriptsDirectory,
      developmentExternalJob: devWorkspace,
      credentials: credentials,
      fileManager: fileManager)
    guard probe.isExecutable else {
      if case .unavailable(let reason) = probe {
        throw EnginePreflightError.engineUnavailable(reason)
      }
      throw EnginePreflightError.engineUnavailable("실행 가능한 엔진이 없다")
    }
    return LauncherSelection(
      mode: mode, engineRoot: engineRoot,
      applicationScriptsDirectory: applicationScriptsDirectory)
  }

  /// 5단계가 필요한 루트 — 정준 중복 판별은 AcquiredScopes가 한다.
  private func scopeTargets(
    roots: [SecurityScopedBookmarkStore.PeekedBookmark],
    devWorkspace: SecurityScopedBookmarkStore.PeekedBookmark?
  ) -> [URL] {
    var targets = roots.map(\.url)
    if let devWorkspace { targets.append(devWorkspace.url) }
    return targets
  }

  /// 6단계(읽기만): 참조가 루트 안인지와 원본 가독성 — 문서는 아직 안 쓴다.
  private func validateGrant(
    job: RoundTripJob, roots: [URL], fileManager: FileManager
  ) throws -> MediaGrantDocument {
    do {
      let grant = try MediaGrantValidator.validate(
        fcpxml: job.inputURL, roots: roots, fileManager: fileManager)
      let unreadable = try MediaGrantValidator.referencedMediaURLs(job.inputURL)
        .filter { !fileManager.isReadableFile(atPath: $0.path) }
      if !unreadable.isEmpty {
        throw EnginePreflightError.unreadableMedia(unreadable.map(\.path))
      }
      return grant
    } catch let error as MediaGrantError {
      throw EnginePreflightError.grantInvalid(
        error.localizedDescription + " — 미디어 루트를 확인하세요")
    }
  }

  /// 8단계(개발만): 외부 job 스테이징 — 준비 검사가 통과한 뒤에야 남는다.
  /// 작업폴더는 scope 안에서 정착(settle)된 값이다.
  private func stageDevelopmentJob(job: RoundTripJob, devWorkspace: URL?) throws -> URL? {
    guard let devWorkspace else { return nil }
    return try deps.stageDevelopmentJob(job.id, job.directoryURL, devWorkspace)
  }

  /// 스테이징될 외부 job의 앞자리 — 변형 전 검사가 미리 볼 자리다(개발만).
  private func stagedJobFuture(job: RoundTripJob, devWorkspace: URL?) -> URL? {
    guard let devWorkspace else { return nil }
    return devWorkspace.appendingPathComponent("Jobs").appendingPathComponent(job.id)
  }

  /// 8단계(변형 전): 쓰기 증명 — 잡·작업공간(두 길), 외부 자리와 roughcut
  /// 결과 자리 **양쪽**(개발은 외부에서 쓰고 App Group으로 동기화된다).
  private func verifyWritableSurfaces(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    externalJob: URL?,
    fileManager: FileManager
  ) throws {
    try requireWritable(job.directoryURL, fileManager: fileManager)
    try requireWritable(
      job.directoryURL.appendingPathComponent("workspace", isDirectory: true),
      fileManager: fileManager)
    if let externalJob {
      try requireWritable(externalJob, fileManager: fileManager)
    }
    if operation == .roughcut {
      // 두 길 모두 App Group 자리로 동기화된다; 개발은 외부 자리에 먼저 쓴다.
      try requireWritableDestination(
        job.directoryURL.appendingPathComponent("roughcut.fcpxml"), fileManager: fileManager)
      if let externalJob {
        try requireWritableDestination(
          externalJob.appendingPathComponent("roughcut.fcpxml"), fileManager: fileManager)
      }
    }
  }

  /// 8단계(변형 뒤): 스테이징이 만든 실제 외부 자리 — 안의 workspace 자리까지.
  private func verifyStagedSurfaces(
    operation: DevelopmentEngineOperation, job: RoundTripJob, externalJob: URL?
  ) throws {
    guard let externalJob else { return }
    try requireWritable(externalJob, fileManager: .default)
    try requireWritable(
      externalJob.appendingPathComponent("workspace", isDirectory: true),
      fileManager: .default)
    if operation == .roughcut {
      try requireWritableDestination(
        externalJob.appendingPathComponent("roughcut.fcpxml"), fileManager: .default)
    }
  }

  /// 최종 호출 — 개발길의 argv는 **스테이징된 job**을 가리킨다(뿌리가 아니다).
  /// 운반자는 검증 때와 같은 값이다.
  private func buildFinalPlan(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    launcher: LauncherSelection,
    externalJob: URL?,
    credentials: EngineRoleCredentials
  ) throws -> EngineLaunchPlan {
    switch launcher.mode {
    case .bundled:
      guard let engineRoot = launcher.engineRoot else {
        throw EnginePreflightError.engineUnavailable("번들 엔진 뿌리를 알 수 없다")
      }
      return .bundled(try BundledEngineInvocation.createRoles(
        operation: operation, jobID: job.id, jobDirectory: job.directoryURL,
        engineRoot: engineRoot, credentials: credentials))
    case .development:
      guard let externalJob, let scripts = launcher.applicationScriptsDirectory else {
        throw EnginePreflightError.engineUnavailable("개발 외부 job이 스테이징되지 않았다")
      }
      return .development(try DevelopmentEngineInvocation.createRoles(
        operation: operation, jobID: job.id, externalJob: externalJob,
        applicationScriptsDirectory: scripts, credentials: credentials))
    }
  }

  /// 7단계: 읽기 전용 readiness 자식 — analyze만 도구를 요구한다.
  private func probeReadiness(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    launcher: LauncherSelection,
    credentials: EngineRoleCredentials,
    fileManager: FileManager
  ) async throws {
    let request = EngineReadinessRequest(
      mode: launcher.mode,
      jobID: job.id,
      appGroupJob: job.directoryURL,
      applicationScriptsDirectory: launcher.applicationScriptsDirectory,
      engineRoot: launcher.engineRoot,
      credentials: credentials)
    let report: EngineReadinessReport
    do {
      report = try await deps.readiness(request)
    } catch let error as EnginePreflightError {
      throw error
    } catch {
      // 원본 오류 문면이 부모 환경을 흘리지 않게 정해진 글로 닫는다(S2A-2).
      throw EnginePreflightError.readinessFailed(
        "엔진 준비 확인을 실행하지 못했습니다 — 개발 설치"
          + "(install-development-engine.sh) 또는 앱 번들을 다시 만드세요")
    }
    try EngineReadiness.evaluate(
      report: report, mode: launcher.mode, engineRoot: launcher.engineRoot,
      requiresTools: operation == .analyze, fileManager: fileManager)
  }

  /// 9단계: 세션 — scope는 이 문 안에서 살고, 호출자의 defer가 한다.
  private func runSession(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    plan: EngineLaunchPlan,
    externalJob: URL?,
    selection: String,
    registry: ActiveSelectionRegistry
  ) async throws -> EngineSessionOutcome {
    let context = EngineSessionContext(
      operation: operation,
      jobID: job.id,
      appGroupJob: job.directoryURL,
      externalJob: externalJob,
      plan: plan,
      selectionRevision: selection,
      registry: registry)
    deps.onSessionStart?(context)
    let runner = EngineSessionRunner(executor: deps.executor)
    // 기본 동기화 클로저는 타입 안(nonisolated)에 정의된 것을 그대로 쓴다 —
    // MainActor 자리에서 클로저 리터럴을 만들어 넘기면 격리 검사에 걸린다.
    if let syncResults = deps.syncResults {
      return try await runner.run(
        context, syncResults: syncResults, importRoughcut: deps.importRoughcut)
    }
    return try await runner.run(context, importRoughcut: deps.importRoughcut)
  }

  /// 쓰기 가능성 증명 — 상한 있는 임시 생성/제거로, 사용자 산물은 건드리지 않는다.
  /// 아직 없는 디렉터리는 **가장 가까운 존재 조상**에서 증명한다: 그 조상 안에
  /// 만들 수 있다는 것이 곧 그 자리를 만들 수 있다는 증거다.
  private func requireWritable(_ directory: URL, fileManager: FileManager) throws {
    var anchor = directory
    while !fileManager.fileExists(atPath: anchor.path), anchor.path != "/" {
      anchor = anchor.deletingLastPathComponent()
    }
    guard fileManager.fileExists(atPath: anchor.path) else {
      throw EnginePreflightError.workspaceNotWritable(directory.path)
    }
    let probe = anchor.appendingPathComponent(".vlogsrt-write-probe-\(UUID().uuidString)")
    do {
      try Data().write(to: probe)
      try fileManager.removeItem(at: probe)
    } catch {
      throw EnginePreflightError.workspaceNotWritable(directory.path)
    }
  }

  /// 결과 자리 — 이미 있는 읽기 전용 파일·디렉터리는 교정 글로 닫는다.
  private func requireWritableDestination(_ destination: URL, fileManager: FileManager) throws {
    let values = try? destination.resourceValues(forKeys: [.isDirectoryKey, .isWritableKey])
    if values?.isDirectory == true {
      throw EnginePreflightError.destinationNotWritable(destination.path)
    }
    if fileManager.fileExists(atPath: destination.path), values?.isWritable != true {
      throw EnginePreflightError.destinationNotWritable(destination.path)
    }
  }
}

/// 실제 배선 — 앱의 main.swift가 쓰는 조합. 저장소·scope·실행기는 실물이다.
extension EngineRequestCoordinator.Dependencies {

  /// 실제 배선 상수 — 개발 작업폴더 북마크의 저장 키(main.swift와 같은 값).
  public static let developmentWorkspaceKey = "developmentEngineWorkspace"

  public func attachingSessionStart(
    _ hook: @escaping @MainActor (EngineSessionContext) -> Void
  ) -> Self {
    Self(
      peekRoots: peekRoots,
      settleRoots: settleRoots,
      peekDevelopmentWorkspace: peekDevelopmentWorkspace,
      settleDevelopmentWorkspace: settleDevelopmentWorkspace,
      loadCredentials: loadCredentials,
      startScope: startScope,
      stopScope: stopScope,
      readiness: readiness,
      stageDevelopmentJob: stageDevelopmentJob,
      executor: executor,
      syncResults: syncResults,
      importRoughcut: importRoughcut,
      onSessionStart: hook)
  }

  public static func live(
    bookmarkAccess: any BookmarkAccess,
    loadCredentials: @escaping @Sendable () -> EngineRoleSnapshot,
    executor: any EngineExecuting,
    importRoughcut: (@Sendable (URL) async throws -> Void)? = nil,
    startScope: @escaping @Sendable (URL) -> Bool = {
      $0.startAccessingSecurityScopedResource()
    },
    stopScope: @escaping @Sendable (URL) -> Void = {
      $0.stopAccessingSecurityScopedResource()
    },
    readiness: (@Sendable (EngineReadinessRequest) async throws -> EngineReadinessReport)? = nil
  ) -> Self {
    Self(
      peekRoots: { try bookmarkAccess.peekStoredBookmarks(prefix: "media-root.") },
      settleRoots: { peeked in try peeked.map { try bookmarkAccess.settle($0) } },
      peekDevelopmentWorkspace: { try bookmarkAccess.peek(key: Self.developmentWorkspaceKey) },
      settleDevelopmentWorkspace: { peeked in
        guard let peeked else { return nil }
        return try bookmarkAccess.settle(peeked)
      },
      loadCredentials: loadCredentials,
      // 기본은 실물 scope다 — 시험은 같은 조립에 기록 가짜를 넣는다(성공·실패).
      startScope: startScope,
      stopScope: stopScope,
      readiness: { request in
        if let readiness { return try await readiness(request) }
        return try await EngineReadinessProber.probe(request)
      },
      stageDevelopmentJob: { jobID, appGroupJob, externalRoot in
        try DevelopmentJobStager.stage(
          jobID: jobID, appGroupJob: appGroupJob, externalRoot: externalRoot)
      },
      executor: executor,
      syncResults: nil,
      importRoughcut: importRoughcut,
      onSessionStart: nil)
  }
}
