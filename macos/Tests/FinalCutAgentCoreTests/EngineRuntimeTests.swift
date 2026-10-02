import CryptoKit
import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  엔진 런타임 — Release 번들 전용 · 취소/드리프트 게이트 · 실행 선택
// ─────────────────────────────────────────────────────────────

/** @Sendable import 클로저가 못 박는 값을 모으는 상자. */
final class URLBox: @unchecked Sendable {
  var urls: [URL] = []
}

/// 녹화하는 가짜 실행기 — 호출 시점의 side effect로 취소·드리프트를 흉내 낸다.
final class RecordingExecutor: EngineExecuting, @unchecked Sendable {
  let status: Int32
  var plans: [String] = []
  var onRun: (() -> Void)?

  init(status: Int32 = 0, onRun: (() -> Void)? = nil) {
    self.status = status
    self.onRun = onRun
  }

  func run(_ plan: EngineLaunchPlan) async throws -> Int32 {
    plans.append(Self.label(plan))
    onRun?()
    return status
  }

  static func label(_ plan: EngineLaunchPlan) -> String {
    switch plan {
    case .bundled: "bundled"
    case .development: "development"
    case .unavailable: "unavailable"
    }
  }
}

func _runtimeContext(
  plan: EngineLaunchPlan,
  registry: ActiveSelectionRegistry,
  revision: String,
  appGroupJob: URL,
  externalJob: URL? = nil,
  operation: DevelopmentEngineOperation = .analyze
) -> EngineSessionContext {
  EngineSessionContext(
    operation: operation,
    jobID: "job-42",
    appGroupJob: appGroupJob,
    externalJob: externalJob,
    plan: plan,
    selectionRevision: revision,
    registry: registry,
    now: { "2026-09-21T09:00:00Z" }
  )
}

func _testRegistry(selection: String = "a".padding(toLength: 64, withPad: "a", startingAt: 0))
  throws -> (ActiveSelectionRegistry, String)
{
  let suite = "test.engine.registry.\(UUID().uuidString)"
  let defaults = try #require(UserDefaults(suiteName: suite))
  let registry = ActiveSelectionRegistry(defaults: defaults)
  try registry.activate(
    ActiveProjectSelection(
      projectUID: "P-1",
      projectName: "Project",
      timelineStart: RationalTime(value: 0, timescale: 1),
      duration: RationalTime(value: 10, timescale: 1),
      frameDuration: RationalTime(value: 1, timescale: 25)
    ))
  // activate가 만든 리비전을 그대로 돌려준다 — 테스트는 이 값을 게이트에 쓴다.
  let stored = defaults.string(forKey: "activeProjectSelectionRevision")
  let revision = try #require(stored)
  return (registry, revision)
}

@Test func releaseBuildNeverFallsBackToDevelopmentEngine() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let engineRoot = root.appendingPathComponent("Engine", isDirectory: true)
  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  let external = root.appendingPathComponent("external-job", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  // 번들이 없는 Release — 개발 스크립트가 있어도 사용하지 않는다.
  let released = EngineRuntime.resolvePlan(
    configuration: .release,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: engineRoot,
    applicationScriptsDirectory: scripts,
    developmentExternalJob: external,
    apiKey: "secret"
  )
  guard case .unavailable(let reason) = released else {
    Issue.record("Release는 unavailable이어야 한다")
    return
  }
  #expect(reason.contains("Release"))

  // 번들이 있으면 Release도 번들로만 실행한다.
  let executable = engineRoot
    .appendingPathComponent("Contents", isDirectory: true)
    .appendingPathComponent("MacOS", isDirectory: true)
    .appendingPathComponent("vlogsrt-engine")
  try FileManager.default.createDirectory(
    at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
  try Data("#!/bin/sh\n".utf8).write(to: executable)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
  let bundled = EngineRuntime.resolvePlan(
    configuration: .release,
    operation: .roughcut,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: engineRoot,
    applicationScriptsDirectory: nil,
    developmentExternalJob: nil,
    apiKey: "secret"
  )
  guard case .bundled(let invocation) = bundled else {
    Issue.record("번들이 있으면 bundled여야 한다")
    return
  }
  // 키는 자식 환경변수 한 통으로, 도구 PATH는 번들 bin+시스템 최소 경로로만.
  #expect(invocation.environment[BundledEngineInvocation.apiKeyEnvironmentName] == "secret")
  #expect(
    invocation.environment[BundledEngineInvocation.pathEnvironmentName]
      == "\(engineRoot.standardizedFileURL.path)/bin:/usr/bin:/bin")
  #expect(
    !(invocation.environment[BundledEngineInvocation.pathEnvironmentName] ?? "")
      .contains("/opt/homebrew"))

  // Debug는 번들이 없을 때 개발 실행기로 되돌린다 — 이제 **실행 가능한** 래퍼가
  // 있어야 한다(S2A-2: 없는 래퍼의 development 계획은 준비의 말이 거짓이다).
  let wrapper = scripts.appendingPathComponent(DevelopmentEngineInvocation.scriptName)
  try Data("#!/bin/sh\n".utf8).write(to: wrapper)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
  let debug = EngineRuntime.resolvePlan(
    configuration: .debug,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: nil,
    applicationScriptsDirectory: scripts,
    developmentExternalJob: external,
    apiKey: nil
  )
  guard case .development(let dev) = debug else {
    Issue.record("Debug 폴백이 development여야 한다")
    return
  }
  #expect(dev.arguments.first == "analyze")

  let stranded = EngineRuntime.resolvePlan(
    configuration: .debug,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: nil,
    applicationScriptsDirectory: nil,
    developmentExternalJob: nil,
    apiKey: nil
  )
  #expect(!stranded.isExecutable)
}

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-2 마지막 단위 — 초기 RED: 실제 호출 경계의 두 결함
// ─────────────────────────────────────────────────────────────

