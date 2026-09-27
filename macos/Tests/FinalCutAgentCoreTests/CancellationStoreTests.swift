import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  S1A — 앱의 취소 기록이 엔진(Python)이 읽는 경로와 만난다
//
//  Python `vlogsrt.project.commands._check_cancellation`은 job 루트와
//  workspace 안의 cancellation.jsonl을 함께 읽는다(엔진 진입점이 루트를
//  VLOGSRT_ENGINE_JOB_DIR로 알린다). 아래 시험은 record가 그 경로들과 같은
//  payload로 연결되는지, workspace가 아직 없을 때도 안전한지를 못 박는다.
//  경로·바이트의 의미는 tests/project/test_workflow_cancellation.py의
//  잡 루트 시험이 파이썬 쪽에서 같은 뜻으로 확인한다.
// ─────────────────────────────────────────────────────────────

private func makeCancellationRequest(
  revision: String = String(repeating: "a", count: 64)
) -> CancellationRequestMessage {
  CancellationRequestMessage(
    inputRevision: revision,
    attemptID: "*",
    requestedAt: "2026-09-27T05:00:00Z",
    reason: "user",
    cancellationRevision: CancellationCodec.revision(
      inputRevision: revision,
      attemptID: "*",
      requestedAt: "2026-09-27T05:00:00Z",
      reason: "user"
    )
  )
}

@Test func recordConnectsAppAndDevelopmentJobsToEngineObservationPaths() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  // job 폴더를 미리 만들지 않는다 — record가 안전하게 만들어야 한다.
  let appGroupJob = root.appendingPathComponent("app-group-job", isDirectory: true)
  let externalJob = root.appendingPathComponent("Jobs", isDirectory: true)
    .appendingPathComponent("job-1", isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let request = makeCancellationRequest()
  try CancellationStore(appGroupJob: appGroupJob, externalJob: externalJob).record(request)
  let expected = try ProtocolCodec.encode(
    request,
    type: "CancellationRequest",
    version: ProtocolCodec.analysisVersion
  )

  for job in [appGroupJob, externalJob] {
    // 앱이 남긴 job 루트가 엔진 관측 경로의 첫째와 정확히 같은 곳이고 payload도 같다.
    let paths = CancellationStore.engineObservationPaths(jobURL: job)
    #expect(paths == [
      job.appendingPathComponent(CancellationStore.fileName),
      job.appendingPathComponent(CancellationStore.workspaceDirectoryName)
        .appendingPathComponent(CancellationStore.fileName),
    ])
    #expect(try String(contentsOf: paths[0], encoding: .utf8) == expected)
    // workspace는 엔진이 만든다 — 아직 없어도 루트 기록이 유실되지 않는다.
    #expect(!FileManager.default.fileExists(atPath: paths[1].path))
  }
}

@Test func recordSurvivesTheWorkspaceCreationRace() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("engine-job", isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let rootRecord = CancellationStore.engineObservationPaths(jobURL: job)[0]

  // workspace가 생기기 전에 남긴 취소 — 엔진이 workspace를 만든 뒤에도 그대로다.
  try CancellationStore(appGroupJob: job, externalJob: nil).record(makeCancellationRequest())
  let before = try Data(contentsOf: rootRecord)
  try FileManager.default.createDirectory(
    at: job.appendingPathComponent(CancellationStore.workspaceDirectoryName, isDirectory: true),
    withIntermediateDirectories: true)
  #expect(try Data(contentsOf: rootRecord) == before)

  // 엔진 실행 뒤 새로 남긴 취소도 같은 경로에 닿는다 — 마지막 요청이 남는다.
  let second = makeCancellationRequest(revision: String(repeating: "b", count: 64))
  try CancellationStore(appGroupJob: job, externalJob: nil).record(second)
  let expected = try ProtocolCodec.encode(
    second, type: "CancellationRequest", version: ProtocolCodec.analysisVersion)
  #expect(try String(contentsOf: rootRecord, encoding: .utf8) == expected)
}

@Test func recordWithSameAppGroupAndExternalJobWritesTheSingleLocation() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("engine-job", isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let request = makeCancellationRequest()
  try CancellationStore(appGroupJob: job, externalJob: job).record(request)
  let expected = try ProtocolCodec.encode(
    request, type: "CancellationRequest", version: ProtocolCodec.analysisVersion)
  #expect(
    try String(
      contentsOf: CancellationStore.engineObservationPaths(jobURL: job)[0], encoding: .utf8)
      == expected)
}
