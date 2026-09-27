import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  프로토콜 1.2 — 취소·사용량·미리보기·자막 승인
// ─────────────────────────────────────────────────────────────

@Test func cancellationRevisionMatchesThePythonEngineDigest() {
  // Python: CancellationRequest.create(input_revision="a"*64,
  //   attempt_id="attempt-001", requested_at="2026-09-21T04:00:30Z", reason="user")
  let revision = CancellationCodec.revision(
    inputRevision: String(repeating: "a", count: 64),
    attemptID: "attempt-001",
    requestedAt: "2026-09-21T04:00:30Z",
    reason: "user"
  )
  #expect(
    revision == "6051b93a3cc78ca337b1424f34b2665a18b184ba34f2069caccbef0ce25df02f")
  #expect(CancellationCodec.allowedReasons == ["user", "active_project_changed"])
}

@Test func cancellationStoreWritesBothLocationsAndNeverDuplicates() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let appGroupJob = root.appendingPathComponent("app-group-job", isDirectory: true)
  let externalJob = root.appendingPathComponent("external-job", isDirectory: true)
  try FileManager.default.createDirectory(at: appGroupJob, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: externalJob, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let request = CancellationRequestMessage(
    inputRevision: String(repeating: "a", count: 64),
    attemptID: "attempt-001",
    requestedAt: "2026-09-21T04:00:30Z",
    reason: "user",
    cancellationRevision: CancellationCodec.revision(
      inputRevision: String(repeating: "a", count: 64),
      attemptID: "attempt-001",
      requestedAt: "2026-09-21T04:00:30Z",
      reason: "user"
    )
  )
  let store = CancellationStore(appGroupJob: appGroupJob, externalJob: externalJob)
  try store.record(request)

  let expectedLine = try ProtocolCodec.encode(
    request,
    type: "CancellationRequest",
    version: ProtocolCodec.analysisVersion
  )
  for base in [appGroupJob, externalJob] {
    let written = try String(contentsOf: base.appendingPathComponent("cancellation.jsonl"), encoding: .utf8)
    #expect(written == expectedLine)
    #expect(written.contains("\"protocol_version\":\"\(ProtocolCodec.analysisVersion)\""))
  }

  // 외부 쪽 기록을 App Group으로 가져온다 — 이미 있으면 덮어쓰지 않는다.
  try FileManager.default.removeItem(at: appGroupJob.appendingPathComponent("cancellation.jsonl"))
  try store.pullExternal()
  #expect(
    FileManager.default.fileExists(atPath: appGroupJob.appendingPathComponent("cancellation.jsonl").path))
}

@Test func cancelledJobsBlockResultSyncButKeepTheRecord() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let external = root.appendingPathComponent("external-job", isDirectory: true)
  let appGroup = root.appendingPathComponent("app-group-job", isDirectory: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: appGroup, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  try Data("roughcut".utf8).write(to: external.appendingPathComponent("roughcut.fcpxml"))

  #expect(!CancellationStore.isCancelled(jobURL: external))
  try DevelopmentJobStager.syncResults(from: external, to: appGroup)
  #expect(
    FileManager.default.fileExists(atPath: appGroup.appendingPathComponent("roughcut.fcpxml").path))

  // 취소 기록이 있으면 동기화가 닫힌다 — 기록 자체는 지우지 않는다.
  try Data("cancelled\n".utf8)
    .write(to: external.appendingPathComponent(CancellationStore.fileName))
  #expect(CancellationStore.isCancelled(jobURL: external))
  #expect(throws: DevelopmentEngineError.cancelled) {
    try DevelopmentJobStager.syncResults(from: external, to: appGroup)
  }
  #expect(
    FileManager.default.fileExists(atPath: external.appendingPathComponent(CancellationStore.fileName).path))
}

@Test func bookmarkStoreSavesUpdatesDeletesAndRejectsSymlinks() throws {
  let suiteName = "test.bookmarks.\(UUID().uuidString)"
  defer { UserDefaults.standard.removePersistentDomain(forName: suiteName) }
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let media = root.appendingPathComponent("media", isDirectory: true)
  try FileManager.default.createDirectory(at: media, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let store = try SecurityScopedBookmarkStore(suiteName: suiteName)
  try store.save(media, key: "media-root.a")
  #expect(store.storedKeys(prefix: "media-root.") == ["media-root.a"])
  #expect(try store.resolve(key: "media-root.a") != nil)
  store.delete(key: "media-root.a")
  #expect(store.storedKeys(prefix: "media-root.") == [])
  #expect(try store.resolve(key: "media-root.a") == nil)

  let alias = root.appendingPathComponent("alias")
  try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: media)
  #expect(throws: BookmarkError.self) {
    try store.save(alias, key: "media-root.symlink")
  }
}

