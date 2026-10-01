import CryptoKit
import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-2 마지막 단위 — 준비 조정자: 순서 · scope 수명 · 관문
// ─────────────────────────────────────────────────────────────

/// scope 시작·해제의 녹화 — 주입된 조작만으로 수명을 관찰한다(실제 북마크·
/// Keychain은 건드리지 않는다).
final class ScopeLog: @unchecked Sendable {
  private let lock = NSLock()
  private var _events: [String] = []
  var failCanonicalPaths: Set<String> = []

  var events: [String] {
    lock.lock()
    defer { lock.unlock() }
    return _events
  }

  func record(_ event: String) {
    lock.lock()
    _events.append(event)
    lock.unlock()
  }
}

/// 받은 계획을 통째로 기록하는 실행기 — 호출 인자(소비자가 보는 것)를 잰다.
/// 액터다: 동기 잠금을 async 문맥에 두지 않고, 기록 자체가 직렬화된다.
actor PlanCapture: EngineExecuting {
  private var _plans: [EngineLaunchPlan] = []
  let status: Int32

  init(status: Int32 = 0) {
    self.status = status
  }

  var plans: [EngineLaunchPlan] { _plans }

  nonisolated func run(_ plan: EngineLaunchPlan) async throws -> Int32 {
    await append(plan)
    return status
  }

  private func append(_ plan: EngineLaunchPlan) {
    _plans.append(plan)
  }
}

/// 성공 보고 — 도구가 둘 다 있는 기본 모양. `tools`가 **있으면** 그 값 그대로다:
/// 명시적 (nil, nil)은 '도구 없음'이지 기본 경로로 바꿔 치울 값이 아니다.
private func _healthyReport(
  tools: (String?, String?)? = nil, openAIKeyPresent: Bool = true
) -> EngineReadinessReport {
  let pair = tools ?? ("/synthetic/bin/ffmpeg", "/synthetic/bin/ffprobe")
  return EngineReadinessReport(
    engine: "vlogsrt-engine", readinessVersion: 1,
    pythonExecutable: "/synthetic/python", pythonVersion: "3.11.9",
    ffmpeg: pair.0, ffprobe: pair.1, openAIKeyPresent: openAIKeyPresent)
}

/// 조정자가 쓸 주입 경계의 기본 모음 — 시험마다 필요한 것만 갈아끼운다.
/// 북마크는 두 단계(peek·settle)로 주입하고, settle은 녹화해 순서를 잰다.
@MainActor
private func _readinessDeps(
  job: RoundTripJob,
  mediaRoot: URL,
  developmentWorkspace: URL? = nil,
  readiness: @escaping @Sendable (EngineReadinessRequest) async throws -> EngineReadinessReport =
    { _ in _healthyReport() },
  executor: any EngineExecuting = PlanCapture(),
  scopeLog: ScopeLog
) -> EngineRequestCoordinator.Dependencies {
  EngineRequestCoordinator.Dependencies(
    peekRoots: {
      [
        SecurityScopedBookmarkStore.PeekedBookmark(
          key: "media-root.synthetic", url: mediaRoot, isStale: false)
      ]
    },
    settleRoots: { peeked in
      scopeLog.record("settle-roots")
      return peeked.map(\.url)
    },
    peekDevelopmentWorkspace: {
      developmentWorkspace.map {
        SecurityScopedBookmarkStore.PeekedBookmark(
          key: "developmentEngineWorkspace", url: $0, isStale: false)
      }
    },
    settleDevelopmentWorkspace: { peeked in
      if peeked != nil { scopeLog.record("settle-workspace") }
      return peeked?.url
    },
    startScope: { url in
      let canonical = url.resolvingSymlinksInPath().standardizedFileURL.path
      scopeLog.record("start:\(canonical)")
      return !scopeLog.failCanonicalPaths.contains(canonical)
    },
    stopScope: { url in
      scopeLog.record("stop:\(url.resolvingSymlinksInPath().standardizedFileURL.path)")
    },
    readiness: readiness,
    stageDevelopmentJob: { jobID, appGroupJob, externalRoot in
      try DevelopmentJobStager.stage(
        jobID: jobID, appGroupJob: appGroupJob, externalRoot: externalRoot)
    },
    executor: executor)
}

/// 조정자 시험의 재료 — 임시 뿌리에 job·미디어 루트·(선택) 실행길 재료를 만든다.
@MainActor
private func _readinessFixture(
  configuration: EngineBuildConfiguration,
  bundlePresent: Bool = false,
  developmentWrapperPresent: Bool = true
) throws -> (
  root: URL, job: RoundTripJob, mediaRoot: URL, scripts: URL, engineRoot: URL,
  externalRoot: URL
) {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let mediaRoot = root.appendingPathComponent("media", isDirectory: true)
  let clip = mediaRoot.appendingPathComponent("clip.mov")
  try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
  try Data("synthetic-readiness-media".utf8).write(to: clip)
  let workspace = AppGroupWorkspace(rootURL: root)
  let job = try RoundTripJob(workspace: workspace, id: "job-42")
  try FileManager.default.createDirectory(
    at: job.directoryURL, withIntermediateDirectories: true)
  let fcpxml = """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE fcpxml>
    <fcpxml version="1.14">
      <resources>
        <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
        <asset id="r2" name="clip.mov" start="0s" duration="600s" hasAudio="1" format="r1">
          <media-rep kind="original-media" src="\(clip.absoluteString)"/>
        </asset>
      </resources>
      <library>
        <event name="이벤트">
          <project name="준비" uid="PROJECT-READINESS">
            <sequence format="r1" tcStart="0s" duration="600s">
              <spine>
                <asset-clip ref="r2" name="클립0" offset="0s" start="0s" duration="12s"/>
              </spine>
            </sequence>
          </project>
        </event>
      </library>
    </fcpxml>
    """
  try job.writeInput(Data(fcpxml.utf8))
  try job.writeProjectSelection(
    ActiveProjectSelection(
      projectUID: "P-1",
      projectName: "Project",
      timelineStart: RationalTime(value: 0, timescale: 1),
      duration: RationalTime(value: 10, timescale: 1),
      frameDuration: RationalTime(value: 1, timescale: 25)
    ))

  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  if developmentWrapperPresent {
    let wrapper = scripts.appendingPathComponent(DevelopmentEngineInvocation.scriptName)
    try Data("#!/bin/sh\n".utf8).write(to: wrapper)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
  }
  let engineRoot = root.appendingPathComponent("Engine", isDirectory: true)
  if bundlePresent {
    let executable = engineRoot
      .appendingPathComponent("Contents", isDirectory: true)
      .appendingPathComponent("MacOS", isDirectory: true)
      .appendingPathComponent(DevelopmentEngineInvocation.scriptName)
    try FileManager.default.createDirectory(
      at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data("#!/bin/sh\n".utf8).write(to: executable)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
  }
  let externalRoot = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: externalRoot, withIntermediateDirectories: true)
  return (root, job, mediaRoot, scripts, engineRoot, externalRoot)
}