/// Debug 폴백은 **실행 가능한** 개발 래퍼가 있을 때만이다 — 래퍼가 없거나 실행
/// 권한이 없으면 unavailable로 닫혀야 한다. 오늘 resolvePlan은 래퍼의 존재를
/// 보지 않고 development 계획을 내주므로, 실행은 시작 단계에서만 죽는다.
@Test func debugFallbackRequiresAnExecutableDevelopmentWrapper() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  let external = root.appendingPathComponent("external-job", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let wrapper = scripts.appendingPathComponent(DevelopmentEngineInvocation.scriptName)

  // 래퍼가 아예 없다 — development가 아니라 unavailable이어야 한다.
  let missing = EngineRuntime.resolvePlan(
    configuration: .debug,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: nil,
    applicationScriptsDirectory: scripts,
    developmentExternalJob: external,
    apiKey: nil
  )
  guard case .unavailable(let missingReason) = missing else {
    Issue.record("래퍼가 없으면 unavailable이어야 한다: \(missing)")
    return
  }
  #expect(missingReason.contains("개발"))

  // 래퍼가 있지만 실행 권한이 없다 — 같은 판정이다.
  try Data("#!/bin/sh\n".utf8).write(to: wrapper)
  let nonExecutable = EngineRuntime.resolvePlan(
    configuration: .debug,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: nil,
    applicationScriptsDirectory: scripts,
    developmentExternalJob: external,
    apiKey: nil
  )
  guard case .unavailable = nonExecutable else {
    Issue.record("실행 권한 없는 래퍼는 unavailable이어야 한다: \(nonExecutable)")
    return
  }

  // 실행 권한을 주면 그제야 development가 된다 — 같은 입력의 대조군이다.
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
  let executable = EngineRuntime.resolvePlan(
    configuration: .debug,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: nil,
    applicationScriptsDirectory: scripts,
    developmentExternalJob: external,
    apiKey: nil
  )
  guard case .development = executable else {
    Issue.record("실행 가능한 래퍼는 development여야 한다: \(executable)")
    return
  }
}

/// 권한 문서의 JSON은 파이썬 소비자(`_staged_app_grant_revision`)가 읽는 키로
/// 나가야 한다 — `grant_revision`(snake_case). 오늘 부호화는 Swift 프로퍼티
/// 이름(grantRevision)을 그대로 쓰므로, 앱이 스테이징한 문서는 엔진 쪽에서
/// 'grant_revision이 없다'로 죽는다. 문서의 바이트가 계약이다.
@Test func mediaGrantDocumentWireJSONCarriesThePythonConsumedKey() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let mediaRoot = root.appendingPathComponent("media", isDirectory: true)
  try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let grant = MediaGrantDocument(roots: [mediaRoot])
  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try grant.write(to: job)

  let raw = try String(
    contentsOf: job.appendingPathComponent(MediaGrantDocument.fileName), encoding: .utf8)
  let payload = try #require(
    JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any])
  // 파이썬이 읽는 키 — 이 자리에 값이 없으면 엔진의 준비가 죽는다.
  let revision = try #require(payload["grant_revision"] as? String)
  #expect(revision == grant.grantRevision)
  #expect(revision.count == 64)
  // 낡은 camelCase 키는 더 이상 나가지 않는다 — 두 철자가 함께 있으면 읽는
  // 쪽이 어느 것이 정본인지 말할 수 없다.
  #expect(payload["grantRevision"] == nil)

  // 교차 증명: 파이썬 소비자의 지문 재료(`_swift_grant_revision`)와 같은 바이트를
  // 말한다 — 유니코드·끝 공백 경로에서도 글자를 가공하지 않는다.
  let unicodeRoot = root.appendingPathComponent("미디어 루트 한글 ", isDirectory: true)
  let unicodeGrant = MediaGrantDocument(roots: [unicodeRoot])
  let canonical = unicodeRoot.resolvingSymlinksInPath().standardizedFileURL.path
  let twin = Data("{\"rootPaths\":[\"\(canonical)\"]}".utf8)
  let twinDigest = SHA256.hash(data: twin).map { String(format: "%02x", $0) }.joined()
  #expect(unicodeGrant.grantRevision == twinDigest)
  let unicodePayload = try #require(
    JSONSerialization.jsonObject(
      with: JSONEncoder().encode(unicodeGrant)) as? [String: Any])
  #expect((unicodePayload["roots"] as? [String]) == [canonical])  // 원문 글자 그대로
}

