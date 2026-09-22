import Foundation
import Testing

@testable import FinalCutAgentCore

@MainActor
private final class FakeImporter: RoundTripImporting {
  var sent: [URL] = []
  var failure: Error?

  func sendOpenDocument(_ fcpxml: URL) async throws {
    if let failure { throw failure }
    sent.append(fcpxml)
  }
}

private let minimalFCPXML = """
  <?xml version="1.0" encoding="UTF-8"?>
  <fcpxml version="1.9">
    <library><event name="e"><project name="프로젝트" uid="P1"><sequence format="r1"><spine/></sequence></project></event></library>
  </fcpxml>
  """

@Test func agentURLParsesOnlyRoundTripJobURLs() throws {
  let good = try AgentURL.roundTripURL(jobID: "roundtrip-7")
  #expect(try AgentURL.roundTripJobID(good) == "roundtrip-7")
  #expect(good.scheme == "vlogsrt-agent")
  #expect(good.host == "roundtrip")

  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(string: "https://roundtrip/roundtrip-7")!)
  }
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(string: "vlogsrt-agent://other/roundtrip-7")!)
  }
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(string: "vlogsrt-agent://roundtrip/")!)
  }
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(string: "vlogsrt-agent://roundtrip/a/b")!)
  }
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(string: "vlogsrt-agent://roundtrip/roundtrip-7?extra=1")!)
  }
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(string: "vlogsrt-agent://roundtrip/../escape")!)
  }
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.roundTripJobID(URL(fileURLWithPath: "/tmp/roundtrip-7"))
  }
}

@Test func agentURLSeparatesAnalyzeAndRoughcutOperations() throws {
  let analyze = try AgentURL.analyzeURL(jobID: "job-1")
  let roughcut = try AgentURL.roughcutURL(jobID: "job-1")
  #expect(try AgentURL.request(analyze) == .analyze("job-1"))
  #expect(try AgentURL.request(roughcut) == .roughcut("job-1"))
  #expect(throws: AgentURLError.self) {
    try AgentURL.request(URL(string: "vlogsrt-agent://analyze/job-1?token=secret")!)
  }
}

@Test @MainActor func coordinatorWritesReadyResultAfterImportSucceeds() async throws {
  let fixture = try makeReceivedJob(xml: minimalFCPXML, jobID: "roundtrip-ok", request: try validRequest(jobID: "roundtrip-ok"))
  let importer = FakeImporter()
  let coordinator = RoundTripCoordinator(
    workspace: AppGroupWorkspace(rootURL: fixture.workspaceRoot),
    importer: importer
  )

  let result = try await coordinator.handle(url: fixture.url)

  #expect(result.status == .roundtripReady)
  #expect(result.outputFileName == RoundTripJob.outputFileName)
  #expect(importer.sent == [fixture.job.outputURL])
  let resultLine = try String(contentsOf: fixture.job.resultURL, encoding: .utf8)
  #expect(resultLine.contains("\"type\":\"RoundTripResult\""))
  #expect(resultLine.contains("\"status\":\"roundtrip_ready\""))
  #expect(FileManager.default.fileExists(atPath: fixture.job.outputURL.path))
}

@Test @MainActor func coordinatorWritesFailedResultWhenImportFails() async throws {
  let fixture = try makeReceivedJob(xml: minimalFCPXML, jobID: "roundtrip-import-fail", request: try validRequest(jobID: "roundtrip-import-fail"))
  let importer = FakeImporter()
  importer.failure = FinalCutImportError.applicationNotFound
  let coordinator = RoundTripCoordinator(
    workspace: AppGroupWorkspace(rootURL: fixture.workspaceRoot),
    importer: importer
  )

  let result = try await coordinator.handle(url: fixture.url)

  #expect(result.status == .failed)
  #expect(result.error?.contains("applicationNotFound") == true)
  #expect(importer.sent.isEmpty)
  let resultLine = try String(contentsOf: fixture.job.resultURL, encoding: .utf8)
  #expect(resultLine.contains("\"status\":\"failed\""))
  // import 실패가 성공으로 세탁되지 않는다 — 출력은 남되 상태는 failed다.
  #expect(FileManager.default.fileExists(atPath: fixture.job.outputURL.path))
}

@Test @MainActor func coordinatorWritesFailedResultWhenXMLIsBroken() async throws {
  let fixture = try makeReceivedJob(xml: "<fcpxml><library", jobID: "roundtrip-broken", request: try validRequest(jobID: "roundtrip-broken"))
  let importer = FakeImporter()
  let coordinator = RoundTripCoordinator(
    workspace: AppGroupWorkspace(rootURL: fixture.workspaceRoot),
    importer: importer
  )

  let result = try await coordinator.handle(url: fixture.url)

  #expect(result.status == .failed)
  #expect(result.error != nil)
  #expect(importer.sent.isEmpty)
  #expect(!FileManager.default.fileExists(atPath: fixture.job.outputURL.path))
}

