import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S2C/S4B — 러프컷 배달 수명주기·원본 매핑·자막 조정자 회귀
// ─────────────────────────────────────────────────────────────

// ─── 원본 지도 — 재시동을 건너 살고, 모호하면 실패로 닫힌다 ────────

@Test func originMapLookupSurvivesRelaunchAndFailsClosedOnAmbiguity() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let fileURL = root.appendingPathComponent("origin-map.jsonl")

  let first = OriginMapStore(fileURL: fileURL)
  try first.record(
    OriginMapping(
      projectUID: "UID-A", originJobID: "roundtrip-1",
      originRevision: String(repeating: "a", count: 64), recordedAt: "2026-10-01T01:00:00Z"))
  try first.record(
    OriginMapping(
      projectUID: "UID-B", originJobID: "roundtrip-2",
      originRevision: String(repeating: "b", count: 64), recordedAt: "2026-10-01T02:00:00Z"))

  // 재시동 뒤의 새 저장소 인스턴스 — 파일이 정본이다.
  let relaunched = OriginMapStore(fileURL: fileURL)
  #expect(try relaunched.lookup(projectUID: "UID-A")?.originJobID == "roundtrip-1")
  #expect(try relaunched.lookup(projectUID: "UID-B")?.originJobID == "roundtrip-2")
  #expect(try relaunched.lookup(projectUID: "UID-C") == nil)

  // 마지막 확정이 정본 — 같은 UID의 재확정은 갱신이다.
  try relaunched.record(
    OriginMapping(
      projectUID: "UID-A", originJobID: "roundtrip-1",
      originRevision: String(repeating: "c", count: 64), recordedAt: "2026-10-01T03:00:00Z"))
  #expect(try relaunched.lookup(projectUID: "UID-A")?.originRevision == String(repeating: "c", count: 64))

  // 같은 UID가 서로 다른 원본을 가리키면 모호다 — 조용히 고르지 않는다.
  try relaunched.record(
    OriginMapping(
      projectUID: "UID-B", originJobID: "roundtrip-9",
      originRevision: String(repeating: "d", count: 64), recordedAt: "2026-10-01T04:00:00Z"))
  #expect(throws: OriginMapError.self) {
    _ = try relaunched.lookup(projectUID: "UID-B")
  }
}

// ─── 배달 수명주기 — 전송 성공은 확인이 아니다 ────────────────────

private func _deliveryJob() throws -> URL {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  try Data("roughcut-xml".utf8).write(
    to: root.appendingPathComponent(RoundTripJob.roughcutFileName))
  return root
}

@Test func roughcutImportSessionRecordsTheLifecycleInOrder() async throws {
  let jobURL = try _deliveryJob()
  defer { try? FileManager.default.removeItem(at: jobURL) }
  let session = RoughcutImportSession(jobID: "roundtrip-1", jobURL: jobURL)

  try session.recordGenerated(inputRevision: String(repeating: "a", count: 64))
  #expect(RoughcutDeliveryStore.current(jobURL: jobURL)?.state == .generated)

  let sent = try await session.send(inputRevision: String(repeating: "a", count: 64)) { _ in }
  #expect(sent)
  #expect(RoughcutDeliveryStore.current(jobURL: jobURL)?.state == .importSent)
  // 아직 확인 아니다 — 결과 프로젝트 UID는 실려 있지 않다.
  #expect(RoughcutDeliveryStore.current(jobURL: jobURL)?.projectUID == nil)

  // 전송 실패는 기록으로 남고 원인 예외는 그대로 올라간다.
  struct SendFailure: Error {}
  await #expect(throws: SendFailure.self) {
    _ = try await session.send(inputRevision: String(repeating: "a", count: 64)) { _ in
      throw SendFailure()
    }
  }
  #expect(RoughcutDeliveryStore.current(jobURL: jobURL)?.state == .importFailed)
}