@MainActor
private func _run(
  _ deps: EngineRequestCoordinator.Dependencies,
  operation: DevelopmentEngineOperation = .analyze,
  job: RoundTripJob,
  configuration: EngineBuildConfiguration = .debug,
  engineRoot: URL?,
  scripts: URL?
) async throws -> EngineSessionOutcome {
  let (registry, _) = try _testRegistry()
  return try await EngineRequestCoordinator(dependencies: deps).run(
    operation: operation,
    job: job,
    configuration: configuration,
    engineRoot: engineRoot,
    applicationScriptsDirectory: scripts,
    registry: registry)
}

/// 개발 실행길의 준비 순서 — scope가 권한 검사·readiness·스테이징보다 **먼저**,
/// 해제는 실행·동기화가 끝난 뒤에만 온다. 최종 호출의 argv는 **스테이징된 job**을
/// 가리킨다(뿌리가 아니라) — 소비자가 보는 인자로 잰다.
@Test @MainActor func developmentReadinessAcquiresScopesBeforeProtectedWorkAndHoldsThroughRun()
  async throws
{
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let scopeLog = ScopeLog()
  let executor = PlanCapture()
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    executor: executor,
    scopeLog: scopeLog)
  let outcome = try await _run(
    deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  #expect(outcome == .completed)

  let started = scopeLog.events.filter { $0.hasPrefix("start:") }
  #expect(started.count == 2)  // 미디어 루트 + 개발 작업폴더
  let mediaCanonical = fixture.mediaRoot.resolvingSymlinksInPath().standardizedFileURL.path
  let externalCanonical = fixture.externalRoot.resolvingSymlinksInPath().standardizedFileURL.path
  #expect(started.contains("start:\(mediaCanonical)"))
  #expect(started.contains("start:\(externalCanonical)"))
  // 해제는 전부 시작 뒤에 — 실행이 scope 안에서 끝났다는 증거다.
  let firstStop = try #require(scopeLog.events.firstIndex { $0.hasPrefix("stop:") })
  let lastStart = try #require(scopeLog.events.lastIndex { $0.hasPrefix("start:") })
  #expect(firstStop > lastStart)
  #expect(scopeLog.events.filter { $0.hasPrefix("stop:") }.count == 2)
  // 북마크 정착(검증·갱신)도 scope 시작 뒤에만 — 보호받는 읽기가 scope 없이
  // 일어나지 않는다는 순서의 증거다.
  let firstSettle = try #require(scopeLog.events.firstIndex { $0.hasPrefix("settle") })
  #expect(firstSettle > lastStart)

  // 최종 호출 — 개발길의 자리 인자는 스테이징된 job 경로다.
  let plan = try #require(await executor.plans.first)
  guard case .development(let invocation) = plan else {
    Issue.record("개발 계획이어야 한다: \(plan)")
    return
  }
  let staged = fixture.externalRoot.appendingPathComponent("Jobs").appendingPathComponent("job-42")
  #expect(invocation.arguments[0] == "analyze")
  #expect(invocation.arguments[1] == "job-42")
  #expect(invocation.arguments[2] == staged.standardizedFileURL.path)
  // 상속 호출(패널 계약) — 마커도 봉투도 없다. 키는 엔진 경계의 .env이 푼다.
  #expect(!invocation.arguments.contains(DevelopmentEngineInvocation.carrierMarkerArgument))
  #expect(invocation.credentialEnvelope == nil)
  // 권한 문서도 잡 디렉터리에 남는다.
  #expect(
    FileManager.default.fileExists(
      atPath: fixture.job.directoryURL.appendingPathComponent(MediaGrantDocument.fileName).path))
}

/// 같은 경로가 미디어 루트이자 개발 작업폴더면 scope는 하나다 — 정준 정체로
/// 중복을 묶는다. 해제도 한 번이다.
@Test @MainActor func devWorkspaceEqualToAMediaRootStartsASingleScope() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let scopeLog = ScopeLog()
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.mediaRoot,  // 같은 폴더
    executor: PlanCapture(),
    scopeLog: scopeLog)
  let outcome = try await _run(
    deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  #expect(outcome == .completed)
  #expect(scopeLog.events.filter { $0.hasPrefix("start:") }.count == 1)
  #expect(scopeLog.events.filter { $0.hasPrefix("stop:") }.count == 1)
}

/// 북마크 두 단계 경계의 순서 증명 — **실제 live 조립**을 지나서.
///
/// 가짜는 북마크 접근 원시(BookmarkAccess)와 scope 관찰자뿐이다: 실제
/// `Dependencies.live` 배선이 조정자를 움직이고, settle 기록(가짜 접근)과
/// start 기록(관찰자)의 순서로 '명시적 검증·갱신은 scope 뒤'를 잰다.
/// UserDefaults·bookmarkData·resolvingBookmarkData는 부르지 않는다.
private final class FakeBookmarkAccess: BookmarkAccess, @unchecked Sendable {
  /// 동시성 안전 근거: 기록은 내부 잠금(ScopeLog)이 지킨다.
  private let scopeLog: ScopeLog
  private let roots: [SecurityScopedBookmarkStore.PeekedBookmark]
  private let workspace: SecurityScopedBookmarkStore.PeekedBookmark?

