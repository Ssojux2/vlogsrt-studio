import Foundation
import Testing

@testable import FinalCutAgentCore

@Test func developmentEngineInvocationUsesApplicationScriptWithoutSecrets() throws {
  let scripts = URL(fileURLWithPath: "/Users/test/Library/Application Scripts/com.ssojux2.vlogsrt")
  let invocation = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "analysis-42",
    externalJob: URL(fileURLWithPath: "/Users/test/Movies/VlogJobs/analysis-42"),
    applicationScriptsDirectory: scripts
  )
  #expect(invocation.executableURL == scripts.appendingPathComponent("vlogsrt-engine"))
  #expect(invocation.arguments == ["analyze", "analysis-42", "/Users/test/Movies/VlogJobs/analysis-42"])
  #expect(!invocation.arguments.joined().contains("OPENAI_API_KEY"))

  #expect(throws: WorkspaceError.self) {
    try DevelopmentEngineInvocation.create(
      operation: .roughcut,
      jobID: "../escape",
      externalJob: URL(fileURLWithPath: "/tmp/escape"),
      applicationScriptsDirectory: scripts
    )
  }
}

@Test func developmentJobStagerCopiesInputsOutAndOnlyAllowlistedResultsBack() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let appGroup = root.appendingPathComponent("group", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  let workspace = AppGroupWorkspace(rootURL: appGroup)
  let groupJob = try workspace.createJob(id: "analysis-1")
  try Data("xml".utf8).write(to: groupJob.appendingPathComponent("input.fcpxml"))
  try Data("selection".utf8).write(
    to: groupJob.appendingPathComponent("project-selection.jsonl")
  )

  let staged = try DevelopmentJobStager.stage(
    jobID: "analysis-1",
    appGroupJob: groupJob,
    externalRoot: external
  )
  #expect(try Data(contentsOf: staged.appendingPathComponent("input.fcpxml")) == Data("xml".utf8))
  try Data("candidate".utf8).write(to: staged.appendingPathComponent("candidates.jsonl"))
  try Data("secret".utf8).write(to: staged.appendingPathComponent(".env"))
  try DevelopmentJobStager.syncResults(from: staged, to: groupJob)
  #expect(FileManager.default.fileExists(atPath: groupJob.appendingPathComponent("candidates.jsonl").path))
  #expect(!FileManager.default.fileExists(atPath: groupJob.appendingPathComponent(".env").path))
}
