import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S4B 패널 계약 회귀 — 인박스·UID 매핑·무자동 실행·승인·재시도·공유 키
// ─────────────────────────────────────────────────────────────

/// ShareInbox.validate를 통과하는 최소 48kHz PCM WAV.
private func demoWAV(seconds: Int = 4) -> [UInt8] {
  var header = Array("RIFF".utf8) + [0, 0, 0, 0] + Array("WAVE".utf8)
  header += Array("fmt ".utf8) + [16, 0, 0, 0] + [1, 0] + [1, 0]
  header += [0x80, 0xBB, 0, 0]  // 48000Hz
  header += [0x00, 0x77, 1, 0] + [2, 0] + [16, 0]
  header += Array("data".utf8)
  header += [0, 0, 0, 0]
  header += [UInt8](repeating: 0, count: 2 * 48_000 * seconds)
  return header
}

private func demoFCPXML(uid: String, seconds: Int = 4) -> Data {
  Data(
    """
    <?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\
    <fcpxml version="1.14"><resources/>\
    <library><event name="데모"><project name="프로토타입" uid="\(uid)">\
    <sequence format="r1" duration="\(seconds)s" tcStart="0s"><spine/>\
    </sequence></project></event></library></fcpxml>
    """.utf8)
}

private func demoShareFiles(root: URL, uid: String) throws -> (wav: URL, xml: URL) {
  let incoming = root.appendingPathComponent("incoming", isDirectory: true)
  try FileManager.default.createDirectory(at: incoming, withIntermediateDirectories: true)
  let wav = incoming.appendingPathComponent("mix.wav")
  let xml = incoming.appendingPathComponent("edit.fcpxml")
  try Data(demoWAV()).write(to: wav)
  try demoFCPXML(uid: uid).write(to: xml)
  return (wav, xml)
}

private func demoOrigin(uid: String, jobID: String = "roundtrip-7") -> OriginMapping {
  OriginMapping(
    projectUID: uid, originJobID: jobID,
    originRevision: String(repeating: "a", count: 64),
    recordedAt: "2026-10-01T00:00:00Z")
}

// ─── 인박스 — 스테이징만 한다(자동 실행의 길이 없다) ────────────────

@Test func captionInboxStagesKeyedByUIDWithoutCreatingJobs() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let files = try demoShareFiles(root: root, uid: "FINAL-UID-1")

  let entry = try CaptionInbox.stage(urls: [files.wav, files.xml], workspace: workspace)
  #expect(entry.projectUID == "FINAL-UID-1")
  // job은커녕 추정 기록도 없다 — 공유 받기는 스테이징만 한다.
  #expect(try CaptionInbox.entries(workspace: workspace) == [entry])
  #expect(
    !FileManager.default.fileExists(
      atPath: root.appendingPathComponent("Jobs").appendingPathComponent("caption-1").path))

  // 같은 UID의 재공유는 마지막이 정본이다 — 항목은 하나로 남는다.
  let again = try CaptionInbox.stage(urls: [files.wav, files.xml], workspace: workspace)
  #expect(try CaptionInbox.entries(workspace: workspace) == [again])
  #expect(try CaptionInbox.entry(forProjectUID: "FINAL-UID-1", workspace: workspace) != nil)
  #expect(try CaptionInbox.entry(forProjectUID: "OTHER", workspace: workspace) == nil)

  // 검증 실패(빈 WAV)는 인박스에 들어가지 않는다.
  let empty = root.appendingPathComponent("empty.wav")
  try Data().write(to: empty)
  #expect(throws: ShareInboxError.self) {
    _ = try CaptionInbox.stage(urls: [files.xml, empty], workspace: workspace)
  }
}

// ─── 추정 요청 — UID 매핑을 resolves 하고 추정만 연다 ───────────────