  init(
    scopeLog: ScopeLog,
    roots: [SecurityScopedBookmarkStore.PeekedBookmark],
    workspace: SecurityScopedBookmarkStore.PeekedBookmark?
  ) {
    self.scopeLog = scopeLog
    self.roots = roots
    self.workspace = workspace
  }

  func peekStoredBookmarks(
    prefix: String
  ) throws -> [SecurityScopedBookmarkStore.PeekedBookmark] {
    roots
  }

  func peek(key: String) throws -> SecurityScopedBookmarkStore.PeekedBookmark? {
    workspace?.key == key ? workspace : nil
  }

  func settle(_ bookmark: SecurityScopedBookmarkStore.PeekedBookmark) throws -> URL {
    scopeLog.record("settle:\(bookmark.key)")
    return bookmark.url
  }
}

@Test @MainActor func bookmarkBoundarySettlesOnlyUnderAcquiredScopesThroughLiveAssembly()
  async throws
{
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let (registry, _) = try _testRegistry()
  let scopeLog = ScopeLog()
  let fake = FakeBookmarkAccess(
    scopeLog: scopeLog,
    roots: [
      SecurityScopedBookmarkStore.PeekedBookmark(
        key: "media-root.synthetic", url: fixture.mediaRoot, isStale: false)
    ],
    workspace: SecurityScopedBookmarkStore.PeekedBookmark(
      key: "developmentEngineWorkspace", url: fixture.externalRoot, isStale: false))

  let dependencies = EngineRequestCoordinator.Dependencies.live(
    bookmarkAccess: fake,

    executor: PlanCapture(),
    startScope: { url in
      scopeLog.record("start:\(url.resolvingSymlinksInPath().path)")
      return true
    },
    stopScope: { url in
      scopeLog.record("stop:\(url.resolvingSymlinksInPath().path)")
    },
    readiness: { _ in _healthyReport() })

  let outcome = try await EngineRequestCoordinator(dependencies: dependencies).run(
    operation: .analyze,
    job: fixture.job,
    configuration: .debug,
    engineRoot: fixture.engineRoot,
    applicationScriptsDirectory: fixture.scripts,
    registry: registry)
  #expect(outcome == .completed)
  let firstSettle = try #require(scopeLog.events.firstIndex { $0.hasPrefix("settle") })
  let lastStart = try #require(scopeLog.events.lastIndex { $0.hasPrefix("start:") })
  #expect(firstSettle > lastStart)
  #expect(scopeLog.events.filter { $0.hasPrefix("settle") }.count == 2)
}

/// 같은 live 조립에 **실패하는** scope 시작 원시를 넣는다 — 부분 확보는 이미
/// 걸은 scope를 정확히 한 번 풀고 세션은 열리지 않는다(실제 실패 동작 그대로).
@Test @MainActor func liveAssemblyReleasesScopesExactlyOnceWhenStartFails() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let (registry, _) = try _testRegistry()
  let scopeLog = ScopeLog()
  let failingExternal = fixture.externalRoot.resolvingSymlinksInPath().standardizedFileURL.path
  let fake = FakeBookmarkAccess(
    scopeLog: scopeLog,
    roots: [
      SecurityScopedBookmarkStore.PeekedBookmark(
        key: "media-root.synthetic", url: fixture.mediaRoot, isStale: false)
    ],
    workspace: SecurityScopedBookmarkStore.PeekedBookmark(
      key: "developmentEngineWorkspace", url: fixture.externalRoot, isStale: false))
  let executor = PlanCapture()
  let dependencies = EngineRequestCoordinator.Dependencies.live(
    bookmarkAccess: fake,

    executor: executor,
    startScope: { url in
      let canonical = url.resolvingSymlinksInPath().standardizedFileURL.path
      scopeLog.record("start:\(canonical)")
      return canonical != failingExternal
    },
    stopScope: { url in
      scopeLog.record("stop:\(url.resolvingSymlinksInPath().standardizedFileURL.path)")
    },
    readiness: { _ in
      Issue.record("scope 실패 뒤 readiness는 없다")
      throw NSError(domain: "readiness", code: 9)
    })

  do {
    _ = try await EngineRequestCoordinator(dependencies: dependencies).run(
      operation: .analyze,
      job: fixture.job,
      configuration: .debug,
      engineRoot: fixture.engineRoot,
      applicationScriptsDirectory: fixture.scripts,
      registry: registry)
    Issue.record("scope 시작 실패가 지나갔다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("scope") || (error.errorDescription ?? "").contains("권한"))
  }
  #expect(await executor.plans.isEmpty)
  let mediaCanonical = fixture.mediaRoot.resolvingSymlinksInPath().standardizedFileURL.path
  #expect(scopeLog.events.filter { $0 == "stop:\(mediaCanonical)" }.count == 1)
}



