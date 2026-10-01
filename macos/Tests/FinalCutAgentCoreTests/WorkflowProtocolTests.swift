import Foundation
import Testing

@testable import FinalCutAgentCore

@Test func captionRequestUsesPythonCompatibleSnakeCaseJSON() throws {
  let request = CaptionRequest(
    originRevision: String(repeating: "a", count: 64),
    finalRevision: String(repeating: "b", count: 64),
    renderAudio: "/tmp/mix.wav",
    renderAudioSHA256: String(repeating: "c", count: 64),
    finalFCPXML: "/tmp/final.fcpxml"
  )
  let line = try ProtocolCodec.encode(
    request,
    type: "CaptionRequest",
    version: ProtocolCodec.captionVersion
  )
  #expect(line.hasSuffix("\n"))
  #expect(line.contains("\"protocol_version\":\"1.1\""))
  #expect(line.contains("\"origin_revision\":\"" + String(repeating: "a", count: 64)))
  #expect(line.contains("\"insertion_method\":\"relative\""))
}

@Test func projectSelectionUsesAnalysisProtocol13AndReducedRationalTimes() throws {
  let selection = try ActiveProjectSelection.validated(
    projectUID: "P-1",
    projectName: "Project",
    timelineStart: RationalTime(value: 3_600, timescale: 100),
    duration: RationalTime(value: 12_000, timescale: 100),
    frameDuration: RationalTime(value: 100, timescale: 2_500)
  )
  let message = try ProjectSelectionMessage.create(selection)
  let line = try ProtocolCodec.encode(
    message,
    type: "ProjectSelection",
    version: ProtocolCodec.analysisVersion
  )
  let object = try #require(JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
  #expect(object["protocol_version"] as? String == ProtocolCodec.analysisVersion)
  let payload = try #require(object["payload"] as? [String: Any])
  #expect(payload["timeline_start"] as? String == "36/1")
  #expect(payload["duration"] as? String == "120/1")
  #expect(payload["frame_duration"] as? String == "1/25")
  #expect((payload["selection_revision"] as? String)?.count == 64)
}

/// Python AnalysisEstimate(1.2) 한 줄 — 집계 묶음 검사의 job 기록으로도 쓴다.
private let estimateLine = """
  {"payload":{"cost_upper_bound":"1.25","estimate_revision":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","max_image_tokens":510,"max_images":6,"max_windows":1,"media_grant_revision":"9999999999999999999999999999999999999999999999999999999999999999","model":"gpt-5.6-luna","pricing_snapshot_revision":"8888888888888888888888888888888888888888888888888888888888888888","settings_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","transcription_seconds":"40/1","unique_source_audio_seconds":"10/1"},"protocol_version":"1.2","type":"AnalysisEstimate"}
  """

@Test func analysisEstimateAndCandidateDecodeFromLegacyAndCurrentVersions() throws {
  // 1.2 기록도 읽는다 — 페이로드가 같은 종류는 버전만 바뀌어도 복원한다(S1B).
  let estimate = try ProtocolCodec.decodeAnalysis(
    AnalysisEstimateMessage.self,
    type: "AnalysisEstimate",
    from: estimateLine.trimmingCharacters(in: .newlines)
  )
  #expect(estimate.maxImages == 6)
  #expect(estimate.costUpperBound == "1.25")
  #expect(estimate.pricingSnapshotRevision.count == 64)
  #expect(estimate.mediaGrantRevision.count == 64)

  let candidateLine = """
    {"payload":{"candidate_id":"candidate-1","clip_instance_id":"clip-1","confidence":"0.93","decision_revision":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","evidence":["vad:no_speech"],"input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","reason":"internal_silence","selected_by_default":true,"source_range":{"end":"2/1","start":"1/1"},"timeline_range":{"end":"3602/1","start":"3601/1"},"window_id":"window-1"},"protocol_version":"1.2","type":"CutCandidate"}
    """
  let candidate = try ProtocolCodec.decodeAnalysis(
    CutCandidateMessage.self,
    type: "CutCandidate",
    from: candidateLine.trimmingCharacters(in: .newlines)
  )
  #expect(candidate.selectedByDefault)
  #expect(candidate.timelineRange.start == "3601/1")

  // 지원 밖 버전은 여전히 거부다.
  #expect(throws: ProtocolCodecError.self) {
    _ = try ProtocolCodec.decodeAnalysis(
      AnalysisEstimateMessage.self,
      type: "AnalysisEstimate",
      from: estimateLine.replacingOccurrences(of: "\"1.2\"", with: "\"9.0\"")
    )
  }
}