@Test func bookmarkContainmentAndGrantRevisionBindRootsAndMediaHashes() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let mediaRoot = root.appendingPathComponent("media", isDirectory: true)
  let clip = mediaRoot.appendingPathComponent("clip.mov")
  let outside = root.appendingPathComponent("outside.mov")
  try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
  try Data("media".utf8).write(to: clip)
  try Data("outside".utf8).write(to: outside)
  defer { try? FileManager.default.removeItem(at: root) }

  #expect(SecurityScopedBookmarkStore.contains(clip, under: [mediaRoot]))
  #expect(!SecurityScopedBookmarkStore.contains(outside, under: [mediaRoot]))
  #expect(!SecurityScopedBookmarkStore.contains(mediaRoot, under: [mediaRoot]))

  let base = SecurityScopedBookmarkStore.grantRevision(
    roots: [mediaRoot], mediaSHA256s: [String(repeating: "a", count: 64)])
  #expect(
    base != SecurityScopedBookmarkStore.grantRevision(
      roots: [mediaRoot], mediaSHA256s: [String(repeating: "b", count: 64)]))
  #expect(base.count == 64)
}

@Test func legacyJobsPreserveOnly11RecordsAndNeverTouch12Jobs() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let jobsRoot = root.appendingPathComponent("Jobs", isDirectory: true)
  let legacy = jobsRoot.appendingPathComponent("job-1.1", isDirectory: true)
  let current = jobsRoot.appendingPathComponent("job-1.2", isDirectory: true)
  try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: current, withIntermediateDirectories: true)
  // marker 내용이 분류를 정한다 — 1.1만 legacy다.
  try Data("{\"protocol_version\":\"1.1\"}\n".utf8)
    .write(to: legacy.appendingPathComponent("analysis-estimate.jsonl"))
  try Data("{\"protocol_version\":\"1.2\"}\n".utf8)
    .write(to: current.appendingPathComponent("analysis-estimate.jsonl"))
  defer { try? FileManager.default.removeItem(at: root) }

  let workspace = AppGroupWorkspace(rootURL: root)
  let store = LegacyJobStore(workspace: workspace)
  #expect(LegacyJobStore.isLegacyProtocolVersion(legacy))
  #expect(!LegacyJobStore.isLegacyProtocolVersion(current))
  #expect(try store.pendingLegacyJobIDs(in: jobsRoot) == ["job-1.1"])
  try store.preserve(jobID: "job-1.1", from: jobsRoot)
  #expect(store.preservedJobIDs() == ["job-1.1"])
  #expect(try store.pendingLegacyJobIDs(in: jobsRoot) == [])
  #expect(throws: LegacyJobError.self) {
    try store.preserve(jobID: "job-1.1", from: jobsRoot)
  }
  // 1.2 작업은 보존 대상이 아니고 원본도 그대로다.
  #expect(
    !FileManager.default.fileExists(
      atPath: store.rootURL.appendingPathComponent("job-1.2").path))
  #expect(
    FileManager.default.fileExists(
      atPath: current.appendingPathComponent("analysis-estimate.jsonl").path))
  // 원본은 그대로 남는다 — 보존은 복사다.
  #expect(
    FileManager.default.fileExists(atPath: legacy.appendingPathComponent("analysis-estimate.jsonl").path))
}

@Test func bundledEngineInvocationCarriesTheKeyAndBundledToolPathOnly() throws {
  let engineRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let jobDirectory = FileManager.default.temporaryDirectory
    .appendingPathComponent("jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  let invocation = try BundledEngineInvocation.create(
    operation: .analyze,
    jobID: "job-42",
    jobDirectory: jobDirectory,
    engineRoot: engineRoot,
    apiKey: "secret-value"
  )
  #expect(
    invocation.executableURL.path.hasSuffix("Contents/MacOS/vlogsrt-engine"))
  #expect(invocation.arguments == ["analyze", "job-42", jobDirectory.standardizedFileURL.path])
  #expect(BundledEngineInvocation.apiKeyEnvironmentName == "VLOGSRT_ENGINE_API_KEY")
  #expect(invocation.currentDirectory == engineRoot)
  // 자식 환경은 키 한 통과 도구 PATH뿐이다 — 부모 환경을 상속하지 않는다.
  #expect(
    invocation.environment[BundledEngineInvocation.apiKeyEnvironmentName] == "secret-value")
  let path = try #require(invocation.environment[BundledEngineInvocation.pathEnvironmentName])
  #expect(path == "\(engineRoot.standardizedFileURL.path)/bin:/usr/bin:/bin")
  // Release 폴백 금지의 실행 지점 — Homebrew 경로가 PATH에 없어야 한다.
  #expect(!path.contains("/opt/homebrew"))
  #expect(path.hasSuffix(":/usr/bin:/bin"))

  // 키가 없으면(오프라인 실행) 키 변수 자체가 없다 — 빈 값으로 오인하지 않는다.
  let offline = try BundledEngineInvocation.create(
    operation: .roughcut,
    jobID: "job-42",
    jobDirectory: jobDirectory,
    engineRoot: engineRoot,
    apiKey: nil
  )
  #expect(offline.environment[BundledEngineInvocation.apiKeyEnvironmentName] == nil)
  #expect(offline.environment[BundledEngineInvocation.pathEnvironmentName] != nil)
  #expect(
    BundledEngineInvocation.bundledToolPath(engineRoot: engineRoot)
      == "\(engineRoot.standardizedFileURL.path)/bin:/usr/bin:/bin")

  #expect(EngineLaunchMode.resolve(bundleURL: nil) == .development)
  #expect(EngineLaunchMode.resolve(bundleURL: engineRoot) == .development)

  let contents = engineRoot.appendingPathComponent("Contents", isDirectory: true)
    .appendingPathComponent("MacOS", isDirectory: true)
  try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
  let executable = contents.appendingPathComponent("vlogsrt-engine")
  try Data("#!/bin/sh\nexit 0\n".utf8).write(to: executable)
  try FileManager.default.setAttributes(
    [.posixPermissions: 0o755], ofItemAtPath: executable.path)
  defer { try? FileManager.default.removeItem(at: engineRoot) }
  #expect(EngineLaunchMode.resolve(bundleURL: engineRoot) == .bundled)
}

