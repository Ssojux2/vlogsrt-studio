import Foundation

// ─────────────────────────────────────────────────────────────
//  STAB-S4B — 자막 job의 앱→엔진 조정자
//
//  분석 조정자(`EngineRequestCoordinator`)와 같은 실행 기반(EngineRuntime
//  계획·중복 게이트·세션 러너·개발 스테이징)을 쓰되, 자막 job의 재료가 다르다:
//
//  - `input.fcpxml`·활성 선택·미디어 권한 문서가 없다 — 입력은 스테이징된
//    `final.wav`·`final.fcpxml`·`caption-origin.json`이다(S4A).
//  - 추정(`caption-estimate`)은 자격 증명이 전혀 필요 없다 — 키가 없어도 돈다.
//  - 실행(`caption-run`)은 STT 역할 하나만 요구하고 승인 문서
//    `caption-authorization.jsonl`가 스테이징돼 있어야 한다(엔진이 다시 잔다).
//  - 선택 드리프트 게이트가 없다 — 자막 job은 FCP 활성 프로젝트에 묶이지
//    않는다(세션의 registry가 nil).
// ─────────────────────────────────────────────────────────────

@MainActor
public final class CaptionJobCoordinator {
  /// 주입 경계 — 시험은 workspace·자격 증명·readiness·실행기를 갈아끼운다.
  public struct Dependencies: Sendable {
    public let peekDevelopmentWorkspace: @Sendable () throws ->
      SecurityScopedBookmarkStore.PeekedBookmark?
    public let settleDevelopmentWorkspace: @Sendable (
      SecurityScopedBookmarkStore.PeekedBookmark?
    ) throws -> URL?
    public let loadCredentials: @Sendable () -> EngineRoleSnapshot
    public let startScope: @Sendable (URL) -> Bool
    public let stopScope: @Sendable (URL) -> Void
    public let stageCaptionJob: @Sendable (
      _ jobID: String, _ appGroupJob: URL, _ externalRoot: URL
    ) throws -> URL
    public let executor: any EngineExecuting
    public let syncResults: (@Sendable (URL, URL, Bool) throws -> Void)?
    public let readiness: (@Sendable (EngineReadinessRequest) async throws -> EngineReadinessReport)?
    public let onSessionStart: (@MainActor (EngineSessionContext) -> Void)?

    public init(
      peekDevelopmentWorkspace: @escaping @Sendable () throws ->
        SecurityScopedBookmarkStore.PeekedBookmark?,
      settleDevelopmentWorkspace: @escaping @Sendable (
        SecurityScopedBookmarkStore.PeekedBookmark?
      ) throws -> URL?,
      loadCredentials: @escaping @Sendable () -> EngineRoleSnapshot,
      startScope: @escaping @Sendable (URL) -> Bool,
      stopScope: @escaping @Sendable (URL) -> Void,
      stageCaptionJob: @escaping @Sendable (
        _ jobID: String, _ appGroupJob: URL, _ externalRoot: URL
      ) throws -> URL,
      executor: any EngineExecuting,
      syncResults: (@Sendable (URL, URL, Bool) throws -> Void)? = nil,
      readiness: (@Sendable (EngineReadinessRequest) async throws -> EngineReadinessReport)? = nil,
      onSessionStart: (@MainActor (EngineSessionContext) -> Void)? = nil
    ) {
      self.peekDevelopmentWorkspace = peekDevelopmentWorkspace
      self.settleDevelopmentWorkspace = settleDevelopmentWorkspace
      self.loadCredentials = loadCredentials
      self.startScope = startScope
      self.stopScope = stopScope
      self.stageCaptionJob = stageCaptionJob
      self.executor = executor
      self.syncResults = syncResults
      self.readiness = readiness
      self.onSessionStart = onSessionStart
    }

    /// 세션 시작 후크만 갈아끼운 복사본 — 앱의 job 판 배선이 쓴다.
    public func attachingSessionStart(
      _ hook: @escaping @MainActor (EngineSessionContext) -> Void
    ) -> Dependencies {
      Dependencies(
        peekDevelopmentWorkspace: peekDevelopmentWorkspace,
        settleDevelopmentWorkspace: settleDevelopmentWorkspace,
        loadCredentials: loadCredentials,
        startScope: startScope,
        stopScope: stopScope,
        stageCaptionJob: stageCaptionJob,
        executor: executor,
        syncResults: syncResults,
        readiness: readiness,
        onSessionStart: hook)
    }