@Test func engineSessionGatesCancellationBeforeRunAndFailureSyncsDiagnostics() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  // 실행 전 취소 — 엔진을 부르지 않는다.
  try Data("cancelled\n".utf8).write(to: job.appendingPathComponent(CancellationStore.fileName))
  let executor = RecordingExecutor()
  let outcome = try await EngineSessionRunner(executor: executor).run(
    _runtimeContext(plan: try makeBundledPlan(), registry: registry, revision: revision, appGroupJob: job)
  )
  #expect(outcome == .cancelledBeforeRun)
  #expect(executor.plans.isEmpty)

  // 종료코드 비0(번들 실행 — 외부 작업이 없으니 동기화 자체가 없다). 3은 중복
  // 소유권으로 예약됐으니 일반 실패는 4로 본다(S2A-1).
  try FileManager.default.removeItem(at: job.appendingPathComponent(CancellationStore.fileName))
  var synced: [(URL, Bool)] = []
  let failed = try await EngineSessionRunner(executor: RecordingExecutor(status: 4)).run(
    _runtimeContext(plan: try makeBundledPlan(), registry: registry, revision: revision, appGroupJob: job),
    syncResults: { _, to, diagnosticsOnly in synced.append((to, diagnosticsOnly)) }
  )
  #expect(failed == .engineFailed(4))
  #expect(synced.isEmpty)

  // 개발(외부) 실행이 실패로 끝나도 진단 증거는 앱으로 나온다 — 진단 전용 동기다(S1B).
  let external = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  let failedExternal = try await EngineSessionRunner(executor: RecordingExecutor(status: 4)).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external
    ),
    syncResults: { _, to, diagnosticsOnly in synced.append((to, diagnosticsOnly)) }
  )
  #expect(failedExternal == .engineFailed(4))
  #expect(synced.count == 1)
  #expect(synced.first?.0 == job)
  #expect(synced.first?.1 == true)

  // 취소된 실패 실행은 증거를 가져오지 않는다 — 취소 게이트가 먼저다(S1A).
  let cancelOnFailure = RecordingExecutor(status: 2, onRun: {
    try? Data("cancelled\n".utf8)
      .write(to: job.appendingPathComponent(CancellationStore.fileName))
  })
  synced.removeAll()
  let cancelledFailure = try await EngineSessionRunner(executor: cancelOnFailure).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external
    ),
    syncResults: { _, to, diagnosticsOnly in synced.append((to, diagnosticsOnly)) }
  )
  #expect(cancelledFailure == .stoppedBeforeSync)
  #expect(synced.isEmpty)
}

@Test func engineSessionStopsBetweenRunSyncAndImportOnCancellationOrDrift() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  // 엔진 실행 중 취소 기록이 생기면 — sync 전에 멈춘다.
  let cancelMidRun = RecordingExecutor(onRun: {
    try? Data("cancelled\n".utf8)
      .write(to: job.appendingPathComponent(CancellationStore.fileName))
  })
  var synced: [URL] = []
  let imported = URLBox()
  let stopped = try await EngineSessionRunner(executor: cancelMidRun).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external
    ),
    syncResults: { _, to, _ in synced.append(to) },
    importRoughcut: { imported.urls.append($0) }
  )
  #expect(stopped == .stoppedBeforeSync)
  #expect(synced.isEmpty)

  // 결과 동기화 뒤 import 직전 취소 — 결과는 남고 Final Cut으로는 안 간다.
  try FileManager.default.removeItem(at: job.appendingPathComponent(CancellationStore.fileName))
  let cancelAfterSync = RecordingExecutor()
  let beforeImport = try await EngineSessionRunner(executor: cancelAfterSync).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external, operation: .roughcut
    ),
    syncResults: { _, to, _ in
      synced.append(to)
      try? Data("cancelled\n".utf8)
        .write(to: job.appendingPathComponent(CancellationStore.fileName))
    },
    importRoughcut: { imported.urls.append($0) }
  )
  #expect(beforeImport == .stoppedBeforeImport)
  #expect(synced.count == 1)
  #expect(imported.urls.isEmpty)

  // 취소를 치우고 정상 종료 — roughcut은 import까지 간다.
  try FileManager.default.removeItem(at: job.appendingPathComponent(CancellationStore.fileName))
  let completed = try await EngineSessionRunner(executor: RecordingExecutor()).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external, operation: .roughcut
    ),
    syncResults: { _, to, _ in synced.append(to) },
    importRoughcut: { imported.urls.append($0) }
  )
  #expect(completed == .completed)
  #expect(imported.urls.map(\.lastPathComponent) == ["roughcut.fcpxml"])
}

