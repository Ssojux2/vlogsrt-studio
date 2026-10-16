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

  // 키가 없어도 운반자는 **항시** 실린다(S2A-2): 빈 값은 '이 실행에는 키가
  // 없다'는 명시적 선언이라 자식 안에서 상속·dotenv의 낡은 키 부활을 닫는다.
  let offline = try BundledEngineInvocation.create(
    operation: .roughcut,
    jobID: "job-42",
    jobDirectory: jobDirectory,
    engineRoot: engineRoot,
    apiKey: nil
  )
  #expect(offline.environment[BundledEngineInvocation.apiKeyEnvironmentName] == "")
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

@Test func mediaAssetProtocolAnswersTheDocumentedRecords() throws {
  let folder = FileManager.default.temporaryDirectory
    .appendingPathComponent("share-staging", isDirectory: true)

  // asset location — XML 전용 내보내기의 답: 미디어는 받지 않고 FCPXML만 받는다.
  let location = AssetLocationRecord.xmlOnly(folder: folder, baseName: "에피소드 12")
  #expect(location.hasMedia == false)
  #expect(location.hasDescription == true)
  #expect(location.cocoaDictionary["basename"] as? String == "에피소드 12")
  #expect(location.cocoaDictionary["hasMedia"] as? Bool == false)
  #expect(location.cocoaDictionary["hasDescription"] as? Bool == true)
  #expect((location.cocoaDictionary["folder"] as? URL) == folder)

  // library location — 라이브러리 보관을 원하지 않는다는 명시적 답(플래그 둘뿐).
  let library = LibraryLocationRecord.none()
  #expect(library.hasArchive == false)
  #expect(library.hasLibraryDescription == false)
  #expect(library.cocoaDictionary.count == 2)
  #expect(library.cocoaDictionary["hasArchive"] as? Bool == false)
  #expect(library.cocoaDictionary["hasDescription"] as? Bool == false)

  // data options 답 — 후보에서 "None" 보기와 1.14에 가장 가까운 버전을 고른다.
  let reply = DataOptionsReply.choose(
    availableMetadataSets: ["Camera View", "None"],
    availableVersions: ["1.13", "1.14", "1.15"])
  #expect(reply.metadataViewName == "None")
  #expect(reply.dtdVersion == "1.14")
  // 1.14가 없으면 가장 가까운 아래(1.13)지 위(1.16)가 아니다.
  let fallback = DataOptionsReply.choose(
    availableMetadataSets: [], availableVersions: ["1.13", "1.16"])
  #expect(fallback.metadataViewName == nil)
  #expect(fallback.dtdVersion == "1.13")
  let empty = DataOptionsReply.choose(availableMetadataSets: [], availableVersions: [])
  #expect(empty.metadataViewName == nil)
  #expect(empty.dtdVersion == nil)
  #expect(reply.cocoaDictionary["metadataSet"] as? String == "None")
  #expect(reply.cocoaDictionary["descriptionVersion"] as? String == "1.14")

  // metadata 답 — 받은 키를 그대로 되돌리고 share.id(추적 ID)를 보탠다.
  let metadata = ShareMetadataReply.reply(
    received: ["com.apple.proapps.share.episodeID": "MyNewEpisode"],
    uniqueID: "asset-track-1")
  #expect(metadata["com.apple.proapps.share.episodeID"] as? String == "MyNewEpisode")
  #expect(metadata[ShareMetadataReply.shareIDKey] as? String == "asset-track-1")
}

@Test func shareDestinationReportsInstallStateAgainstTheBundledRevision() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let installed = root.appendingPathComponent("Share Destinations", isDirectory: true)
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
      .path.hasSuffix("Library/Application Support/ProApps/Share Destinations"))
}