/// Python AnalysisResult(1.3) 한 줄 — 창·상류·소스 수와 사유를 그대로 실은다.
private let analysisResultLine = """
  {"payload":{"attempt_id":"attempt-001","candidate_count":1,"estimate_revision":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","reason_codes":["api_exception"],"result_revision":"4c85fd3ab3460de3a06e4ee59987c9e2b3ee9fccb7c067f0b2ef01b428ccdf35","status":"partial_error","upstream":{"audio":{"failed":0,"skipped":0,"total":2},"sources":{"total":2,"usable":1},"transcription":{"failed":0,"skipped":0,"total":2},"vad":{"failed":0,"skipped":0,"total":2}},"windows":{"failed":1,"skipped":0,"successful":1,"total":2}},"protocol_version":"1.3","type":"AnalysisResult"}
  """

/// 전체 실패 집계 — Python AnalysisResult.create가 만든 정합 라인(리비전 포함).
private let analysisErrorLine = """
  {"payload":{"attempt_id":"attempt-001","candidate_count":0,"estimate_revision":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","reason_codes":["stream_unavailable"],"result_revision":"17b3555d17ad45913ee710c9fe77262107e512a114743160b32980abe814468f","status":"error","upstream":{"audio":{"failed":1,"skipped":0,"total":1},"sources":{"total":1,"usable":0},"transcription":{"failed":0,"skipped":1,"total":1},"vad":{"failed":0,"skipped":1,"total":1}},"windows":{"failed":1,"skipped":0,"successful":0,"total":1}},"protocol_version":"1.3","type":"AnalysisResult"}
  """

/// 성공 집계 — 시도 상태 불일치(failed 시도 + success 집계) 시험의 재료.
private let analysisSuccessLine = """
  {"payload":{"attempt_id":"attempt-001","candidate_count":1,"estimate_revision":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","reason_codes":[],"result_revision":"ffa04e67f64c58e86cb720cceec797189be2c247ebb63222f244ad1574c7981a","status":"success","upstream":{"audio":{"failed":0,"skipped":0,"total":1},"sources":{"total":1,"usable":1},"transcription":{"failed":0,"skipped":0,"total":1},"vad":{"failed":0,"skipped":0,"total":1}},"windows":{"failed":0,"skipped":0,"successful":1,"total":1}},"protocol_version":"1.3","type":"AnalysisResult"}
  """

/// STAB-S1B-R1 — 소비자 우회 회귀: 묶을 수 없는 기록이 검토를 켜거나 재실행으로 흐른다.
/// 지금 판독기(readAnalysisOutcome)와 공유 라우팅 어미(shouldLoadCandidates)를 함께 쓴다.
@Test func unboundHistoryMustNotEnableCandidateReviewOrImplicitRerun() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  _ = try workspace.createJob(id: "job-r1")
  let job = try RoundTripJob(workspace: workspace, id: "job-r1")
  let attemptsURL = job.directoryURL.appendingPathComponent("analysis-attempts.jsonl")

  func stage(
    result: String, attempts: [String], estimate: String? = estimateLine,
    staleCandidates: Bool = true
  ) throws {
    try? FileManager.default.removeItem(at: job.analysisResultURL)
    try? FileManager.default.removeItem(at: job.analysisEstimateURL)
    try? FileManager.default.removeItem(at: job.candidatesURL)
    try Data(result.utf8).write(to: job.analysisResultURL, options: .atomic)
    try Data((attempts.joined(separator: "\n") + "\n").utf8)
      .write(to: attemptsURL, options: .atomic)
    if let estimate {
      try Data(estimate.utf8).write(to: job.analysisEstimateURL, options: .atomic)
    }
    if staleCandidates {
      try Data("stale\n".utf8).write(to: job.candidatesURL, options: .atomic)
    }
  }

  func mustNotBeReviewable() throws {
    let outcome = job.readAnalysisOutcome()
    #expect(
      !AnalysisOutcome.shouldLoadCandidates(
        outcome: outcome, candidatesFileExists: true
      ),
      "묶을 수 없는 기록이 후보 검토를 켰다: \(outcome)"
    )
  }

  // 낡은 집계(더 최근 시도) — 지금 결과로 읽히면 안 된다.
  try stage(
    result: analysisResultLine,
    attempts: [attempt("failed"), attempt("succeeded", id: "attempt-002")]
  )
  try mustNotBeReviewable()

  // 마지막 시도 running·cancelled — 검토를 켤 수 없다.
  try stage(result: analysisResultLine, attempts: [attempt("running")])
  try mustNotBeReviewable()
  try stage(result: analysisResultLine, attempts: [attempt("cancelled")])
  try mustNotBeReviewable()

  // 추정 기록이 없거나 깨졌다 — 옛 유효 기록으로 넘어가면 안 된다.
  try stage(result: analysisResultLine, attempts: [attempt("succeeded")], estimate: nil)
  try mustNotBeReviewable()
  try stage(result: analysisResultLine, attempts: [attempt("succeeded")], estimate: "not json\n")
  try mustNotBeReviewable()
  try stage(
    result: analysisResultLine, attempts: [attempt("succeeded")],
    estimate: estimateLine.dropFirst(200).description
  )
  try mustNotBeReviewable()

  // 시도 기록이 없거나 깨졌다 — 묶음의 증거가 없다.
  try stage(result: analysisResultLine, attempts: [])
  try mustNotBeReviewable()
  try stage(result: analysisResultLine, attempts: ["garbage\n"])
  try mustNotBeReviewable()

  // 시도 상태 불일치 — success 집계에 failed 시도는 성공이 아니다.
  try stage(result: analysisSuccessLine, attempts: [attempt("failed")])
  try mustNotBeReviewable()
  // partial 집계에 cancelled 시도도 검토를 켤 수 없다.
  try stage(result: analysisResultLine, attempts: [attempt("cancelled")])
  try mustNotBeReviewable()

  // 헤드라인 우회: 상태 없음(unknown)이 낡은 후보 파일을 검토 가능으로 만든다.
  #expect(
    !AnalysisOutcome.shouldLoadCandidates(outcome: .legacyUnknown, candidatesFileExists: true),
    "unknown이 후보 검토를 켰다"
  )
}