@Test func registryDriftRecordsActiveProjectChangedInBothLocations() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("external-job", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  // 엔진이 남긴 revision.jsonl — 드리프트 취소의 input_revision 근원.
  let workspace = job.appendingPathComponent("workspace", isDirectory: true)
  try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
  let inputRevision = String(repeating: "f", count: 64)
  try Data(
    ("{\"payload\":{\"revision\":\"\(inputRevision)\"},\"protocol_version\":\"1.0\","
      + "\"type\":\"ProjectRevision\"}\n").utf8
  ).write(to: workspace.appendingPathComponent("revision.jsonl"))

  // 실행 중 활성 프로젝트가 바뀐다.
  let driftMidRun = RecordingExecutor(onRun: { registry.invalidate() })
  let outcome = try await EngineSessionRunner(executor: driftMidRun).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external
    )
  )
  #expect(outcome == .staleSelection)
  for base in [job, external] {
    let line = try String(
      contentsOf: base.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
    #expect(line.contains("\"reason\":\"active_project_changed\""))
    #expect(line.contains("\"attempt_id\":\"*\""))
    #expect(line.contains(inputRevision))
    #expect(line.contains("\"protocol_version\":\"\(ProtocolCodec.analysisVersion)\""))
  }
  // 취소됐으므로 다음 실행은 시작조차 하지 않는다.
  let again = try await EngineSessionRunner(executor: RecordingExecutor()).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision,
      appGroupJob: job, externalJob: external
    )
  )
  #expect(again == .cancelledBeforeRun)
  // revision 기록이 없을 때의 근원 — nil 대신 all-zero(게이트는 존재로 닫힌다).
  #expect(
    EngineSessionRunner.stagedInputRevision(job: external, fileManager: .default) == nil)
}