@Test func roughcutImportRefusesToSendChangedOrConfirmedOutput() async throws {
  let jobURL = try _deliveryJob()
  defer { try? FileManager.default.removeItem(at: jobURL) }
  let session = RoughcutImportSession(jobID: "roundtrip-1", jobURL: jobURL)
  try session.recordGenerated(inputRevision: String(repeating: "a", count: 64))

  // 출력이 바뀌면 해시가 다르다 — 자동 재전송 금지(S2C).
  try Data("tampered".utf8).write(
    to: jobURL.appendingPathComponent(RoundTripJob.roughcutFileName))
  let refused = try await session.send(inputRevision: String(repeating: "a", count: 64)) { _ in }
  #expect(!refused)
  #expect(RoughcutDeliveryStore.current(jobURL: jobURL)?.state == .generated)

  // 확인된 배달은 다시 보내지 않는다.
  try Data("roughcut-xml".utf8).write(
    to: jobURL.appendingPathComponent(RoundTripJob.roughcutFileName))
  _ = try await session.send(inputRevision: String(repeating: "a", count: 64)) { _ in }
  try RoughcutConfirmation.confirm(
    jobID: "roundtrip-1", jobURL: jobURL, activeProjectUID: "RESULT-UID",
    originMap: OriginMapStore(fileURL: jobURL.appendingPathComponent("map.jsonl")))
  #expect(RoughcutDeliveryStore.current(jobURL: jobURL)?.state == .importConfirmed)
  let again = try await session.send(inputRevision: String(repeating: "a", count: 64)) { _ in }
  #expect(!again)
}

@Test func roughcutConfirmationBindsObservedIdentityAndWritesOriginMap() throws {
  let jobURL = try _deliveryJob()
  defer { try? FileManager.default.removeItem(at: jobURL) }
  let mapFile = jobURL.appendingPathComponent("map.jsonl")
  let session = RoughcutImportSession(jobID: "roundtrip-7", jobURL: jobURL)
  try session.recordGenerated(inputRevision: String(repeating: "a", count: 64))

  // 원본 프로젝트가 열려 있으면 확인이 아니다 — 결과 프로젝트여야 한다.
  #expect(!RoughcutConfirmation.eligible(activeProjectUID: "SOURCE", sourceProjectUID: "SOURCE"))
  #expect(!RoughcutConfirmation.eligible(activeProjectUID: nil, sourceProjectUID: "SOURCE"))
  #expect(RoughcutConfirmation.eligible(activeProjectUID: "RESULT", sourceProjectUID: "SOURCE"))

  try RoughcutConfirmation.confirm(
    jobID: "roundtrip-7", jobURL: jobURL, activeProjectUID: "RESULT",
    originMap: OriginMapStore(fileURL: mapFile))
  let record = RoughcutDeliveryStore.current(jobURL: jobURL)
  #expect(record?.state == .importConfirmed)
  #expect(record?.projectUID == "RESULT")
  // 확정은 원본 지도를 남긴다 — 자막 공유 받기(S4B)의 조회처다.
  #expect(
    try OriginMapStore(fileURL: mapFile).lookup(projectUID: "RESULT")?.originJobID
      == "roundtrip-7")
  #expect(
    try OriginMapStore(fileURL: mapFile).lookup(projectUID: "RESULT")?.originRevision
      == String(repeating: "a", count: 64))
}

// ─── 자막 조정자 — 추정은 무자격, 실행은 승인·STT를 요구한다 ────────

private func _captionFixture() throws -> (
  root: URL, job: RoundTripJob, scripts: URL, externalRoot: URL, appGroup: AppGroupWorkspace
) {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  let appGroup = AppGroupWorkspace(rootURL: root)
  let job = try RoundTripJob(workspace: appGroup, id: "caption-1")
  try FileManager.default.createDirectory(at: job.directoryURL, withIntermediateDirectories: true)
  for (name, bytes) in [
    ("final.wav", Data("wav".utf8)),
    (
      "final.fcpxml",
      Data(
        """
        <fcpxml version="1.14"><library><event name="E">\
        <project name="P" uid="FINAL-UID-1"><sequence duration="1s"/></project>\
        </event></library></fcpxml>
        """.utf8)
    ),
    (
      "caption-origin.json",
      Data(
        """
        {"origin_job_id":"roundtrip-7","origin_revision":"\(String(repeating: "a", count: 64))","project_uid":"FINAL-UID-1"}
        """.utf8)
    ),
  ] {
    try bytes.write(to: job.directoryURL.appendingPathComponent(name))
  }
  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  let wrapper = scripts.appendingPathComponent(DevelopmentEngineInvocation.scriptName)
  try Data("#!/bin/sh\n".utf8).write(to: wrapper)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
  let externalRoot = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: externalRoot, withIntermediateDirectories: true)
  return (root, job, scripts, externalRoot, appGroup)
}