    /// 실제 배선 — 앱 프로세스가 부르는 모습 그대로(시험은 이것을 본뜬다).
    /// 개발 작업폴더 북마크 키는 분석 조정자와 같은 것이다.
    public static func live(executor: any EngineExecuting) -> Dependencies {
      let bookmarkStore = try? SecurityScopedBookmarkStore()
      return Dependencies(
        peekDevelopmentWorkspace: {
          try bookmarkStore?.peek(
            key: EngineRequestCoordinator.Dependencies.developmentWorkspaceKey)
        },
        settleDevelopmentWorkspace: { peeked in
          guard let peeked else { return nil }
          return try bookmarkStore?.settle(peeked)
        },
        loadCredentials: {
          EngineRoleCredentialStore(storage: KeychainStore()).loadSnapshot()
        },
        startScope: { url in url.startAccessingSecurityScopedResource() },
        stopScope: { url in url.stopAccessingSecurityScopedResource() },
        stageCaptionJob: { jobID, appGroupJob, externalRoot in
          try DevelopmentJobStager.stageCaption(
            jobID: jobID, appGroupJob: appGroupJob, externalRoot: externalRoot)
        },
        executor: executor,
        syncResults: { from, to, diagnosticsOnly in
          try DevelopmentJobStager.syncResults(
            from: from, to: to, diagnosticsOnly: diagnosticsOnly)
        },
        readiness: { request in try await EngineReadinessProber.probe(request) },
        onSessionStart: nil
      )
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
    fileManager: FileManager = .default
  ) async throws -> EngineSessionOutcome {
    try validateStagedInputs(operation: operation, job: job, fileManager: fileManager)
    if CancellationStore.isCancelled(jobURL: job.directoryURL, fileManager: fileManager) {
      return .cancelledBeforeRun
    }
    let credentials = try loadAndGateCredentials(operation: operation)
    let mode = EngineLaunchMode.resolve(bundleURL: engineRoot, fileManager: fileManager)
    if mode == .development, configuration != .debug {
      throw EnginePreflightError.engineUnavailable(
        "Release 빌드는 번들 엔진으로만 실행한다 — Resources/Engine이 없다(개발 폴백 없음)")
    }
    let peekedWorkspace = mode == .development ? try deps.peekDevelopmentWorkspace() : nil
    var scopes = CaptionScopeBox(start: deps.startScope, stop: deps.stopScope)
    defer { scopes.releaseAll() }
    if let peekedWorkspace { scopes.acquire(peekedWorkspace.url) }
    let devWorkspace = try deps.settleDevelopmentWorkspace(peekedWorkspace)
    try verifyWritableSurfaces(
      operation: operation, job: job, devWorkspace: devWorkspace, fileManager: fileManager)
    if operation == .captionRun, let readiness = deps.readiness {
      let request = EngineReadinessRequest(
        mode: mode,
        jobID: job.id,
        appGroupJob: job.directoryURL,
        applicationScriptsDirectory: applicationScriptsDirectory,
        engineRoot: engineRoot,
        credentials: credentials)
      _ = try await readiness(request)
    }
    let externalJob = try stageDevelopmentJob(job: job, devWorkspace: devWorkspace)
    let plan = try EngineRuntime.resolvePlanValidated(
      configuration: configuration,
      operation: operation,
      jobID: job.id,
      appGroupJob: job.directoryURL,
      engineRoot: engineRoot,
      applicationScriptsDirectory: applicationScriptsDirectory,
      developmentExternalJob: externalJob,
      credentials: credentials,
      fileManager: fileManager)
    guard plan.isExecutable else {
      if case .unavailable(let reason) = plan {
        throw EnginePreflightError.engineUnavailable(reason)
      }
      throw EnginePreflightError.engineUnavailable("실행 가능한 엔진이 없다")
    }
    let runner = EngineSessionRunner(executor: deps.executor)
    let context = EngineSessionContext(
      operation: operation,
      jobID: job.id,
      appGroupJob: job.directoryURL,
      externalJob: externalJob,
      plan: plan,
      selectionRevision: "",
      registry: nil,
      now: { ISO8601DateFormatter().string(from: Date()) })
    deps.onSessionStart?(context)
    return try await runner.run(
      context,
      syncResults: deps.syncResults ?? { from, to, diagnosticsOnly in
        try DevelopmentJobStager.syncResults(from: from, to: to, diagnosticsOnly: diagnosticsOnly)
      },
      importRoughcut: nil)
  }