@Test func developmentDriftCancellationBindsToTheRunningExternalRevision() async throws {
  // 외부 job workspace에만 리비전이 있고 App Group에는 없다 — 개발 실행 중 드리프트
  // 취소는 실행 경로(외부)의 리비전을 실어야 엔진이 이 실행을 멈춘다(S1A-R1).
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  let externalWorkspace = external.appendingPathComponent("workspace", isDirectory: true)
  try FileManager.default.createDirectory(
    at: externalWorkspace, withIntermediateDirectories: true)
  let inputRevision = String(repeating: "e", count: 64)
  try Data(
    ("{\"payload\":{\"revision\":\"\(inputRevision)\"},\"protocol_version\":\"1.0\","
      + "\"type\":\"ProjectRevision\"}\n").utf8
  ).write(to: externalWorkspace.appendingPathComponent("revision.jsonl"))

  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  let plan = EngineLaunchPlan.development(
    try DevelopmentEngineInvocation.create(
      operation: .analyze,
      jobID: "job-42",
      externalJob: external,
      applicationScriptsDirectory: scripts
    )
  )

  let driftMidRun = RecordingExecutor(onRun: { registry.invalidate() })
  let outcome = try await EngineSessionRunner(executor: driftMidRun).run(
    _runtimeContext(
      plan: plan, registry: registry, revision: revision,
      appGroupJob: job, externalJob: external
    )
  )
  #expect(outcome == .staleSelection)
  for base in [job, external] {
    let line = try String(
      contentsOf: base.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
    #expect(line.contains(inputRevision))
  }
}

@Test func runningJobCancellationPrefersTheRunningEngineWorkspace() throws {
  // 양쪽에 서로 다른 리비전이 있다 — 기록은 실행 경로의 것만 실린다(S1A-R1).
  // App Group을 먼저 읽으면 개발 실행 중 취소가 낡은 리비전으로 묶인다.
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let appGroup = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: appGroup, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let appGroupRevision = String(repeating: "a", count: 64)
  let externalRevision = String(repeating: "e", count: 64)
  for (base, revision) in [(appGroup, appGroupRevision), (external, externalRevision)] {
    let workspace = base.appendingPathComponent("workspace", isDirectory: true)
    try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
    try Data(
      ("{\"payload\":{\"revision\":\"\(revision)\"},\"protocol_version\":\"1.0\","
        + "\"type\":\"ProjectRevision\"}\n").utf8
    ).write(to: workspace.appendingPathComponent("revision.jsonl"))
  }

  let development = RunningJobCancellation(
    appGroupJob: appGroup, externalJob: external, runningJob: external)
  #expect(development.inputRevision() == externalRevision)
  try development.record(reason: "user", requestedAt: "2026-09-27T06:00:00Z")
  for base in [appGroup, external] {
    let line = try String(
      contentsOf: base.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
    #expect(line.contains(externalRevision))
    #expect(!line.contains(appGroupRevision))
  }

  let bundled = RunningJobCancellation(
    appGroupJob: appGroup, externalJob: external, runningJob: appGroup)
  #expect(bundled.inputRevision() == appGroupRevision)
  try bundled.record(reason: "user", requestedAt: "2026-09-27T06:00:01Z")
  let line = try String(
    contentsOf: appGroup.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
  #expect(line.contains(appGroupRevision))
}

@Test func runningJobCancellationBindsPreReceiveToTheStagedInput() throws {
  // receive 전 — 리비전 기록이 없다. 스테이징된 input.fcpxml의 SHA-256(엔진의
  // input_sha256)로 job에 묶는다. all-zero 와일드카드가 아니다(S1A-R1).
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let appGroup = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: appGroup, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let stagedInput = Data("<fcpxml version=\"1.14\"/>\n".utf8)
  try stagedInput.write(to: appGroup.appendingPathComponent(RunningJobCancellation.inputFileName))
  try stagedInput.write(to: external.appendingPathComponent(RunningJobCancellation.inputFileName))
  let expected = CancellationCodec.hex(SHA256.hash(data: stagedInput))

  let preReceive = RunningJobCancellation(
    appGroupJob: appGroup, externalJob: external, runningJob: external)
  #expect(preReceive.inputRevision() == expected)
  try preReceive.record(reason: "user", requestedAt: "2026-09-27T06:02:00Z")
  for base in [appGroup, external] {
    let line = try String(
      contentsOf: base.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
    #expect(line.contains(expected))
    #expect(!line.contains(String(repeating: "0", count: 64)))
  }

  // 스테이징 입력마저 없으면 all-zero — 엔진은 무시하지만 앱 게이트는 닫힌다.
  let bare = root.appendingPathComponent("bare", isDirectory: true)
  try FileManager.default.createDirectory(at: bare, withIntermediateDirectories: true)
  let empty = RunningJobCancellation(appGroupJob: bare, externalJob: nil, runningJob: bare)
  #expect(empty.inputRevision() == String(repeating: "0", count: 64))
}

@Test func runningJobCancellationAlwaysRecordsTheRunningRootDespiteMirrorDrift() throws {
  // 미러가 어긋나거나 없어도 실행 루트는 반드시 받는다(S1A-R1) — 실행 폴더의
  // 기록이 빠지면 실행 중 취소가 유실된다.
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let appGroup = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  let unrelatedMirror = root.appendingPathComponent("stale-mirror", isDirectory: true)
  try FileManager.default.createDirectory(at: appGroup, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: unrelatedMirror, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  // 개발 실행 — 실행 경로(외부)에만 리비전이 있고 미러는 이 실행과 무관한 폴더다.
  let externalWorkspace = external.appendingPathComponent("workspace", isDirectory: true)
  try FileManager.default.createDirectory(
    at: externalWorkspace, withIntermediateDirectories: true)
  let externalRevision = String(repeating: "e", count: 64)
  try Data(
    ("{\"payload\":{\"revision\":\"\(externalRevision)\"},\"protocol_version\":\"1.0\","
      + "\"type\":\"ProjectRevision\"}\n").utf8
  ).write(to: externalWorkspace.appendingPathComponent("revision.jsonl"))

  let development = RunningJobCancellation(
    appGroupJob: appGroup, externalJob: unrelatedMirror, runningJob: external)
  try development.record(reason: "user", requestedAt: "2026-09-27T06:04:00Z")
  let devLine = try String(
    contentsOf: external.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
  #expect(devLine.contains(externalRevision))

  // 개발 실행 — App Group을 못 믿어도(없는 경로) 실행 루트는 받는다.
  let missing = root.appendingPathComponent("not-there", isDirectory: true)
  let orphan = RunningJobCancellation(
    appGroupJob: missing, externalJob: nil, runningJob: external)
  try orphan.record(reason: "user", requestedAt: "2026-09-27T06:04:01Z")
  #expect(
    FileManager.default.fileExists(
      atPath: external.appendingPathComponent(CancellationStore.fileName).path))

  // 번들 실행 — 낡은 외부 미러가 남아 있어도 App Group(실행 루트)이 정본이다.
  let appGroupWorkspace = appGroup.appendingPathComponent("workspace", isDirectory: true)
  try FileManager.default.createDirectory(
    at: appGroupWorkspace, withIntermediateDirectories: true)
  let appGroupRevision = String(repeating: "a", count: 64)
  try Data(
    ("{\"payload\":{\"revision\":\"\(appGroupRevision)\"},\"protocol_version\":\"1.0\","
      + "\"type\":\"ProjectRevision\"}\n").utf8
  ).write(to: appGroupWorkspace.appendingPathComponent("revision.jsonl"))
  let bundled = RunningJobCancellation(
    appGroupJob: appGroup, externalJob: unrelatedMirror, runningJob: appGroup)
  try bundled.record(reason: "user", requestedAt: "2026-09-27T06:04:02Z")
  let bundledLine = try String(
    contentsOf: appGroup.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
  #expect(bundledLine.contains(appGroupRevision))
  #expect(!bundledLine.contains(externalRevision))
}

func makeBundledPlan() throws -> EngineLaunchPlan {
  let invocation = try? BundledEngineInvocation.create(
    operation: .analyze,
    jobID: "job-42",
    jobDirectory: FileManager.default.temporaryDirectory,
    engineRoot: FileManager.default.temporaryDirectory,
    apiKey: "secret"
  )
  return .bundled(try #require(invocation))
}

// ─────────────────────────────────────────────────────────────
//  공유 받기 — 검증 통과 전에는 잡이 생기지 않는다
// ─────────────────────────────────────────────────────────────

@Test func sharedIntakeCreatesNoJobWhenEitherHalfIsInvalid() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let origin = OriginMapping(
    projectUID: "FINAL-UID-1", originJobID: "roundtrip-7",
    originRevision: String(repeating: "a", count: 64), recordedAt: "2026-10-01T00:00:00Z")

  let xml = root.appendingPathComponent("final.fcpxml")
  try Data(
    """
    <fcpxml version="1.14"><library><event name="E">\
    <project name="P" uid="FINAL-UID-1"><sequence duration="1s"/></project>\
    </event></library></fcpxml>
    """.utf8
  ).write(to: xml)
  let bad = root.appendingPathComponent("bad.wav")
  try Data().write(to: bad)

  #expect(throws: ShareInboxError.self) {
    _ = try ShareInbox.receive(
      urls: [xml, bad],
      workspace: workspace,
      jobID: "caption-1",
      origin: origin
    )
  }
  #expect(
    !FileManager.default.fileExists(
      atPath: root.appendingPathComponent("Jobs").appendingPathComponent("caption-1").path))

  // 유효한 쌍만 잡을 만든다 — 스테이징되는 문서는 엔진이 요구하는 원본 매핑이다.
  let good = root.appendingPathComponent("good.wav")
  try Data(wavHeader(rate: 48_000)).write(to: good)
  let originURL = try ShareInbox.receive(
    urls: [xml, good],
    workspace: workspace,
    jobID: "caption-2",
    origin: origin
  )
  #expect(originURL.lastPathComponent == "caption-origin.json")
  #expect(
    !FileManager.default.fileExists(
      atPath: root.appendingPathComponent("Jobs/caption-2/caption-request.jsonl").path))
}

// ─────────────────────────────────────────────────────────────
//  미디어 권한 — FCPXML 참조가 활성 루트 안인가 + grant 문서
// ─────────────────────────────────────────────────────────────

@Test func mediaGrantValidatesContainmentAndStagesTheDocument() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let mediaRoot = root.appendingPathComponent("media", isDirectory: true)
  let clip = mediaRoot.appendingPathComponent("clip.mov")
  try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
  try Data("media".utf8).write(to: clip)
  let fcpxml = root.appendingPathComponent("in.fcpxml")
  try EngineRuntimeTestFixtures.write(
    """
    <?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.14"><resources>
      <asset id="r2" name="clip.mov" uid="A" start="0s" duration="1s" hasAudio="1">
        <media-rep kind="original-media" src="\(clip.absoluteString)"/>
      </asset>
    </resources>
    <library><event><project name="P"><sequence format="f"/></project></event></library></fcpxml>
    """, to: fcpxml)
  defer { try? FileManager.default.removeItem(at: root) }

  #expect(throws: MediaGrantError.noAllowedRoots.self) {
    _ = try MediaGrantValidator.validate(fcpxml: fcpxml, roots: [])
  }

  let outside = root.appendingPathComponent("elsewhere", isDirectory: true)
  let escapee = outside.appendingPathComponent("escapee.mov")
  try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
  try Data("x".utf8).write(to: escapee)
  let escaping = root.appendingPathComponent("escaping.fcpxml")
  try EngineRuntimeTestFixtures.write(
    """
    <?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.14"><resources>
      <asset id="r3" name="escapee.mov" uid="B" start="0s" duration="1s" hasAudio="1">
        <media-rep kind="original-media" src="\(escapee.absoluteString)"/>
      </asset>
    </resources></fcpxml>
    """, to: escaping)
  do {
    _ = try MediaGrantValidator.validate(fcpxml: escaping, roots: [mediaRoot])
    Issue.record("루트 밖 참조는 실패해야 한다")
  } catch let error as MediaGrantError {
    guard case .mediaOutsideAllowedRoots(let paths) = error else {
      Issue.record("기대와 다른 오류다: \(error)")
      return
    }
    #expect(paths == [escapee.path])
  }

  // 유효한 참조는 권한 문서를 내고 — 문서의 리비전은 루트 목록에 묶인다.
  let grant = try MediaGrantValidator.validate(fcpxml: fcpxml, roots: [mediaRoot])
  #expect(grant.roots == [mediaRoot.resolvingSymlinksInPath().standardizedFileURL.path])
  #expect(grant.grantRevision.count == 64)
  #expect(
    grant.grantRevision
      == MediaGrantDocument.revision(rootPaths: grant.roots)
  )
  #expect(
    grant.grantRevision
      != MediaGrantDocument.revision(rootPaths: [grant.roots[0], "/somewhere/else"]).description)
  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try grant.write(to: job)
  let written = try Data(contentsOf: job.appendingPathComponent(MediaGrantDocument.fileName))
  let decoded = try JSONDecoder().decode(MediaGrantDocument.self, from: written)
  #expect(decoded == grant)
}