@Test func analysisResultDecodesAndSummarizesAllFourOutcomeFaces() throws {
  let result = try #require(
    try ProtocolCodec.decode(
      AnalysisResultMessage.self,
      type: "AnalysisResult",
      version: ProtocolCodec.analysisVersion,
      from: analysisResultLine.trimmingCharacters(in: .newlines)
    )
  )
  #expect(result.status == "partial_error")
  #expect(result.windows.successful == 1)
  #expect(result.windows.failed == 1)
  #expect(result.candidateCount == 1)
  #expect(result.upstream.sources.usable == 1)
  #expect(result.upstream.audio.skipped == 0)
  #expect(result.reasonCodes == ["api_exception"])

  let partial = AnalysisOutcome.make(result)
  #expect(
    partial == .partial(candidateCount: 1, windowsFailed: 1, windowsSkipped: 0, reasons: ["api_exception"])
  )
  // 네 얼굴이 각각 다르게 읽힌다 — 후보 0건 성공이 실패로, 실패가 완료로 보이면 안 된다.
  let summaries = [
    AnalysisOutcome.success(candidateCount: 0).userFacingSummary,
    partial.userFacingSummary,
    AnalysisOutcome.failed(reasons: ["vad_error"]).userFacingSummary,
    AnalysisOutcome.legacyUnknown.userFacingSummary,
  ]
  #expect(summaries[0].contains("컷 후보 0건"))
  #expect(summaries[0].contains("정상"))
  #expect(summaries[1].contains("부분 실패"))
  #expect(summaries[1].contains("검토 가능"))
  #expect(summaries[2].contains("분석 실패"))
  #expect(summaries[2].contains("보존"))
  #expect(summaries[3].contains("알 수 없다"))
  #expect(summaries[3].contains("자동 재실행하지 않는다"))
  #expect(Set(summaries).count == 4)

  // 낡은 봉투의 집계는 해석할 수 없다 — 성공도 재실행도 아닌 unknown이다.
  #expect(throws: ProtocolCodecError.self) {
    _ = try ProtocolCodec.decode(
      AnalysisResultMessage.self,
      type: "AnalysisResult",
      version: ProtocolCodec.analysisVersion,
      from: analysisResultLine.replacingOccurrences(of: "\"1.3\"", with: "\"1.2\"")
    )
  }
  #expect(AnalysisOutcome.make(nil) == .legacyUnknown)
}