@Test @MainActor func captionPanelIntakeStagesJobAndOnlyOpensEstimate() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let files = try demoShareFiles(root: root, uid: "FINAL-UID-1")
  _ = try CaptionInbox.stage(urls: [files.wav, files.xml], workspace: workspace)
  try OriginMapStore(workspace: workspace).record(demoOrigin(uid: "FINAL-UID-1"))

  let model = CaptionPanelModel(workspace: workspace)
  let action = try model.requestEstimate(activeProjectUID: "FINAL-UID-1")

  // 유료 실행으로 가는 URL은 이 동작이 만들지 않는다 — 추정(무료)만.
  guard case .openCaptionEstimate(let jobID) = action else {
    Issue.record("추정 요청이 추정 URL을 돌려주지 않았다: \(action)")
    return
  }
  #expect(action.url?.host == "caption-estimate")
  let jobURL = workspace.rootURL.appendingPathComponent("Jobs").appendingPathComponent(jobID)
  #expect(
    FileManager.default.fileExists(atPath: jobURL.appendingPathComponent("final.wav").path))
  #expect(
    FileManager.default.fileExists(atPath: jobURL.appendingPathComponent("final.fcpxml").path))
  let originDocument = try JSONSerialization.jsonObject(
    with: Data(contentsOf: jobURL.appendingPathComponent("caption-origin.json"))
  ) as? [String: String]
  #expect(originDocument?["origin_job_id"] == "roundtrip-7")
  #expect(originDocument?["project_uid"] == "FINAL-UID-1")
  // 승인 문서가 없으면 실행은 열리지 않는다 — 자동 유료 실행의 길이 없다.
  #expect(
    !FileManager.default.fileExists(
      atPath: jobURL.appendingPathComponent("caption-authorization.jsonl").path))
  // 인박스는 소비됐다 — 같은 내보내기의 이중 스테이징이 없다.
  #expect(try CaptionInbox.entries(workspace: workspace).isEmpty)

  // 매핑이 없는 UID는 폐쇄로 닫힌다 — 인박스 항목은 남아 나중에 다시 시도된다.
  let unmapped = try demoShareFiles(root: root, uid: "FINAL-UID-2")
  _ = try CaptionInbox.stage(urls: [unmapped.wav, unmapped.xml], workspace: workspace)
  #expect(throws: CaptionPanelError.self) {
    _ = try model.requestEstimate(activeProjectUID: "FINAL-UID-2")
  }
  #expect(try CaptionInbox.entries(workspace: workspace).count == 1)
}

// ─── 승인·재시도 — 승인은 추정에 묶이고 재시도는 문서를 다시 쓰지 않는다 ──

@Test @MainActor func captionPanelAuthorizationIsEstimateBoundAndRetryReusesIt() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let job = try RoundTripJob(workspace: workspace, id: "caption-1")
  try FileManager.default.createDirectory(at: job.directoryURL, withIntermediateDirectories: true)

  // 재시도는 승인 문서가 없으면 닫힌다 — 승인 없는 실행이 없다.
  #expect(throws: CaptionPanelError.self) {
    _ = try CaptionPanelModel(workspace: workspace).retryRun(job: job)
  }

  // 추정(엔진이 쓰는 모양 그대로)을 job에 심는다.
  let estimate = CaptionEstimateMessage(
    requestRevision: String(repeating: "1", count: 64),
    originRevision: String(repeating: "a", count: 64),
    finalRevision: String(repeating: "2", count: 64),
    renderAudioSHA256: String(repeating: "3", count: 64),
    audioSeconds: "4",
    transcriptionSeconds: "8",
    costUpperBound: "0.001",
    pricingSnapshotRevision: String(repeating: "4", count: 64),
    settingsSHA256: String(repeating: "5", count: 64),
    estimateRevision: String(repeating: "6", count: 64))
  let estimateLine = try ProtocolCodec.encode(
    estimate, type: "CaptionEstimate", version: ProtocolCodec.captionVersion)
  try Data("\(estimateLine)\n".utf8).write(to: job.captionEstimateURL)

  let model = CaptionPanelModel(workspace: workspace)
  let run = try model.authorizeRun(job: job)
  #expect(run.url?.host == "caption-run")
  let authorized = try String(contentsOf: job.captionAuthorizationURL, encoding: .utf8)
  #expect(authorized.contains(estimate.estimateRevision))
  #expect(authorized.contains(estimate.requestRevision))

  // 재시도는 같은 승인 문서를 그대로 둔다 — 바이트가 바뀌지 않는다.
  let retry = try model.retryRun(job: job)
  #expect(retry.url?.host == "caption-run")
  #expect(try String(contentsOf: job.captionAuthorizationURL, encoding: .utf8) == authorized)
}

// ─── 상태 관측 — 결과·무발화·실패·받은 내보내기 대기를 갈라 본다 ────