/// 번들이 선택되면 개발 작업폴더는 손대지 않는다 — 북마크 해석·scope·스테이징
/// 전부. 최종 호출은 번들 환경 한 통이다.
@Test @MainActor func bundledSelectionNeverAcquiresOrStagesTheDevelopmentWorkspace() async throws {
  let fixture = try _readinessFixture(configuration: .debug, bundlePresent: true)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  // 번들 길의 도구 요구 — bin에 실제 도구를 둔다(분석은 이걸 요구한다).
  let bin = fixture.engineRoot.appendingPathComponent("bin", isDirectory: true)
  try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
  for tool in ["ffmpeg", "ffprobe"] {
    let path = bin.appendingPathComponent(tool)
    try Data("#!/bin/sh\n".utf8).write(to: path)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path.path)
  }
  let scopeLog = ScopeLog()
  let executor = PlanCapture()
  let deps = EngineRequestCoordinator.Dependencies(
    peekRoots: {
      [
        SecurityScopedBookmarkStore.PeekedBookmark(
          key: "media-root.synthetic", url: fixture.mediaRoot, isStale: false)
      ]
    },
    settleRoots: { peeked in peeked.map(\.url) },
    peekDevelopmentWorkspace: {
      scopeLog.record("peek-workspace")
      return SecurityScopedBookmarkStore.PeekedBookmark(
        key: "developmentEngineWorkspace", url: fixture.externalRoot, isStale: false)
    },
    settleDevelopmentWorkspace: { peeked in peeked?.url },

    startScope: { url in
      scopeLog.record("start:\(url.path)")
      return true
    },
    stopScope: { url in scopeLog.record("stop:\(url.path)") },
    readiness: { request in
      #expect(request.mode == .bundled)
      // 번들 준비는 bin 자리를 말하는 보고로 증명한다(경로 동일성).
      return _healthyReport(
        tools: (bin.appendingPathComponent("ffmpeg").path, bin.appendingPathComponent("ffprobe").path))
    },
    stageDevelopmentJob: { _, _, _ in
      Issue.record("번들 선택에서 개발 스테이징이 불렸다")
      throw NSError(domain: "readiness", code: 1)
    },
    executor: executor)
  let outcome = try await _run(
    deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  #expect(outcome == .completed)
  #expect(!scopeLog.events.contains("peek-workspace"))
  #expect(scopeLog.events.filter { $0.hasPrefix("start:") }.count == 1)  // 미디어 루트만
  #expect(
    !FileManager.default.fileExists(
      atPath: fixture.externalRoot.appendingPathComponent("Jobs").path))
  guard case .bundled(let invocation) = try #require(await executor.plans.first) else {
    Issue.record("번들 계획이어야 한다")
    return
  }
  // 상속 호출(패널 계약) — 운반자 환경은 아예 없다. PATH만 간다.
  #expect(invocation.environment[BundledEngineInvocation.apiKeyEnvironmentName] == nil)
  #expect(invocation.environment[BundledEngineInvocation.roleCarrierEnvironmentName] == nil)
}

/// scope 시작이 도중에 실패하면 이미 걸은 scope만 해제하고 실행기·readiness는
/// 아예 부르지 않는다 — 부분 권한으로 계속하지 않고, 해제도 두 번이 아니다.
@Test @MainActor func partialScopeFailureReleasesStartedScopesExactlyOnceAndRunsNothing()
  async throws
{
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let scopeLog = ScopeLog()
  scopeLog.failCanonicalPaths = [
    fixture.externalRoot.resolvingSymlinksInPath().standardizedFileURL.path
  ]
  let readinessCalls = CountBox()
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    readiness: { _ in
      readinessCalls.value += 1
      throw NSError(domain: "readiness", code: 9)
    },
    executor: PlanCapture(),
    scopeLog: scopeLog)
  await #expect(throws: EnginePreflightError.self) {
    _ = try await _run(
      deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  }
  #expect(readinessCalls.value == 0)
  let mediaCanonical = fixture.mediaRoot.resolvingSymlinksInPath().standardizedFileURL.path
  #expect(scopeLog.events.filter { $0 == "stop:\(mediaCanonical)" }.count == 1)
  #expect(scopeLog.events.filter { $0.hasPrefix("stop:") }.count == 1)
}

