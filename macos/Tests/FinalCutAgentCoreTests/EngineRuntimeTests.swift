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

  // Debug는 번들이 없을 때 개발 실행기로 되돌린다.
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

@Test func engineSessionGatesCancellationBeforeRunAndFailureSkipsSync() async throws {
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

  // 종료코드 비0 — 동기화 없이 실패로 닫는다.
  try FileManager.default.removeItem(at: job.appendingPathComponent(CancellationStore.fileName))
  var synced: [URL] = []
  let failed = try await EngineSessionRunner(executor: RecordingExecutor(status: 3)).run(
    _runtimeContext(plan: try makeBundledPlan(), registry: registry, revision: revision, appGroupJob: job),
    syncResults: { _, to in synced.append(to) }
  )
  #expect(failed == .engineFailed(3))
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
    syncResults: { _, to in synced.append(to) },
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
    syncResults: { _, to in
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
    syncResults: { _, to in synced.append(to) },
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
    #expect(line.contains("\"protocol_version\":\"1.2\""))
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

  let xml = root.appendingPathComponent("final.fcpxml")
  try Data("<fcpxml/>".utf8).write(to: xml)
  let bad = root.appendingPathComponent("bad.wav")
  try Data().write(to: bad)

  #expect(throws: ShareInboxError.self) {
    _ = try ShareInbox.receive(
      urls: [xml, bad],
      workspace: workspace,
      jobID: "caption-1",
      originRevision: String(repeating: "a", count: 64)
    )
  }
  #expect(
    !FileManager.default.fileExists(
      atPath: root.appendingPathComponent("Jobs").appendingPathComponent("caption-1").path))

  // 유효한 쌍만 잡을 만든다.
  let good = root.appendingPathComponent("good.wav")
  try Data(wavHeader(rate: 48_000)).write(to: good)
  let requestURL = try ShareInbox.receive(
    urls: [xml, good],
    workspace: workspace,
    jobID: "caption-2",
    originRevision: String(repeating: "a", count: 64)
  )
  #expect(requestURL.lastPathComponent == "caption-request.jsonl")
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

@Test func sdefDeclaresAssetClassWithPropertiesAndAMakeCommand() throws {
  let sdefURL = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .appendingPathComponent("macos/Resources/FinalCutAgent.sdef")
  let data = try Data(contentsOf: sdefURL)
  let document = try XMLDocument(data: data, options: [])
  let classes = try document.nodes(forXPath: "/dictionary/suite/class")
  #expect(classes.count == 1)
  let asset = try #require(classes.first as? XMLElement)
  #expect(asset.attribute(forName: "name")?.stringValue == "asset")
  let propertyNames = try document.nodes(forXPath: "/dictionary/suite/class/property")
    .compactMap { ($0 as? XMLElement)?.attribute(forName: "name")?.stringValue }
  #expect(
    propertyNames == ["asset location", "library location", "data options"])
  // 속성은 4자 코드와 cocoa 키를 가진다 — AppleScript가 잡아 쓰는 좌표다.
  for node in try document.nodes(forXPath: "/dictionary/suite/class/property") {
    let element = try #require(node as? XMLElement)
    #expect(element.attribute(forName: "code")?.stringValue?.count == 4)
    #expect(element.attribute(forName: "type")?.stringValue != nil)
  }
  // make는 명령이지 속성이 아니다 — asset을 명령으로 선언하지 않는다.
  let commands = try document.nodes(forXPath: "/dictionary/suite/command")
    .compactMap { ($0 as? XMLElement)?.attribute(forName: "name")?.stringValue }
  #expect(commands == ["make"])
  let modelProperties = MediaAssetProperty.allCases.map(\.rawValue)
  #expect(modelProperties == propertyNames)
  #expect(MediaAssetMakeCommand.make.rawValue == "make")
  // 코어 모델의 dataOptions 기본 — 분석은 원본 우선 512px.
  #expect(MediaDataOptions.analysis.maxLongEdge == 512)
}
