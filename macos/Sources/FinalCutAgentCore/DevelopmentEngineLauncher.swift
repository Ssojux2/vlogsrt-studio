import Foundation

public enum DevelopmentEngineOperation: String, Sendable {
  case analyze
  case roughcut
}

public struct DevelopmentEngineInvocation: Equatable, Sendable {
  public static let scriptName = "vlogsrt-engine"
  public let executableURL: URL
  public let arguments: [String]

  public static func create(
    operation: DevelopmentEngineOperation,
    jobID: String,
    externalJob: URL,
    applicationScriptsDirectory: URL
  ) throws -> Self {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw WorkspaceError.invalidJobID(jobID)
    }
    return Self(
      executableURL: applicationScriptsDirectory.appendingPathComponent(scriptName),
      arguments: [operation.rawValue, jobID, externalJob.standardizedFileURL.path]
    )
  }
}

public enum DevelopmentJobStager {
  private static let inputs = [
    "input.fcpxml",
    "project-selection.jsonl",
    "analysis-authorization.jsonl",
    "edit-plan.jsonl",
  ]
  private static let results = [
    "analysis-estimate.jsonl",
    "analysis-attempts.jsonl",
    "candidates.jsonl",
    "vision-decisions.jsonl",
    "edit-plan.jsonl",
    "roughcut.fcpxml",
    "roughcut.fcpxml.review.json",
  ]

  public static func stage(
    jobID: String,
    appGroupJob: URL,
    externalRoot: URL,
    fileManager: FileManager = .default
  ) throws -> URL {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw WorkspaceError.invalidJobID(jobID)
    }
    let jobs = externalRoot.appendingPathComponent("Jobs", isDirectory: true)
    let destination = jobs.appendingPathComponent(jobID, isDirectory: true)
    try fileManager.createDirectory(at: destination, withIntermediateDirectories: true)
    for name in inputs {
      let source = appGroupJob.appendingPathComponent(name)
      guard fileManager.fileExists(atPath: source.path) else { continue }
      try copyStable(source, destination.appendingPathComponent(name), fileManager: fileManager)
    }
    guard fileManager.fileExists(atPath: destination.appendingPathComponent("input.fcpxml").path)
    else { throw DevelopmentEngineError.missingInput }
    return destination
  }

  public static func syncResults(
    from externalJob: URL,
    to appGroupJob: URL,
    fileManager: FileManager = .default
  ) throws {
    for name in results {
      let rootSource = externalJob.appendingPathComponent(name)
      let workspaceSource = externalJob.appendingPathComponent("workspace").appendingPathComponent(name)
      let source = fileManager.fileExists(atPath: rootSource.path) ? rootSource : workspaceSource
      guard fileManager.fileExists(atPath: source.path) else { continue }
      let destination = appGroupJob.appendingPathComponent(name)
      if fileManager.fileExists(atPath: destination.path) {
        try fileManager.removeItem(at: destination)
      }
      try fileManager.copyItem(at: source, to: destination)
    }
  }

  private static func copyStable(
    _ source: URL,
    _ destination: URL,
    fileManager: FileManager
  ) throws {
    if fileManager.fileExists(atPath: destination.path) {
      guard try Data(contentsOf: source) == Data(contentsOf: destination) else {
        throw DevelopmentEngineError.stagedInputChanged(destination.lastPathComponent)
      }
      return
    }
    try fileManager.copyItem(at: source, to: destination)
  }
}

public final class DevelopmentEngineLauncher: @unchecked Sendable {
  public init() {}

  public func run(_ invocation: DevelopmentEngineInvocation) async throws -> Int32 {
    let task = try NSUserUnixTask(url: invocation.executableURL)
    return try await withCheckedThrowingContinuation { continuation in
      task.execute(withArguments: invocation.arguments) { error in
        if let error {
          continuation.resume(throwing: error)
        } else {
          continuation.resume(returning: 0)
        }
      }
    }
  }
}

public enum DevelopmentEngineError: Error, Equatable, LocalizedError {
  case missingInput
  case stagedInputChanged(String)

  public var errorDescription: String? {
    switch self {
    case .missingInput:
      "App Group job에 input.fcpxml이 없습니다"
    case .stagedInputChanged(let name):
      "재개하려는 외부 작업의 입력이 바뀌었습니다: \(name)"
    }
  }
}