/// 어떤 끝에서든 scope는 전부 풀린다 — 정상 완료·실행 실패·던지는 오류.
@Test @MainActor func scopesAreReleasedOnEveryOutcome() async throws {
  for terminal in ["completed", "failed", "thrown"] {
    let fixture = try _readinessFixture(configuration: .debug)
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    let scopeLog = ScopeLog()
    let executor: any EngineExecuting =
      terminal == "completed" ? PlanCapture(status: 0) : PlanCapture(status: 4)
    let deps = _readinessDeps(
      job: fixture.job,
      mediaRoot: fixture.mediaRoot,
      developmentWorkspace: fixture.externalRoot,
      executor: executor,
      scopeLog: scopeLog)
    do {
      let outcome = try await _run(
        deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
      if terminal == "failed" { #expect(outcome == .engineFailed(4)) }
    } catch {
      #expect(terminal == "thrown")
    }
    #expect(scopeLog.events.filter { $0.hasPrefix("stop:") }.count == 2)
  }
}

/// 개발 래퍼가 없거나 실행 가능하지 않으면 실행 가능한 실패 글로 닫는다 —
/// readiness 자식도 실행기도 부르지 않고 scope도 걸지 않는다.
@Test @MainActor func missingOrNonExecutableDevelopmentWrapperIsActionableFailure() async throws {
  for variant in ["missing", "non-executable"] {
    let fixture = try _readinessFixture(
      configuration: .debug, developmentWrapperPresent: false)
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    if variant == "non-executable" {
      let wrapper = fixture.scripts.appendingPathComponent(
        DevelopmentEngineInvocation.scriptName)
      try Data("#!/bin/sh\n".utf8).write(to: wrapper)  // 실행 권한 없음 — 644
    }
    let scopeLog = ScopeLog()
    let readinessCalls = CountBox()
    let deps = _readinessDeps(
      job: fixture.job,
      mediaRoot: fixture.mediaRoot,
      developmentWorkspace: fixture.externalRoot,
      readiness: { _ in
        readinessCalls.value += 1
        throw NSError(domain: "readiness", code: 9)
      },
      executor: PlanCapture(),
      scopeLog: scopeLog)
    do {
      _ = try await _run(
        deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
      Issue.record("래퍼가 없으면 실패해야 한다 (\(variant))")
    } catch let error as EnginePreflightError {
      let message = error.errorDescription ?? ""
      #expect(message.contains("개발"))
      #expect(message.contains("설치"))
    }
    #expect(readinessCalls.value == 0)
    #expect(scopeLog.events.isEmpty)
  }
}

/// Release에서 번들이 없으면 개발 폴백이 없다 — 정해진 글로 닫히고, 개발
/// 작업폴더 북마크도 scope도 손대지 않는다.
@Test @MainActor func releaseWithoutBundleNeverFallsBackToDevelopment() async throws {
  let fixture = try _readinessFixture(configuration: .release, bundlePresent: false)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let scopeLog = ScopeLog()
  let deps = EngineRequestCoordinator.Dependencies(
    peekRoots: {
      [
        SecurityScopedBookmarkStore.PeekedBookmark(
          key: "media-root.synthetic", url: fixture.mediaRoot, isStale: false)
      ]
    },
    settleRoots: { peeked in peeked.map(\.url) },
    peekDevelopmentWorkspace: {
      scopeLog.record("peek-workspace")
      return SecurityScopedBookmarkStore.PeekedBookmark(
        key: "developmentEngineWorkspace", url: fixture.externalRoot, isStale: false)
    },
    settleDevelopmentWorkspace: { peeked in peeked?.url },

    startScope: { _ in true },
    stopScope: { _ in },
    readiness: { _ in
      Issue.record("실행기가 없으면 readiness도 없다")
      throw NSError(domain: "readiness", code: 9)
    },
    stageDevelopmentJob: { _, _, _ in
      Issue.record("Release는 개발 스테이징을 하지 않는다")
      throw NSError(domain: "readiness", code: 1)
    },
    executor: PlanCapture())
  do {
    _ = try await _run(
      deps, job: fixture.job, configuration: .release, engineRoot: fixture.engineRoot,
      scripts: fixture.scripts)
    Issue.record("Release 무실행은 실패해야 한다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("Release"))
  }
  #expect(!scopeLog.events.contains("peek-workspace"))
  #expect(scopeLog.events.isEmpty)
}

/// 승인 파일이 스테이징된 analyze는 유료다(패널 계약) — 개발 .env의 키 유무가
/// readiness 보고서에 없으면 실행기·스테이징·scope는 아예 없다. 문면에 값은
/// 절대 없다(불리언만 오고 간다).
@Test @MainActor func paidAnalyzeIsBlockedWhenTheEngineReportsNoDevKey() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  try Data("{}".utf8).write(to: fixture.job.analysisAuthorizationURL)
  let scopeLog = ScopeLog()
  let readinessCalls = CountBox()
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    readiness: { _ in
      readinessCalls.value += 1
      return _healthyReport(openAIKeyPresent: false)
    },
    executor: PlanCapture(),
    scopeLog: scopeLog)
  do {
    _ = try await _run(
      deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("키 없는 보고서로 유료 요청이 지나갔다")
  } catch let error as EnginePreflightError {
    let message = error.errorDescription ?? ""
    #expect(message.contains("OpenAI"))
    #expect(!message.contains("synthetic"))
  }
  #expect(readinessCalls.value == 1)
  // 게이트는 readiness 뒤(보고서가 재료)지만 실행기 앞이다 — 엔진 호출은 없다.
  let engineCalls = await (deps.executor as? PlanCapture)?.plans.count ?? 0
  #expect(engineCalls == 0)
}

/// 키 유무 보고가 참이면 승인된 분석은 준비를 지나 실행기까지 간다.
@Test @MainActor func paidAnalyzeProceedsWhenTheEngineReportsADevKey() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  try Data("{}".utf8).write(to: fixture.job.analysisAuthorizationURL)
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    readiness: { _ in _healthyReport(openAIKeyPresent: true) },
    executor: PlanCapture(),
    scopeLog: ScopeLog())
  let outcome = try await _run(
    deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  #expect(outcome == .completed)
}

/// 승인 없는 analyze는 키 없이도 준비를 지난다 — 읽기 전용 readiness 자식이
/// 정확히 한 번, 그 뒤에 실행기가 돈다.
@Test @MainActor func keyFreeAnalyzeRunsTheReadOnlyReadinessChildThenTheExecutor() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let readinessCalls = CountBox()
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    readiness: { _ in
      readinessCalls.value += 1
      return _healthyReport()
    },
    executor: PlanCapture(),
    scopeLog: ScopeLog())
  let outcome = try await _run(
    deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  #expect(outcome == .completed)
  #expect(readinessCalls.value == 1)
}

/// roughcut은 분석 전용 도구를 요구하지 않는다 — 도구가 없는 보고로도
/// 개발·번들 길이 모두 지나간다.
@Test @MainActor func roughcutDoesNotDemandAnalysisToolsOrKeys() async throws {
  for mode in ["development", "bundled"] {
    let fixture = try _readinessFixture(
      configuration: .debug, bundlePresent: mode == "bundled")
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    let deps = _readinessDeps(
      job: fixture.job,
      mediaRoot: fixture.mediaRoot,
      developmentWorkspace: mode == "development" ? fixture.externalRoot : nil,
      readiness: { _ in _healthyReport(tools: (nil, nil)) },
      executor: PlanCapture(),
      scopeLog: ScopeLog())
    let outcome = try await _run(
      deps, operation: .roughcut, job: fixture.job, engineRoot: fixture.engineRoot,
      scripts: fixture.scripts)
    #expect(outcome == .completed)
  }
}

/// analyze는 실제 자식 환경의 ffmpeg·ffprobe를 요구한다 — 하나라도 없으면
/// 교정 글로 닫고, 권한 문서·스테이징·실행기는 없다(변형이 일어나지 않는다).
@Test @MainActor func analyzeRequiresToolsResolvedInTheActualChildEnvironment() async throws {
  for missingTool in ["ffmpeg", "ffprobe"] {
    let fixture = try _readinessFixture(configuration: .debug)
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    let executor = PlanCapture()
    let deps = _readinessDeps(
      job: fixture.job,
      mediaRoot: fixture.mediaRoot,
      developmentWorkspace: fixture.externalRoot,
      readiness: { _ in
        _healthyReport(
          tools: (missingTool == "ffmpeg" ? nil : "/synthetic/bin/ffmpeg",
                  missingTool == "ffprobe" ? nil : "/synthetic/bin/ffprobe"))
      },
      executor: executor,
      scopeLog: ScopeLog())
    do {
      _ = try await _run(
        deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
      Issue.record("도구 없이 지나갔다 (\(missingTool))")
    } catch let error as EnginePreflightError {
      #expect((error.errorDescription ?? "").contains(missingTool))
    }
    #expect(await executor.plans.isEmpty)
    #expect(
      !FileManager.default.fileExists(
        atPath: fixture.externalRoot.appendingPathComponent("Jobs").path))
    #expect(
      !FileManager.default.fileExists(
        atPath: fixture.job.directoryURL.appendingPathComponent(MediaGrantDocument.fileName).path))
  }
}