  // ─── 단계들 ────────────────────────────────────────────────

  /// 스테이징된 자막 재료 — 이 job에는 input.fcpxml이 없다(S4A).
  private func validateStagedInputs(
    operation: DevelopmentEngineOperation, job: RoundTripJob, fileManager: FileManager
  ) throws {
    let required = ["final.wav", "final.fcpxml", "caption-origin.json"]
    for name in required {
      let url = job.directoryURL.appendingPathComponent(name)
      guard fileManager.fileExists(atPath: url.path) else {
        throw EnginePreflightError.missingCaptionInput(name)
      }
    }
    if operation == .captionRun {
      let authorization = job.directoryURL.appendingPathComponent("caption-authorization.jsonl")
      guard fileManager.fileExists(atPath: authorization.path) else {
        throw EnginePreflightError.missingCaptionAuthorization
      }
    }
  }

  /// 추정은 무자격도 돈다 — 실행만 STT 역할 하나를 요구한다(비전은 자막에 없다).
  private func loadAndGateCredentials(
    operation: DevelopmentEngineOperation
  ) throws -> EngineRoleCredentials {
    let snapshot = deps.loadCredentials()
    guard case .ready(let credentials) = snapshot else {
      throw EnginePreflightError.credentialStorageUnavailable
    }
    guard operation == .captionRun else { return credentials }
    if let blocker = EngineRoleCredentialStore.launchBlocker(for: credentials) {
      throw EnginePreflightError.visionEndpointInvalid(blocker)
    }
    let stt = credentials.sttKey.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !stt.isEmpty else {
      throw EnginePreflightError.captionSTTKeyMissing
    }
    return credentials
  }

  /// 쓰기 증명 — job 자리와 workspace, 실행이면 산물 폴더(captions/)까지.
  private func verifyWritableSurfaces(
    operation: DevelopmentEngineOperation,
    job: RoundTripJob,
    devWorkspace: URL?,
    fileManager: FileManager
  ) throws {
    try requireWritableDirectory(job.directoryURL, fileManager: fileManager)
    try requireWritableDirectory(
      job.directoryURL.appendingPathComponent("workspace", isDirectory: true),
      fileManager: fileManager)
    if operation == .captionRun {
      try requireWritableDirectory(
        job.directoryURL.appendingPathComponent(
          DevelopmentJobStager.captionDeliveryDirectoryName, isDirectory: true),
        fileManager: fileManager)
    }
    if let devWorkspace {
      try requireWritableDirectory(devWorkspace, fileManager: fileManager)
    }
  }

  private func stageDevelopmentJob(job: RoundTripJob, devWorkspace: URL?) throws -> URL? {
    guard let devWorkspace else { return nil }
    return try deps.stageCaptionJob(job.id, job.directoryURL, devWorkspace)
  }
}

/// 개발 작업폴더 하나의 scope — AcquiredScopes의 자막 job 축소판.
struct CaptionScopeBox: Sendable {
  private let start: @Sendable (URL) -> Bool
  private let stop: @Sendable (URL) -> Void
  private var held: [URL] = []

  init(start: @escaping @Sendable (URL) -> Bool, stop: @escaping @Sendable (URL) -> Void) {
    self.start = start
    self.stop = stop
  }

  mutating func acquire(_ url: URL) {
    if start(url) { held.append(url) }
  }

  mutating func releaseAll() {
    for url in held { stop(url) }
    held.removeAll()
  }
}

/// 쓰기 증명 — 없는 자리는 가장 가까운 존재 조상에서 잰다(분석 조정자와 같은 선).
func requireWritableDirectory(_ directory: URL, fileManager: FileManager) throws {
  var probeTarget = directory
  var missing: [URL] = []
  while !fileManager.fileExists(atPath: probeTarget.path) {
    missing.append(probeTarget)
    let parent = probeTarget.deletingLastPathComponent()
    if parent.path == probeTarget.path { break }
    probeTarget = parent
  }
  guard fileManager.isWritableFile(atPath: probeTarget.path) else {
    throw EnginePreflightError.engineUnavailable(
      "쓸 수 없는 작업 자리다: \(directory.path)")
  }
  for url in missing.reversed() {
    try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
  }
}