extension EngineRuntimeTestFixtures {

  /// 테스트 문자열을 파일로 — 매번 Data 변환 상용구를 줄인다.
  static func write(_ text: String, to url: URL) throws {
    try Data(text.utf8).write(to: url)
  }
}

enum EngineRuntimeTestFixtures {}

// ─────────────────────────────────────────────────────────────
//  SDEF — asset 클래스/속성 + make 명령 구조
// ─────────────────────────────────────────────────────────────

@Test func sdefDeclaresApplesDocumentedMediaAssetProtocol() throws {
  let sdefURL = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .appendingPathComponent("macos/Resources/FinalCutAgent.sdef")
  let data = try Data(contentsOf: sdefURL)
  let document = try XMLDocument(data: data, options: [])

  func names(_ xpath: String) throws -> [String] {
    try document.nodes(forXPath: xpath)
      .compactMap { ($0 as? XMLElement)?.attribute(forName: "name")?.stringValue }
  }
  func codes(_ xpath: String) throws -> [String] {
    try document.nodes(forXPath: xpath)
      .compactMap { ($0 as? XMLElement)?.attribute(forName: "code")?.stringValue }
  }

  // 표준 suite를 XInclude로 싣는다 — get·open 등 기본 용어의 뿌리다.
  #expect(try document.nodes(forXPath: "//*[local-name()='include']").count == 1)

  // record-type 두 개 — 문서가 정의한 레코드 형(asset location·library location).
  #expect(try names("/dictionary/suite/record-type") == ["asset location", "library location"])
  #expect(try codes("/dictionary/suite/record-type") == ["aslc", "lblc"])
  #expect(
    try names("/dictionary/suite/record-type[@name='asset location']/property")
      == ["folder", "base name", "has media", "has description"])
  #expect(
    try codes("/dictionary/suite/record-type[@name='asset location']/property")
      == ["asfd", "asbn", "ashm", "ashd"])
  #expect(
    try names("/dictionary/suite/record-type[@name='library location']/property")
      == ["library folder", "library base name", "has archive", "has library description"])
  #expect(
    try codes("/dictionary/suite/record-type[@name='library location']/property")
      == ["lbfd", "lbbn", "lbha", "lbhd"])

  // user defined record(usrf) — metadata·data options의 값 형.
  #expect(try names("/dictionary/suite/value-type") == ["user defined record"])
  #expect(try codes("/dictionary/suite/value-type") == ["usrf"])

  // make는 표준 corecrel을 이 suite에서 다시 선언한다 — 문서의 정본 매개변수.
  let makeCommand = try #require(
    try document.nodes(forXPath: "/dictionary/suite/command[@name='make']")
      .first as? XMLElement)
  #expect(makeCommand.attribute(forName: "code")?.stringValue == "corecrel")
  #expect(
    try names("/dictionary/suite/command[@name='make']/parameter")
      == ["new", "at", "with data", "with properties"])
  #expect(
    try codes("/dictionary/suite/command[@name='make']/parameter")
      == ["kocl", "insh", "data", "prdt"])
  let makeCocoa = try #require(
    try document.nodes(forXPath: "/dictionary/suite/command[@name='make']/cocoa")
      .first as? XMLElement)
  #expect(makeCocoa.attribute(forName: "class")?.stringValue == "MakeAssetCommand")
  let makeResult = try #require(
    try document.nodes(forXPath: "/dictionary/suite/command[@name='make']/result")
      .first as? XMLElement)
  #expect(makeResult.attribute(forName: "type")?.stringValue == "specifier")
  // open은 Cocoa scripting(NSApplication 위임)이 처리한다 — access group만 얹는다.
  #expect(try codes("/dictionary/suite/command[@name='open']") == ["aevtodoc"])

  // asset은 application의 요소다 — 창 없는 실행기의 컨테이너 계층.
  let element = try #require(
    try document.nodes(
      forXPath: "/dictionary/suite/class-extension[@name='application']/element"
    ).first as? XMLElement)
  #expect(element.attribute(forName: "type")?.stringValue == "asset")
  let elementCocoa = try #require(
    try document.nodes(
      forXPath: "/dictionary/suite/class-extension[@name='application']/element/cocoa"
    ).first as? XMLElement)
  #expect(elementCocoa.attribute(forName: "key")?.stringValue == "assets")

  // asset 클래스 — 문서가 정의한 여섯 속성(id부터 data options까지).
  let assetClass = try #require(
    try document.nodes(forXPath: "/dictionary/suite/class[@name='asset']")
      .first as? XMLElement)
  #expect(assetClass.attribute(forName: "code")?.stringValue == "aset")
  let assetCocoa = try #require(
    try document.nodes(forXPath: "/dictionary/suite/class[@name='asset']/cocoa")
      .first as? XMLElement)
  #expect(assetCocoa.attribute(forName: "class")?.stringValue == "MediaAssetProxy")
  let propertyNames = try names("/dictionary/suite/class[@name='asset']/property")
  #expect(
    propertyNames
      == ["id", "name", "location info", "library info", "metadata", "data options"])
  #expect(
    try codes("/dictionary/suite/class[@name='asset']/property")
      == ["ID  ", "pnam", "locn", "lbry", "meta", "dopt"])
  let modelProperties = MediaAssetProperty.allCases.map(\.rawValue)
  #expect(modelProperties == propertyNames)
  #expect(MediaAssetMakeCommand.make.rawValue == "make")

  // 코드 길이는 sdef DTD의 하드 한도다 — suite·클래스·속성·레코드는 4자,
  // 명령은 이벤트 클래스+ID가 붙은 8자(corecrel·aevtodoc)다.
  let fourCharacterCodeAxes = [
    "/dictionary/suite", "/dictionary/suite/record-type", "/dictionary/suite/value-type",
    "/dictionary/suite/command/parameter",
    "/dictionary/suite/class", "/dictionary/suite/class/property",
    "/dictionary/suite/record-type/property",
  ]
  for axis in fourCharacterCodeAxes {
    for code in try codes(axis) {
      #expect(code.count == 4, "4자 코드여야 한다: \(code) (\(axis))")
    }
  }
  for code in try codes("/dictionary/suite/command") {
    #expect(code.count == 8, "8자 코드(이벤트 클래스+ID)여야 한다: \(code)")
  }

  // 접근 그룹 — sandbox Final Cut이 자산·요소·make에 닿는 문서 요구사항.
  let accessGroups = try document.nodes(forXPath: "//access-group")
    .compactMap { ($0 as? XMLElement)?.attribute(forName: "identifier")?.stringValue }
  #expect(!accessGroups.isEmpty)
  #expect(accessGroups.allSatisfy { $0 == "com.apple.proapps.assetmanagement.import" })
  #expect(
    try document.nodes(
      forXPath: "/dictionary/suite/class[@name='asset']/access-group").count == 1)
  #expect(
    try document.nodes(
      forXPath: "/dictionary/suite/class-extension[@name='application']/element/access-group"
    ).count == 1)
  #expect(
    try document.nodes(
      forXPath: "/dictionary/suite/command[@name='make']/access-group").count == 1)
}

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-2 단위2 — V2 역할 계획 — 두 실행길의 운반 모양
// ─────────────────────────────────────────────────────────────