@Test func shareInboxRequiresExactlyOnePCMAndOneFCPXML() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let audio = root.appendingPathComponent("mix.wav")
  let xml = root.appendingPathComponent("edit.fcpxml")
  try Data("audio".utf8).write(to: audio)
  try Data("xml".utf8).write(to: xml)

  let pair = try ShareInbox.classify([xml, audio])
  #expect(pair.audio == audio)
  #expect(pair.fcpxml == xml)
  #expect(throws: ShareInboxError.self) {
    try ShareInbox.classify([audio])
  }
}

/// Python CaptionResult(1.1) 한 줄 — 성공한 무발화(SRT 없음)의 실제 모양(S4A).
private let captionNoSpeechLine = """
  {"payload":{"final_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","quality":["coverage:complete"],"speech_status":"no_speech","srt_path":null,"warnings":["발화 없음"]},"protocol_version":"1.1","type":"CaptionResult"}
  """

/// Python CaptionAttempt(1.1) 한 줄 — 시도가 실행 중이라는 내구 진행 기록(S4A).
private let captionAttemptLine = """
  {"payload":{"attempt_id":"caption-attempt-001","error":null,"estimate_revision":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","request_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","started_at":"2026-10-01T04:00:00Z","status":"running"},"protocol_version":"1.1","type":"CaptionAttempt"}
  """

@Test func captionResultAndAttemptDecodeFromPythonLines() throws {
  let result = try ProtocolCodec.decode(
    CaptionResultMessage.self,
    type: "CaptionResult",
    version: ProtocolCodec.captionVersion,
    from: captionNoSpeechLine.trimmingCharacters(in: .newlines)
  )
  #expect(result.speechStatus == "no_speech")
  #expect(result.srtPath == nil)
  #expect(result.warnings == ["발화 없음"])
  #expect(result.quality == ["coverage:complete"])

  let attempt = try ProtocolCodec.decode(
    CaptionAttemptMessage.self,
    type: "CaptionAttempt",
    version: ProtocolCodec.captionVersion,
    from: captionAttemptLine.trimmingCharacters(in: .newlines)
  )
  #expect(attempt.status == "running")
  #expect(attempt.error == nil)
  #expect(attempt.requestRevision.count == 64)

  // 승인이 없는 자막 결과는 앱이 읽을 이유가 없다 — 버전·타입이 어긋나면 만들지 않는다.
  #expect(throws: ProtocolCodecError.self) {
    _ = try ProtocolCodec.decode(
      CaptionAttemptMessage.self,
      type: "CaptionAttempt",
      version: ProtocolCodec.captionVersion,
      from: captionAttemptLine.replacingOccurrences(of: "\"1.1\"", with: "\"1.0\"")
    )
  }
}

@Test func shareInboxStagesAPythonCompatibleCaptionRequest() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let incoming = root.appendingPathComponent("incoming", isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: incoming, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let audio = incoming.appendingPathComponent("mix.wav")
  let xml = incoming.appendingPathComponent("edit.fcpxml")
  try Data("audio".utf8).write(to: audio)
  try Data("<fcpxml/>".utf8).write(to: xml)

  let requestURL = try ShareInbox.stageCaptionRequest(
    SharedExport(audio: audio, fcpxml: xml),
    originRevision: String(repeating: "a", count: 64),
    in: job
  )
  let line = try String(contentsOf: requestURL, encoding: .utf8)
  #expect(line.contains("\"type\":\"CaptionRequest\""))
  #expect(line.contains("\"render_audio\":\"" + job.appendingPathComponent("final.wav").path))
  #expect(line.contains("\"final_fcpxml\":\"" + job.appendingPathComponent("final.fcpxml").path))
}

@Test func fcpxmldBundlesResolveToInfoXMLBeforeStaging() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let bundle = root.appendingPathComponent("Project.fcpxmld", isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let info = bundle.appendingPathComponent("Info.fcpxml")
  let audio = root.appendingPathComponent("mix.wav")
  try Data("<fcpxml/>".utf8).write(to: info)
  try Data("audio".utf8).write(to: audio)

  #expect(try FCPXMLDocument.resolve(bundle) == info)
  let staged = try ShareInbox.stage(SharedExport(audio: audio, fcpxml: bundle), in: job)
  #expect(try Data(contentsOf: staged.fcpxml) == Data("<fcpxml/>".utf8))
}

@Test func pasteboardTypesPreferCurrentVersionThenGeneric() {
  #expect(
    FinalCutPasteboard.supportedTypeNames == [
      "com.apple.finalcutpro.xml.v1-14",
      "com.apple.finalcutpro.xml.v1-13",
      "com.apple.finalcutpro.xml",
    ])
}