@Test @MainActor func captionPanelObservationCoversThePanelPhases() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let model = CaptionPanelModel(workspace: workspace)

  // 활성 프로젝트가 없다 — 대기.
  #expect(model.observe(activeProjectUID: nil, job: nil) == .idle)

  // 인박스에 활성 UID 항목이 있다 — 추정 요청 대기.
  let files = try demoShareFiles(root: root, uid: "FINAL-UID-1")
  _ = try CaptionInbox.stage(urls: [files.wav, files.xml], workspace: workspace)
  #expect(
    model.observe(activeProjectUID: "FINAL-UID-1", job: nil)
      == .intakeReady(projectUID: "FINAL-UID-1"))

  // job 기록: 추정만 있으면 승인 대기, 승인 뒤 시도 running이면 실행 중.
  let job = try RoundTripJob(workspace: workspace, id: "caption-9")
  try FileManager.default.createDirectory(at: job.directoryURL, withIntermediateDirectories: true)
  let estimateLine = try ProtocolCodec.encode(
    CaptionEstimateMessage(
      requestRevision: String(repeating: "1", count: 64),
      originRevision: String(repeating: "a", count: 64),
      finalRevision: String(repeating: "2", count: 64),
      renderAudioSHA256: String(repeating: "3", count: 64),
      audioSeconds: "4", transcriptionSeconds: "8", costUpperBound: "0.001",
      pricingSnapshotRevision: String(repeating: "4", count: 64),
      settingsSHA256: String(repeating: "5", count: 64),
      estimateRevision: String(repeating: "6", count: 64)),
    type: "CaptionEstimate", version: ProtocolCodec.captionVersion)
  try Data("\(estimateLine)\n".utf8).write(to: job.captionEstimateURL)
  #expect(
    model.observe(activeProjectUID: "FINAL-UID-1", job: job)
      == .estimateReady(audioSeconds: "4", costUpperBound: "0.001"))

  try Data("authorized\n".utf8).write(to: job.captionAuthorizationURL)
  try Data(
    (try ProtocolCodec.encode(
      CaptionAttemptMessage(
        requestRevision: String(repeating: "1", count: 64),
        estimateRevision: String(repeating: "6", count: 64),
        attemptID: "caption-x", status: "running", startedAt: "2026-10-01T00:00:00Z",
        error: nil),
      type: "CaptionAttempt", version: ProtocolCodec.captionVersion) + "\n").utf8
  ).write(to: job.directoryURL.appendingPathComponent(RoundTripJob.captionAttemptsFileName))
  #expect(model.observe(activeProjectUID: "FINAL-UID-1", job: job) == .running(jobID: "caption-9"))

  // 성공한 무발화 — SRT 없는 성공 상태다.
  try Data(
    (try ProtocolCodec.encode(
      CaptionResultMessage(
        finalRevision: String(repeating: "2", count: 64), srtPath: nil,
        warnings: ["발화 없음"], quality: ["coverage:complete"], speechStatus: "no_speech"),
      type: "CaptionResult", version: ProtocolCodec.captionVersion) + "\n").utf8
  ).write(to: job.captionResultURL)
  #expect(model.observe(activeProjectUID: "FINAL-UID-1", job: job) == .succeededNoSpeech)
}

// ─── 상속 호출 계약 — 키는 엔진 경계(.env)의 몫이다 ────────────────

/// 상속 호출 계약(패널 시대) — 키는 개발 .env이 엔진 경계에서 풀고 앱은 값을
/// 다루지 않는다. 준비 보고서의 유무 불리언 해석만 앱의 몫이다: 칸이 없는
/// 낡은 보고서는 부재로 읽어 유료 실행을 닫는 쪽이 안전하다.
@Test func readinessKeyPresenceDecodesFailClosedForOldReports() throws {
  let payload = """
    {"engine":"vlogsrt-engine","readiness_version":1,"python":{},"tools":{}}
    """.data(using: .utf8)!
  let legacy = try JSONDecoder().decode(EngineReadinessReport.self, from: payload)
  #expect(legacy.openAIKeyPresent == false)

  let present = """
    {"engine":"vlogsrt-engine","readiness_version":1,"openai_key_present":true,
     "python":{},"tools":{}}
    """.data(using: .utf8)!
  #expect(try JSONDecoder().decode(EngineReadinessReport.self, from: present).openAIKeyPresent)
}

// ─── 새 에이전트 URL 호스트 ─────────────────────────────────────

@Test func agentURLCarriesTheCaptionHosts() throws {
  let estimate = try AgentURL.captionEstimateURL(jobID: "caption-1")
  #expect(estimate.absoluteString == "vlogsrt-agent://caption-estimate/caption-1")
  #expect(try AgentURL.request(estimate) == .captionEstimate("caption-1"))
  let run = try AgentURL.captionRunURL(jobID: "caption-2")
  #expect(run.absoluteString == "vlogsrt-agent://caption-run/caption-2")
  #expect(try AgentURL.request(run) == .captionRun("caption-2"))
}
