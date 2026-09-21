import Foundation
import Testing

@testable import FinalCutAgentCore

@Test func roundTripStatusVocabularyMatchesContract() {
  #expect(RoundTripStatus.received.rawValue == "received")
  #expect(RoundTripStatus.roundtripRequested.rawValue == "roundtrip_requested")
  #expect(RoundTripStatus.roundtripReady.rawValue == "roundtrip_ready")
  #expect(RoundTripStatus.failed.rawValue == "failed")
}

@Test func roundTripJobLayoutUsesContractFileNames() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let job = try RoundTripJob(workspace: workspace, id: "roundtrip-42")
  #expect(job.directoryURL.lastPathComponent == "roundtrip-42")
  #expect(job.directoryURL.deletingLastPathComponent().lastPathComponent == "Jobs")
  #expect(job.inputURL.lastPathComponent == "input.fcpxml")
  #expect(job.requestURL.lastPathComponent == "roundtrip-request.jsonl")
  #expect(job.outputURL.lastPathComponent == "roundtrip.fcpxml")
  #expect(job.resultURL.lastPathComponent == "roundtrip-result.jsonl")
}

@Test func roundTripJobStoresTheActiveProjectSelectionBeforeAnalysis() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  _ = try workspace.createJob(id: "analysis-1")
  let job = try RoundTripJob(workspace: workspace, id: "analysis-1")
  let selection = ActiveProjectSelection(
    projectUID: "P-1",
    projectName: "Project",
    timelineStart: RationalTime(value: 0, timescale: 1),
    duration: RationalTime(value: 10, timescale: 1),
    frameDuration: RationalTime(value: 1, timescale: 25)
  )
  try job.writeProjectSelection(selection)
  let text = try String(contentsOf: job.projectSelectionURL, encoding: .utf8)
  #expect(text.contains("\"protocol_version\":\"1.1\""))
  #expect(text.contains("\"project_uid\":\"P-1\""))
}

@Test func roundTripJobRejectsTraversalIDs() {
  let workspace = AppGroupWorkspace(rootURL: FileManager.default.temporaryDirectory)
  #expect(throws: WorkspaceError.self) {
    _ = try RoundTripJob(workspace: workspace, id: "../escape")
  }
  #expect(throws: WorkspaceError.self) {
    _ = try RoundTripJob(workspace: workspace, id: "")
  }
  #expect(throws: WorkspaceError.self) {
    _ = try RoundTripJob(workspace: workspace, id: "sub/dir")
  }
}

@Test func roundTripJobWritesRejectOverwritingExistingFiles() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let workspace = AppGroupWorkspace(rootURL: root)
  let job = try RoundTripJob(workspace: workspace, id: "roundtrip-1")
  _ = try workspace.createJob(id: "roundtrip-1")
  defer { try? FileManager.default.removeItem(at: root) }

  try job.writeInput(Data("<fcpxml/>".utf8))
  #expect(throws: RoundTripJobError.self) {
    try job.writeInput(Data("<fcpxml/>".utf8))
  }

  let request = RoundTripRequest(
    jobID: "roundtrip-1",
    inputFileName: RoundTripJob.inputFileName,
    outputFileName: RoundTripJob.outputFileName
  )
  try job.writeRequest(request)
  #expect(throws: RoundTripJobError.self) {
    try job.writeRequest(request)
  }

  let result = try RoundTripResult(
    jobID: "roundtrip-1",
    status: .roundtripReady,
    outputFileName: RoundTripJob.outputFileName,
    error: nil
  )
  try job.writeResult(result)
  #expect(throws: RoundTripJobError.self) {
    try job.writeResult(result)
  }
}

@Test func roundTripRequestEncodesInProtocolEnvelope() throws {
  let request = RoundTripRequest(
    jobID: "roundtrip-9",
    inputFileName: "input.fcpxml",
    outputFileName: "roundtrip.fcpxml"
  )
  let line = try ProtocolCodec.encode(request, type: "RoundTripRequest")
  #expect(line.hasSuffix("\n"))
  #expect(line.contains("\"protocol_version\":\"1.0\""))
  #expect(line.contains("\"type\":\"RoundTripRequest\""))
  #expect(line.contains("\"job_id\":\"roundtrip-9\""))
  #expect(line.contains("\"input_file_name\":\"input.fcpxml\""))
  #expect(line.contains("\"output_file_name\":\"roundtrip.fcpxml\""))
}

@Test func roundTripResultRejectsContradictoryPayloads() throws {
  #expect(throws: RoundTripResultError.self) {
    _ = try RoundTripResult(
      jobID: "j",
      status: .roundtripReady,
      outputFileName: "roundtrip.fcpxml",
      error: "성공인데 오류가 있음"
    )
  }
  #expect(throws: RoundTripResultError.self) {
    _ = try RoundTripResult(jobID: "j", status: .roundtripReady, outputFileName: nil, error: nil)
  }
  #expect(throws: RoundTripResultError.self) {
    _ = try RoundTripResult(jobID: "j", status: .failed, outputFileName: nil, error: nil)
  }
  #expect(throws: RoundTripResultError.self) {
    _ = try RoundTripResult(jobID: "j", status: .received, outputFileName: nil, error: nil)
  }
  let success = try RoundTripResult(
    jobID: "j",
    status: .roundtripReady,
    outputFileName: "roundtrip.fcpxml",
    error: nil
  )
  #expect(success.error == nil)
  let failure = try RoundTripResult(jobID: "j", status: .failed, outputFileName: nil, error: "변환 실패")
  #expect(failure.outputFileName == nil)
}