@Test func workspaceRejectsTraversalAndCreatesAJobDirectory() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let job = try workspace.createJob(id: "job-42")
  #expect(job.deletingLastPathComponent().lastPathComponent == "Jobs")
  #expect(throws: WorkspaceError.self) {
    try workspace.createJob(id: "../escape")
  }
}

@Test func roundTripJobReadsAnalysisOutcomeBoundToTheCurrentJob() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  _ = try workspace.createJob(id: "job-outcome")
  let job = try RoundTripJob(workspace: workspace, id: "job-outcome")

  func stageJob(
    result: String, attempts: [String], estimate: String? = estimateLine
  ) throws {
    try? FileManager.default.removeItem(at: job.analysisResultURL)
    try Data(result.utf8).write(to: job.analysisResultURL, options: .atomic)
    let attemptsURL = job.directoryURL.appendingPathComponent("analysis-attempts.jsonl")
    try? FileManager.default.removeItem(at: attemptsURL)
    if !attempts.isEmpty {
      try Data((attempts.joined(separator: "\n") + "\n").utf8)
        .write(to: attemptsURL, options: .atomic)
    }
    if let estimate {
      try Data(estimate.utf8).write(to: job.analysisEstimateURL, options: .atomic)
    } else {
      try? FileManager.default.removeItem(at: job.analysisEstimateURL)
    }
  }

  // 기록이 전혀 없으면 새 작업이다 — 모호한 기록과 갈라 말한다(S1B-R1).
  #expect(job.readAnalysisRecord() == .fresh)

  // 마지막 시도가 집계와 같은 job(리비전·시도·상태)에 묶였을 때만 읽힌다.
  try stageJob(
    result: analysisResultLine, attempts: [attempt("running"), attempt("succeeded")]
  )
  let outcome = job.readAnalysisOutcome()
  #expect(outcome == .partial(
    candidateCount: 1, windowsFailed: 1, windowsSkipped: 0, reasons: ["api_exception"]
  ))
  #expect(outcome.userFacingSummary.contains("부분 실패"))

  // 마지막 시도가 취소·running이면 집계는 검토를 켜지 못한다.
  try stageJob(result: analysisResultLine, attempts: [attempt("succeeded"), attempt("cancelled")])
  #expect(job.readAnalysisRecord() == .unknown)
  try stageJob(result: analysisResultLine, attempts: [attempt("succeeded"), attempt("running")])
  #expect(job.readAnalysisRecord() == .unknown)

  // 낡은 집계(더 최근 시도가 성공)도 지금 결과로 읽히지 않는다.
  try stageJob(
    result: analysisResultLine,
    attempts: [attempt("failed"), attempt("succeeded", id: "attempt-002")]
  )
  #expect(job.readAnalysisRecord() == .unknown)

  // 시도 상태 불일치 — success 집계에 failed 시도는 성공이 아니다.
  try stageJob(result: analysisSuccessLine, attempts: [attempt("failed")])
  #expect(job.readAnalysisRecord() == .unknown)

  // 추정 기록이 없거나 리비전이 다른 집계는 묶이지 않은 것이다 — 옛 기록으로
  // 넘어가지 않는다.
  try stageJob(result: analysisResultLine, attempts: [attempt("succeeded")], estimate: nil)
  #expect(job.readAnalysisRecord() == .unknown)
  try stageJob(result: analysisResultLine, attempts: [attempt("succeeded")])
  #expect(
    job.readAnalysisOutcome() == .partial(
      candidateCount: 1, windowsFailed: 1, windowsSkipped: 0, reasons: ["api_exception"]
    )
  )
  try stageJob(
    result: analysisResultLine, attempts: [attempt("succeeded")],
    estimate: estimateLine.replacingOccurrences(
      of: String(repeating: "e", count: 64), with: String(repeating: "f", count: 64)
    )
  )
  #expect(job.readAnalysisRecord() == .unknown)

  // 옛 시도가 남긴 후보 파일이 있어도 지금 집계가 실패면 검토로 보이지 않는다.
  try stageJob(
    result: analysisErrorLine,
    attempts: [attempt("running"), attempt("failed")]
  )
  try Data("stale\n".utf8).write(to: job.candidatesURL, options: .atomic)
  let failed = job.readAnalysisOutcome()
  if case .failed = failed {} else {
    Issue.record("실패 집계여야 한다")
  }
  #expect(
    !AnalysisOutcome.shouldLoadCandidates(
      outcome: failed,
      candidatesFileExists: FileManager.default.fileExists(atPath: job.candidatesURL.path)
    )
  )
  // unknown은 어떤 후보 파일도 검토로 켜지 않는다 — 헤드라인 우회의 못 박기.
  #expect(
    !AnalysisOutcome.shouldLoadCandidates(outcome: .legacyUnknown, candidatesFileExists: true)
  )
  #expect(
    !AnalysisOutcome.shouldLoadCandidates(
      outcome: .partial(candidateCount: 2, windowsFailed: 1, windowsSkipped: 0, reasons: []),
      candidatesFileExists: false
    )
  )
}