/// readiness 실패는 닫힌다 — 부서진 인터프리터(예외), 못 믿을 보고(모양 불량).
/// 권한 문서와 스테이징은 남지 않는다: 읽기 전용 검사가 변형보다 먼저다.
@Test @MainActor func readinessFailuresCloseTheGateWithoutMutatingWork() async throws {
  for broken: String in ["child-threw", "malformed-report", "missing-python"] {
    let fixture = try _readinessFixture(configuration: .debug)
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    let executor = PlanCapture()
    let deps = _readinessDeps(
      job: fixture.job,
      mediaRoot: fixture.mediaRoot,
      developmentWorkspace: fixture.externalRoot,
      readiness: { _ in
        switch broken {
        case "child-threw": throw NSError(domain: "readiness", code: 1)
        case "malformed-report":
          return EngineReadinessReport(
            engine: nil, readinessVersion: nil, pythonExecutable: nil,
            pythonVersion: nil, ffmpeg: "/x", ffprobe: "/x")
        default:
          return EngineReadinessReport(
            engine: "vlogsrt-engine", readinessVersion: 1,
            pythonExecutable: nil, pythonVersion: nil, ffmpeg: "/x", ffprobe: "/x")
        }
      },
      executor: executor,
      scopeLog: ScopeLog())
    do {
      _ = try await _run(
        deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
      Issue.record("readiness 실패가 열려 있다 (\(broken))")
    } catch let error as EnginePreflightError {
      #expect((error.errorDescription ?? "").contains("엔진"))
    }
    #expect(await executor.plans.isEmpty)
    #expect(
      !FileManager.default.fileExists(
        atPath: fixture.job.directoryURL.appendingPathComponent(MediaGrantDocument.fileName).path))
    #expect(
      !FileManager.default.fileExists(
        atPath: fixture.externalRoot.appendingPathComponent("Jobs").path))
  }
}

/// 번들 readiness는 번들 도구 **자리와 같은 경로**로만 만족한다 — Homebrew나
/// 그 밖의 번들 밖 경로를 말하는 보고는 거짓 준비다. 번들 bin이 차면 통과한다.
@Test @MainActor func bundledReadinessRequiresTheBundledToolPathsThemselves() async throws {
  let fixture = try _readinessFixture(configuration: .debug, bundlePresent: true)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let executor = PlanCapture()

  // 번들 bin이 비어 있다 — Homebrew 경로를 말하는 보고는 준비가 아니다.
  let homebrewDeps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: nil,
    readiness: { _ in
      _healthyReport(
        tools: ("/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin/ffprobe"))
    },
    executor: executor,
    scopeLog: ScopeLog())
  do {
    _ = try await _run(
      homebrewDeps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("Homebrew 도구로 번들 준비가 뚫렸다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("번들"))
  }
  #expect(await executor.plans.isEmpty)

  // 번들 bin에 진짜 도구를 두고 보고가 같은 자리를 말하면 지나간다.
  let bin = fixture.engineRoot.appendingPathComponent("bin", isDirectory: true)
  try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
  for tool in ["ffmpeg", "ffprobe"] {
    let path = bin.appendingPathComponent(tool)
    try Data("#!/bin/sh\n".utf8).write(to: path)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path.path)
  }
  // 물리 동일성으로 잰다 — 철자(/var vs /private/var)는 환경 따라 흔들린다.
  let canonicalFFmpeg = _physicalRoot(bin.appendingPathComponent("ffmpeg")).path
  let canonicalFFprobe = _physicalRoot(bin.appendingPathComponent("ffprobe")).path
  let passing = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: nil,
    readiness: { _ in
      _healthyReport(tools: (canonicalFFmpeg, canonicalFFprobe))
    },
    executor: PlanCapture(),
    scopeLog: ScopeLog())
  let outcome = try await _run(
    passing, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
  #expect(outcome == .completed)
}

/// 읽기 전용 roughcut 결과 자리는 실행 전에 닫힌다 — 이미 있는 읽기 전용
/// 파일이나 무효한 자리(디렉터리)는 교정 글로 실패, 실행기는 없다. 두 길
/// 모두에서 검사한다.
@Test @MainActor func readonlyOrInvalidRoughcutDestinationFailsBeforeLaunch() async throws {
  for variant in ["readonly-file", "directory"] {
    let fixture = try _readinessFixture(configuration: .debug)
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    let executor = PlanCapture()
    let deps = _readinessDeps(
      job: fixture.job,
      mediaRoot: fixture.mediaRoot,
      developmentWorkspace: fixture.externalRoot,
      executor: executor,
      scopeLog: ScopeLog())
    let destination = fixture.externalRoot
      .appendingPathComponent("Jobs").appendingPathComponent("job-42")
      .appendingPathComponent("roughcut.fcpxml")
    // 스테이징이 일어날 자리를 미리 만들어 둔다 — 실행 전 검사가 이걸 봐야 한다.
    try FileManager.default.createDirectory(
      at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
    if variant == "readonly-file" {
      try Data("locked\n".utf8).write(to: destination)
      try FileManager.default.setAttributes([.posixPermissions: 0o444], ofItemAtPath: destination.path)
    } else {
      try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
    }
    do {
      _ = try await _run(
        deps, operation: .roughcut, job: fixture.job, engineRoot: fixture.engineRoot,
        scripts: fixture.scripts)
      Issue.record("무효한 자리가 지나갔다 (\(variant))")
    } catch let error as EnginePreflightError {
      #expect((error.errorDescription ?? "").contains("roughcut"))
    }
    #expect(await executor.plans.isEmpty)
    // 검사는 권한 문서가 남기 **전에** 일어난다 — 변형이 없었다는 증거다.
    #expect(
      !FileManager.default.fileExists(
        atPath: fixture.job.directoryURL.appendingPathComponent(MediaGrantDocument.fileName).path))
    try? FileManager.default.setAttributes(
      [.posixPermissions: 0o644], ofItemAtPath: destination.path)
  }
}