@MainActor
private func _captionDeps(
  fixture: (root: URL, job: RoundTripJob, scripts: URL, externalRoot: URL, appGroup: AppGroupWorkspace),
  executor: any EngineExecuting,
  openAIKeyPresent: Bool = true
) -> CaptionJobCoordinator.Dependencies {
  CaptionJobCoordinator.Dependencies(
    peekDevelopmentWorkspace: {
      SecurityScopedBookmarkStore.PeekedBookmark(
        key: "developmentEngineWorkspace", url: fixture.externalRoot, isStale: false)
    },
    settleDevelopmentWorkspace: { $0?.url },
    startScope: { _ in true },
    stopScope: { _ in },
    stageCaptionJob: { jobID, appGroupJob, externalRoot in
      try DevelopmentJobStager.stageCaption(
        jobID: jobID, appGroupJob: appGroupJob, externalRoot: externalRoot)
    },
    executor: executor,
    readiness: { _ in
      EngineReadinessReport(
        engine: "vlogsrt-engine", readinessVersion: 1, pythonExecutable: nil,
        pythonVersion: nil, ffmpeg: nil, ffprobe: nil, openAIKeyPresent: openAIKeyPresent)
    })
}

@Test @MainActor func captionEstimateRunsKeylessThroughTheCaptionStager() async throws {
  let fixture = try _captionFixture()
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let capture = PlanCapture()
  let deps = _captionDeps(fixture: fixture, executor: capture, openAIKeyPresent: false)
  // 키 보고가 없어도 추정은 돈다 — 전사기도 키도 필요 없다(S4A/S4B).
  let outcome = try await CaptionJobCoordinator(dependencies: deps).run(
    operation: .captionEstimate,
    job: fixture.job,
    configuration: .debug,
    engineRoot: nil,
    applicationScriptsDirectory: fixture.scripts)
  #expect(outcome == .completed)
  let callCount = await capture.plans.count
  #expect(callCount == 1)
  // 자막 스테이징은 input.fcpxml 없이 최종 내보내기만 옮긴다(S4A).
  let externalJob = fixture.externalRoot.appendingPathComponent("Jobs/caption-1")
  #expect(
    FileManager.default.fileExists(atPath: externalJob.appendingPathComponent("final.wav").path))
  #expect(
    FileManager.default.fileExists(
      atPath: externalJob.appendingPathComponent("caption-origin.json").path))
  #expect(
    !FileManager.default.fileExists(
      atPath: externalJob.appendingPathComponent("input.fcpxml").path))
}

@Test @MainActor func captionRunRequiresAuthorizationAndADevKey() async throws {
  let fixture = try _captionFixture()
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let capture = PlanCapture()

  // 승인 문서가 없으면 실행은 열리지 않는다 — 호출 0회.
  let noAuthorization = _captionDeps(fixture: fixture, executor: capture)
  await #expect(throws: EnginePreflightError.self) {
    _ = try await CaptionJobCoordinator(dependencies: noAuthorization).run(
      operation: .captionRun,
      job: fixture.job,
      configuration: .debug,
      engineRoot: nil,
      applicationScriptsDirectory: fixture.scripts)
  }
  let beforeAuth = await capture.plans.count
  #expect(beforeAuth == 0)

  // 승인 문서가 있어도 개발 .env 키 유무가 없으면(패널 계약) 유료 실행은 닫힌다.
  try Data("authorized".utf8).write(
    to: fixture.job.directoryURL.appendingPathComponent("caption-authorization.jsonl"))
  let noKey = _captionDeps(fixture: fixture, executor: capture, openAIKeyPresent: false)
  await #expect(throws: EnginePreflightError.self) {
    _ = try await CaptionJobCoordinator(dependencies: noKey).run(
      operation: .captionRun,
      job: fixture.job,
      configuration: .debug,
      engineRoot: nil,
      applicationScriptsDirectory: fixture.scripts)
  }
  let beforeReady = await capture.plans.count
  #expect(beforeReady == 0)

  // 승인 + 키 유무 보고가 함께면 실행이 열린다.
  let ready = _captionDeps(fixture: fixture, executor: capture, openAIKeyPresent: true)
  let outcome = try await CaptionJobCoordinator(dependencies: ready).run(
    operation: .captionRun,
    job: fixture.job,
    configuration: .debug,
    engineRoot: nil,
    applicationScriptsDirectory: fixture.scripts)
  #expect(outcome == .completed)
  let runCalls = await capture.plans.count
  #expect(runCalls == 1)
}

@Test func agentURLCarriesTheImportHost() throws {
  let url = try AgentURL.importURL(jobID: "roundtrip-7")
  #expect(url.absoluteString == "vlogsrt-agent://import/roundtrip-7")
  #expect(try AgentURL.request(url) == .importJob("roundtrip-7"))
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.request(URL(string: "vlogsrt-agent://unknown/job-1")!)
  }
}