@Test func shareValidationRejectsEmptyAndWrongRateAudio() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let xml = root.appendingPathComponent("final.fcpxml")
  try Data("<fcpxml/>".utf8).write(to: xml)

  let empty = root.appendingPathComponent("empty.wav")
  try Data().write(to: empty)
  #expect(throws: ShareInboxError.self) {
    try ShareInbox.validate(SharedExport(audio: empty, fcpxml: xml))
  }

  // 44.1kHz PCM WAV — 엔진 계약(48kHz) 위반.
  let wrongRate = root.appendingPathComponent("wrong.wav")
  try Data(wavHeader(rate: 44_100)).write(to: wrongRate)
  #expect(throws: ShareInboxError.self) {
    try ShareInbox.validate(SharedExport(audio: wrongRate, fcpxml: xml))
  }

  let good = root.appendingPathComponent("good.wav")
  try Data(wavHeader(rate: 48_000)).write(to: good)
  try ShareInbox.validate(SharedExport(audio: good, fcpxml: xml))
}

/// 최소 WAV 헤더 — PCM 1채널, 주어진 샘플 레이트.
func wavHeader(rate: UInt32) -> Data {
  var data = Data()
  data.append(Data("RIFF".utf8))
  data.append(Data([0x24, 0x00, 0x00, 0x00]))
  data.append(Data("WAVE".utf8))
  data.append(Data("fmt ".utf8))
  data.append(Data([0x10, 0x00, 0x00, 0x00]))
  data.append(Data([0x01, 0x00]))  // PCM
  data.append(Data([0x01, 0x00]))  // mono
  withUnsafeBytes(of: rate.littleEndian) { data.append(contentsOf: $0) }
  data.append(Data([0x00, 0x00, 0x00, 0x00]))  // byte rate placeholder
  data.append(Data([0x02, 0x00]))  // block align
  data.append(Data([0x10, 0x00]))  // bits per sample
  data.append(Data("data".utf8))
  data.append(Data([0x00, 0x00, 0x00, 0x00]))  // data chunk size
  return data
}

@Test func mediaAssetProtocolValidatesLocationsAndCreations() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let mediaRoot = root.appendingPathComponent("media", isDirectory: true)
  let clip = mediaRoot.appendingPathComponent("clip.mov")
  try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
  try Data("media".utf8).write(to: clip)
  defer { try? FileManager.default.removeItem(at: root) }

  let inside = MediaAssetLocation.validate(assetID: "a1", fileURL: clip, allowedRoots: [mediaRoot])
  #expect(inside.isUnderAllowedRoot)
  let outside = MediaAssetLocation.validate(
    assetID: "a1", fileURL: root.appendingPathComponent("outside.mov"), allowedRoots: [mediaRoot])
  #expect(!outside.isUnderAllowedRoot)

  let creation = try MediaAssetCreation.validate(assetID: "render-1", fileURL: clip)
  #expect(creation.assetID == "render-1")
  #expect(throws: MediaAssetProtocolError.self) {
    _ = try MediaAssetCreation.validate(assetID: "", fileURL: clip)
  }
  #expect(MediaDataOptions.analysis.maxLongEdge == 512)
  #expect(MediaDataOptions.analysis.preferProxy == false)
}

@Test func shareDestinationReportsInstallStateAgainstTheBundledRevision() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let installed = root.appendingPathComponent("ShareTemplates", isDirectory: true)
  try FileManager.default.createDirectory(at: installed, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let destination = ShareDestination(
    bundledURL: FileManager.default.temporaryDirectory,  // 개정 비교만 한다 — 존재 여부가 아니다
    installedDirectory: installed
  )
  #expect(destination.installState() == .notInstalled)

  let bundle = installed.appendingPathComponent(ShareDestination.bundleName, isDirectory: true)
  try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: true)
  try Data("<plist/>".utf8).write(to: bundle.appendingPathComponent("Info.plist"))
  let state = destination.installState()
  guard case .installed(let matches) = state else {
    Issue.record("설치 상태여야 한다: \(state)")
    return
  }
  #expect(matches == false)

  #expect(
    ShareDestination.defaultInstalledDirectory()
      .path.hasSuffix("Library/Application Support/ProApps/ShareTemplates"))
}