/// 잡 디렉터리·작업공간이 쓰기 불가면 실행 전에 닫힌다 — 임시 생성/제거로 증명.
@Test @MainActor func unwritableJobOrWorkspaceFailsBeforeLaunch() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let executor = PlanCapture()
  let deps = _readinessDeps(
    job: fixture.job,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    executor: executor,
    scopeLog: ScopeLog())
  let workspace = fixture.job.directoryURL.appendingPathComponent("workspace", isDirectory: true)
  try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
  try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: workspace.path)
  defer {
    try? FileManager.default.setAttributes(
      [.posixPermissions: 0o755], ofItemAtPath: workspace.path)
  }
  do {
    _ = try await _run(
      deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("읽기 전용 작업공간이 지나갔다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("작업"))
  }
  #expect(await executor.plans.isEmpty)
}

/// 취소·드리프트는 스테이징·readiness·실행 전에 닫힌다 — 이 요청은 아무것도
/// 쓰지 않는다(드리프트는 취소 기록 한 줄만 남긴다).
@Test @MainActor func cancelledOrStaleSelectionNeverReachesStagingReadinessOrExecutor()
  async throws
{
  for mode in ["cancelled", "stale"] {
    let fixture = try _readinessFixture(configuration: .debug)
    defer { try? FileManager.default.removeItem(at: fixture.root) }
    let (registry, _) = try _testRegistry()
    if mode == "cancelled" {
      try Data("cancelled\n".utf8)
        .write(to: fixture.job.directoryURL.appendingPathComponent(CancellationStore.fileName))
    } else {
      registry.invalidate()
    }
    let scopeLog = ScopeLog()
    let readinessCalls = CountBox()
    let executor = PlanCapture()
    let deps = EngineRequestCoordinator.Dependencies(
      peekRoots: {
        [
          SecurityScopedBookmarkStore.PeekedBookmark(
            key: "media-root.synthetic", url: fixture.mediaRoot, isStale: false)
        ]
      },
      settleRoots: { peeked in peeked.map(\.url) },
      peekDevelopmentWorkspace: {
        SecurityScopedBookmarkStore.PeekedBookmark(
          key: "developmentEngineWorkspace", url: fixture.externalRoot, isStale: false)
      },
      settleDevelopmentWorkspace: { peeked in peeked?.url },
  
      startScope: { _ in true },
      stopScope: { _ in },
      readiness: { _ in
        readinessCalls.value += 1
        throw NSError(domain: "readiness", code: 9)
      },
      stageDevelopmentJob: { jobID, appGroupJob, externalRoot in
        scopeLog.record("staged")
        return try DevelopmentJobStager.stage(
          jobID: jobID, appGroupJob: appGroupJob, externalRoot: externalRoot)
      },
      executor: executor)
    let outcome = try await EngineRequestCoordinator(dependencies: deps).run(
      operation: .analyze,
      job: fixture.job,
      configuration: .debug,
      engineRoot: fixture.engineRoot,
      applicationScriptsDirectory: fixture.scripts,
      registry: registry)
    if mode == "cancelled" {
      #expect(outcome == .cancelledBeforeRun)
    } else {
      #expect(outcome == .staleSelection)
      #expect(
        FileManager.default.fileExists(
          atPath: fixture.job.directoryURL.appendingPathComponent(
            CancellationStore.fileName).path))
    }
    #expect(!scopeLog.events.contains("staged"))
    #expect(readinessCalls.value == 0)
    #expect(await executor.plans.isEmpty)
    #expect(scopeLog.events.isEmpty)
  }
}

