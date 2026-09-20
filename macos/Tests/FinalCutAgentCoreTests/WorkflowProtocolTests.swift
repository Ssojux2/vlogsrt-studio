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
  let line = try ProtocolCodec.encode(request, type: "CaptionRequest")
  #expect(line.hasSuffix("\n"))
  #expect(line.contains("\"protocol_version\":\"1.0\""))
  #expect(line.contains("\"origin_revision\":\"" + String(repeating: "a", count: 64)))
  #expect(line.contains("\"insertion_method\":\"relative\""))
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