/// 공유 라우터 — 새 작업·검증된 결과·모호한 기록의 행선을 못 박는다(S1B-R1).
/// unknown은 엔진 호출·후보 검토 어디로도 가지 않는다.
@Test func reviewRouterSeparatesFreshValidAndUnknownHistory() throws {
  #expect(
    AnalysisReviewRouter.route(record: .fresh, candidatesFileExists: false, retryArmed: false)
      == .freshJob
  )
  // 낡은 후보 파일이 있어도 새 작업 판정은 기록이 전혀 없을 때뿐이다.
  #expect(
    AnalysisReviewRouter.route(record: .fresh, candidatesFileExists: true, retryArmed: true)
      == .freshJob
  )
  #expect(
    AnalysisReviewRouter.route(record: .unknown, candidatesFileExists: true, retryArmed: true)
      == .showUnknown
  )
  #expect(
    AnalysisReviewRouter.route(record: .unknown, candidatesFileExists: true, retryArmed: false)
      == .showUnknown
  )

  // 검증된 success·partial은 후보 검토로, 검증된 실패는 명시적 재시도로만.
  let partial = try decodeResult(analysisResultLine)
  let success = try decodeResult(analysisSuccessLine)
  let error = try decodeResult(analysisErrorLine)
  #expect(
    AnalysisReviewRouter.route(record: .valid(partial), candidatesFileExists: true, retryArmed: true)
      == .reviewCandidates(
        .partial(candidateCount: 1, windowsFailed: 1, windowsSkipped: 0, reasons: ["api_exception"])
      )
  )
  #expect(
    AnalysisReviewRouter.route(
      record: .valid(success), candidatesFileExists: false, retryArmed: false
    ) == .reviewCandidates(.success(candidateCount: 1))
  )
  #expect(
    AnalysisReviewRouter.route(record: .valid(error), candidatesFileExists: true, retryArmed: false)
      == .showFailure(.failed(reasons: ["stream_unavailable"]))
  )
  #expect(
    AnalysisReviewRouter.route(record: .valid(error), candidatesFileExists: true, retryArmed: true)
      == .retryEngine
  )
}

private func decodeResult(_ line: String) throws -> AnalysisResultMessage {
  try ProtocolCodec.decode(
    AnalysisResultMessage.self,
    type: "AnalysisResult",
    version: ProtocolCodec.analysisVersion,
    from: line.trimmingCharacters(in: .newlines)
  )
}

/// 실패 뒤 요청 행선 — 같은 작업의 재시도는 명시적이고 구형은 자동 재실행하지 않는다.
@Test func failureRetryRoutingIsExplicitAndLegacyNeverAutoReruns() {
  let failure = AnalysisOutcome.failed(reasons: ["vad_error"])
  #expect(
    AnalysisOutcome.retryRoute(outcome: failure, retryArmed: false) == .showFailure(canRetry: true)
  )
  #expect(AnalysisOutcome.retryRoute(outcome: failure, retryArmed: true) == .retryEngine)
  #expect(
    AnalysisOutcome.retryRoute(outcome: .legacyUnknown, retryArmed: true) == .noOutcome
  )
  #expect(
    AnalysisOutcome.retryRoute(
      outcome: .success(candidateCount: 2), retryArmed: true
    ) == .noOutcome
  )
}

private func attempt(_ status: String, id: String = "attempt-001") -> String {
  let a = String(repeating: "a", count: 64)
  let e = String(repeating: "e", count: 64)
  return """
    {"payload":{"attempt_id":"\(id)","error":\(status == "failed" ? "\"분석 단계 실패\"" : "null"),"estimate_revision":"\(e)","input_revision":"\(a)","started_at":"2026-09-27T04:00:01Z","status":"\(status)"},"protocol_version":"1.3","type":"AnalysisAttempt"}
    """
}