/// 잡 아이디·입력·선택이 없으면 정해진 글로 닫는다 — scope조차 걸지 않는다.
@Test @MainActor func invalidJobIDMissingInputAndMissingSelectionAreActionable() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let scopeLog = ScopeLog()

  // 안전하지 않은 잡 아이디 — RoundTripJob 자체가 거절한다.
  do {
    _ = try RoundTripJob(workspace: AppGroupWorkspace(rootURL: fixture.root), id: "../escape")
    Issue.record("위험한 잡 아이디가 지나갔다")
  } catch is WorkspaceError {}

  let deps = EngineRequestCoordinator.Dependencies(
    peekRoots: {
      [
        SecurityScopedBookmarkStore.PeekedBookmark(
          key: "media-root.synthetic", url: fixture.mediaRoot, isStale: false)
      ]
    },
    settleRoots: { peeked in peeked.map(\.url) },
    peekDevelopmentWorkspace: { nil },
    settleDevelopmentWorkspace: { peeked in peeked?.url },

    startScope: { _ in
      scopeLog.record("start")
      return true
    },
    stopScope: { _ in scopeLog.record("stop") },
    readiness: { _ in throw NSError(domain: "readiness", code: 9) },
    stageDevelopmentJob: { _, _, _ in throw NSError(domain: "readiness", code: 1) },
    executor: PlanCapture())

  // 입력이 없다.
  try FileManager.default.removeItem(at: fixture.job.inputURL)
  do {
    _ = try await _run(
      deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("입력 없이 지나갔다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("input.fcpxml"))
  }
  #expect(scopeLog.events.isEmpty)

  // 입력은 있지만 선택 기록이 없다.
  try Data("<fcpxml/>\n".utf8).write(to: fixture.job.inputURL)
  try FileManager.default.removeItem(at: fixture.job.projectSelectionURL)
  do {
    _ = try await _run(
      deps, job: fixture.job, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("선택 없이 지나갔다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("선택"))
  }
  #expect(scopeLog.events.isEmpty)
}

/// 루트 밖 미디어·읽을 수 없는 원본은 scope가 걸린 뒤에 검사되고, 실패하면
/// scope는 물러난다 — 권한 문서도 스테이징도 남지 않는다.
@Test @MainActor func escapingOrUnreadableMediaFailsAfterScopeAcquisition() async throws {
  let fixture = try _readinessFixture(configuration: .debug)
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let scopeLog = ScopeLog()

  // 루트 밖 참조 — grant 검사가 닫는다(정해진 문면에 교정 글이 붙는다).
  let outside = fixture.root.appendingPathComponent("elsewhere", isDirectory: true)
  try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
  let escapee = outside.appendingPathComponent("escapee.mov")
  try Data("x".utf8).write(to: escapee)
  let escapingJob = try RoundTripJob(
    workspace: AppGroupWorkspace(rootURL: fixture.root), id: "job-43")
  try FileManager.default.createDirectory(
    at: escapingJob.directoryURL, withIntermediateDirectories: true)
  let escapingXML = """
    <?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.14"><resources>
      <asset id="r2" name="escapee.mov" start="0s" duration="1s" hasAudio="1">
        <media-rep kind="original-media" src="\(escapee.absoluteString)"/>
      </asset>
    </resources></fcpxml>
    """
  try escapingJob.writeInput(Data(escapingXML.utf8))
  try escapingJob.writeProjectSelection(
    ActiveProjectSelection(
      projectUID: "P-1",
      projectName: "Project",
      timelineStart: RationalTime(value: 0, timescale: 1),
      duration: RationalTime(value: 10, timescale: 1),
      frameDuration: RationalTime(value: 1, timescale: 25)))
  let deps = _readinessDeps(
    job: escapingJob,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    executor: PlanCapture(),
    scopeLog: scopeLog)
  do {
    _ = try await _run(
      deps, job: escapingJob, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("루트 밖 참조가 지나갔다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("미디어"))
  }
  #expect(!scopeLog.events.filter { $0.hasPrefix("stop:") }.isEmpty)
  #expect(
    !FileManager.default.fileExists(
      atPath: escapingJob.directoryURL.appendingPathComponent(MediaGrantDocument.fileName).path))

  // 참조가 루트 안이지만 원본이 없다 — 가독성 검사가 닫는다.
  let ghost = fixture.mediaRoot.appendingPathComponent("ghost.mov")
  let ghostJob = try RoundTripJob(
    workspace: AppGroupWorkspace(rootURL: fixture.root), id: "job-44")
  try FileManager.default.createDirectory(
    at: ghostJob.directoryURL, withIntermediateDirectories: true)
  let ghostXML = """
    <?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.14"><resources>
      <asset id="r2" name="ghost.mov" start="0s" duration="1s" hasAudio="1">
        <media-rep kind="original-media" src="\(ghost.absoluteString)"/>
      </asset>
    </resources></fcpxml>
    """
  try ghostJob.writeInput(Data(ghostXML.utf8))
  try ghostJob.writeProjectSelection(
    ActiveProjectSelection(
      projectUID: "P-1",
      projectName: "Project",
      timelineStart: RationalTime(value: 0, timescale: 1),
      duration: RationalTime(value: 10, timescale: 1),
      frameDuration: RationalTime(value: 1, timescale: 25)))
  let ghostDeps = _readinessDeps(
    job: ghostJob,
    mediaRoot: fixture.mediaRoot,
    developmentWorkspace: fixture.externalRoot,
    executor: PlanCapture(),
    scopeLog: scopeLog)
  do {
    _ = try await _run(
      ghostDeps, job: ghostJob, engineRoot: fixture.engineRoot, scripts: fixture.scripts)
    Issue.record("없는 원본이 지나갔다")
  } catch let error as EnginePreflightError {
    #expect((error.errorDescription ?? "").contains("ghost.mov"))
  }
}

/// readiness 보고의 판독기 — 실제 자식이 남긴 한 줄 JSON을 그대로 읽는다.
@Test func readinessReportDecodesTheBoundedChildResponse() throws {
  let payload = """
    {"carrier_versions": ["1", "2"], "engine": "vlogsrt-engine",
    "python": {"executable": "/opt/venv/bin/python", "version": "3.11.9"},
    "readiness_version": 1,
    "tools": {"ffmpeg": null, "ffprobe": "/usr/local/bin/ffprobe"}}
    """
  let report = try JSONDecoder().decode(
    EngineReadinessReport.self, from: Data(payload.utf8))
  #expect(report.engine == "vlogsrt-engine")
  #expect(report.readinessVersion == 1)
  #expect(report.pythonExecutable == "/opt/venv/bin/python")
  #expect(report.pythonVersion == "3.11.9")
  #expect(report.ffmpeg == nil)
  #expect(report.ffprobe == "/usr/local/bin/ffprobe")
  // 통신 문면(진입점이 남기는 모양 그대로의 문장열)에는 자격 증명 재료가 들어갈
  // 자리가 아예 없다 — 공개 데이터뿐이다. 생산 보고는 Decodable이 전부이므로
  // 시험은 실제 문면을 지어 검사한다(생산을 시험 편의로 넓히지 않는다).
  let wire = payload
  #expect(!wire.lowercased().contains("key"))
  #expect(!wire.lowercased().contains("secret"))
}