@Test @MainActor func coordinatorRejectsBadURLsBeforeTouchingTheJob() async throws {
  let fixture = try makeReceivedJob(xml: minimalFCPXML, jobID: "roundtrip-untouched", request: try validRequest(jobID: "roundtrip-untouched"))
  let coordinator = RoundTripCoordinator(
    workspace: AppGroupWorkspace(rootURL: fixture.workspaceRoot),
    importer: FakeImporter()
  )

  var thrown: Error?
  do {
    _ = try await coordinator.handle(url: URL(string: "vlogsrt-agent://other/roundtrip-untouched")!)
  } catch {
    thrown = error
  }
  #expect(thrown is AgentURLError)
  #expect(!FileManager.default.fileExists(atPath: fixture.job.resultURL.path))
}

private func validRequest(jobID: String) throws -> String {
  try ProtocolCodec.encode(
    RoundTripRequest(
      jobID: jobID,
      inputFileName: RoundTripJob.inputFileName,
      outputFileName: RoundTripJob.outputFileName
    ),
    type: "RoundTripRequest"
  )
}

private struct ReceivedJob {
  let workspaceRoot: URL
  let job: RoundTripJob
  let url: URL
}

@Test @MainActor func coordinatorWritesFailedResultWithoutRequest() async throws {
  let fixture = try makeReceivedJob(xml: minimalFCPXML, jobID: "roundtrip-no-request", request: nil)
  let importer = FakeImporter()
  let coordinator = RoundTripCoordinator(
    workspace: AppGroupWorkspace(rootURL: fixture.workspaceRoot),
    importer: importer
  )

  let result = try await coordinator.handle(url: fixture.url)

  #expect(result.status == .failed)
  #expect(importer.sent.isEmpty)
  #expect(!FileManager.default.fileExists(atPath: fixture.job.outputURL.path))
  let resultLine = try String(contentsOf: fixture.job.resultURL, encoding: .utf8)
  #expect(resultLine.contains("\"status\":\"failed\""))
}

@Test @MainActor func coordinatorRejectsMalformedOrDriftingRequests() async throws {
  let badRequests: [(String, String?)] = [
    ("malformed", "이건 JSON이 아니다"),
    ("multi-line", "first\nsecond"),
    (
      "wrong-type",
      try ProtocolCodec.encode(
        RoundTripRequest(jobID: "roundtrip-drift", inputFileName: "input.fcpxml", outputFileName: "roundtrip.fcpxml"),
        type: "CaptionRequest"
      ).trimmingCharacters(in: .whitespacesAndNewlines)
    ),
    (
      "wrong-version",
      #"{"payload":{"job_id":"roundtrip-drift","input_file_name":"input.fcpxml","output_file_name":"roundtrip.fcpxml"},"protocol_version":"2.0","type":"RoundTripRequest"}"#
    ),
    (
      "wrong-job-id",
      try ProtocolCodec.encode(
        RoundTripRequest(jobID: "roundtrip-other", inputFileName: "input.fcpxml", outputFileName: "roundtrip.fcpxml"),
        type: "RoundTripRequest"
      ).trimmingCharacters(in: .whitespacesAndNewlines)
    ),
    (
      "wrong-file-names",
      try ProtocolCodec.encode(
        RoundTripRequest(jobID: "roundtrip-drift", inputFileName: "other.fcpxml", outputFileName: "out.fcpxml"),
        type: "RoundTripRequest"
      ).trimmingCharacters(in: .whitespacesAndNewlines)
    ),
  ]
  for (label, line) in badRequests {
    let fixture = try makeReceivedJob(xml: minimalFCPXML, jobID: "roundtrip-drift", request: line)
    let importer = FakeImporter()
    let coordinator = RoundTripCoordinator(
      workspace: AppGroupWorkspace(rootURL: fixture.workspaceRoot),
      importer: importer
    )

    let result = try await coordinator.handle(url: fixture.url)

    #expect(result.status == .failed, "\(label) 요청이 failed로 기록되어야 한다")
    #expect(importer.sent.isEmpty, "\(label) import가 호출되지 않아야 한다")
    #expect(!FileManager.default.fileExists(atPath: fixture.job.outputURL.path), "\(label) 출력이 만들어지지 않아야 한다")
  }
}

private func makeReceivedJob(xml: String, jobID: String, request: String?) throws -> ReceivedJob {
  let workspaceRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let workspace = AppGroupWorkspace(rootURL: workspaceRoot)
  let job = try RoundTripJob(workspace: workspace, id: jobID)
  _ = try workspace.createJob(id: jobID)
  try Data(xml.utf8).write(to: job.inputURL)
  if let request {
    try Data(request.utf8).write(to: job.requestURL)
  }
  let url = try AgentURL.roundTripURL(jobID: jobID)
  return ReceivedJob(workspaceRoot: workspaceRoot, job: job, url: url)
}