@Test func roleCredentialsPlanCarriesTheV2CarrierOnBothPaths() throws {
  /// 번들길은 사적 환경 운반자 한 통, 개발길은 stdin 봉투 — V1 운반자는
  /// 실리지 않는다(자식의 충돌 판정이 두 운반자를 금한다).
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let engineRoot = root.appendingPathComponent("Engine", isDirectory: true)
  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  let external = root.appendingPathComponent("external-job", isDirectory: true)
  let executable = engineRoot
    .appendingPathComponent("Contents", isDirectory: true)
    .appendingPathComponent("MacOS", isDirectory: true)
    .appendingPathComponent("vlogsrt-engine")
  try FileManager.default.createDirectory(
    at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  try Data("#!/bin/sh\n".utf8).write(to: executable)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
  // 개발 폴백도 실행 가능한 래퍼가 있어야 한다(S2A-2).
  let wrapper = scripts.appendingPathComponent(DevelopmentEngineInvocation.scriptName)
  try Data("#!/bin/sh\n".utf8).write(to: wrapper)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)

  let roles = EngineRoleCredentials(
    sttKey: "synthetic-stt-key",
    visionKey: "synthetic-vision-key",
    visionBaseURL: "https://vision.example/v1")

  guard case .bundled(let bundled) = EngineRuntime.resolvePlan(
    configuration: .release,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: engineRoot,
    applicationScriptsDirectory: nil,
    developmentExternalJob: nil,
    credentials: roles
  ) else {
    Issue.record("번들이 있으면 bundled여야 한다")
    return
  }
  // V2 운반자 한 통 — 값은 JSON 세 필드로만 간다.
  let carried = try #require(bundled.environment[BundledEngineInvocation.roleCarrierEnvironmentName])
  let decoded = try #require(
    JSONSerialization.jsonObject(with: Data(carried.utf8)) as? [String: String])
  #expect(decoded[EngineRoleCredentials.sttKeyField] == "synthetic-stt-key")
  #expect(decoded[EngineRoleCredentials.visionKeyField] == "synthetic-vision-key")
  #expect(decoded[EngineRoleCredentials.visionBaseURLField] == "https://vision.example/v1")
  // V1 운반자는 실리지 않는다 — 두 운반자가 같은 호출에 오면 자식이 닫는다.
  #expect(bundled.environment[BundledEngineInvocation.apiKeyEnvironmentName] == nil)
  #expect(
    bundled.environment[BundledEngineInvocation.pathEnvironmentName]
      == "\(engineRoot.standardizedFileURL.path)/bin:/usr/bin:/bin")

  guard case .development(let development) = EngineRuntime.resolvePlan(
    configuration: .debug,
    operation: .analyze,
    jobID: "job-42",
    appGroupJob: root,
    engineRoot: nil,
    applicationScriptsDirectory: scripts,
    developmentExternalJob: external,
    credentials: roles
  ) else {
    Issue.record("Debug 폴백이 development여야 한다")
    return
  }
  #expect(development.credentialEnvelope == .roles(roles))
  let joined = development.arguments.joined()
  #expect(!joined.contains("synthetic-stt-key"))
  #expect(!joined.contains("synthetic-vision-key"))
}
