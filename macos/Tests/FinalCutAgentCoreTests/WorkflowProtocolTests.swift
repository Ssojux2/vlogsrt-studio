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

@Test func projectSelectionUsesAnalysisProtocol12AndReducedRationalTimes() throws {
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
  #expect(object["protocol_version"] as? String == "1.2")
  let payload = try #require(object["payload"] as? [String: Any])
  #expect(payload["timeline_start"] as? String == "36/1")
  #expect(payload["duration"] as? String == "120/1")
  #expect(payload["frame_duration"] as? String == "1/25")
  #expect((payload["selection_revision"] as? String)?.count == 64)
}

@Test func analysisEstimateAndCandidateDecodeFromPython12() throws {
  let estimateLine = """
    {"payload":{"cost_upper_bound":"1.25","estimate_revision":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","max_image_tokens":510,"max_images":6,"max_windows":1,"media_grant_revision":"9999999999999999999999999999999999999999999999999999999999999999","model":"gpt-5.6-luna","pricing_snapshot_revision":"8888888888888888888888888888888888888888888888888888888888888888","settings_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","transcription_seconds":"40/1","unique_source_audio_seconds":"10/1"},"protocol_version":"1.2","type":"AnalysisEstimate"}
    """
  let estimate = try ProtocolCodec.decode(
    AnalysisEstimateMessage.self,
    type: "AnalysisEstimate",
    version: ProtocolCodec.analysisVersion,
    from: estimateLine.trimmingCharacters(in: .newlines)
  )
  #expect(estimate.maxImages == 6)
  #expect(estimate.costUpperBound == "1.25")
  #expect(estimate.pricingSnapshotRevision.count == 64)
  #expect(estimate.mediaGrantRevision.count == 64)

  let candidateLine = """
    {"payload":{"candidate_id":"candidate-1","clip_instance_id":"clip-1","confidence":"0.93","decision_revision":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","evidence":["vad:no_speech"],"input_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","reason":"internal_silence","selected_by_default":true,"source_range":{"end":"2/1","start":"1/1"},"timeline_range":{"end":"3602/1","start":"3601/1"},"window_id":"window-1"},"protocol_version":"1.2","type":"CutCandidate"}
    """
  let candidate = try ProtocolCodec.decode(
    CutCandidateMessage.self,
    type: "CutCandidate",
    version: ProtocolCodec.analysisVersion,
    from: candidateLine.trimmingCharacters(in: .newlines)
  )
  #expect(candidate.selectedByDefault)
  #expect(candidate.timelineRange.start == "3601/1")

  #expect(throws: ProtocolCodecError.self) {
    _ = try ProtocolCodec.decode(
      AnalysisEstimateMessage.self,
      type: "AnalysisEstimate",
      version: ProtocolCodec.analysisVersion,
      from: estimateLine.replacingOccurrences(of: "\"1.2\"", with: "\"1.1\"")
    )
  }
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
