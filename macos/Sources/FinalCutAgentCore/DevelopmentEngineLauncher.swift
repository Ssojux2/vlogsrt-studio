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
    "pricing-snapshot.jsonl",
    "analysis-authorization.jsonl",
    "edit-plan.jsonl",
    "approved-candidates.txt",
    "cancellation.jsonl",
    "media-grant.json",
    "caption-authorization.jsonl",
  ]
  private static let results = [
    "analysis-estimate.jsonl",
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "candidates.jsonl",
    "vision-decisions.jsonl",
    "edit-plan.jsonl",
    "roughcut.fcpxml",
    "roughcut.fcpxml.review.json",
    "caption-request.jsonl",
    "caption-estimate.jsonl",
    "caption-result.jsonl",
    "cancellation.jsonl",
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
    try fileManager.createDirectory(at: externalRoot, withIntermediateDirectories: true)
    let confinedRoot = externalRoot.resolvingSymlinksInPath().standardizedFileURL
    let jobs = confinedRoot.appendingPathComponent("Jobs", isDirectory: true)
    try requireDirectoryOrMissing(jobs, fileManager: fileManager)
    let destination = jobs.appendingPathComponent(jobID, isDirectory: true)
    try fileManager.createDirectory(at: destination, withIntermediateDirectories: true)
    try requireDirectory(destination, fileManager: fileManager)
    guard destination.resolvingSymlinksInPath().path.hasPrefix(confinedRoot.path + "/") else {
      throw DevelopmentEngineError.unsafePath(destination.path)
    }
    for name in inputs {
      let source = appGroupJob.appendingPathComponent(name)
      guard fileManager.fileExists(atPath: source.path) else { continue }
      try requireRegularFile(source)
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
    // 취소된 작업의 결과는 동기화하지 않는다 — 취소 뒤 새 호출·sync·import 차단.
    guard !CancellationStore.isCancelled(jobURL: externalJob, fileManager: fileManager) else {
      throw DevelopmentEngineError.cancelled
    }
    for name in results {
      let rootSource = externalJob.appendingPathComponent(name)
      let workspaceSource = externalJob.appendingPathComponent("workspace").appendingPathComponent(name)
      let source = fileManager.fileExists(atPath: rootSource.path) ? rootSource : workspaceSource
      guard fileManager.fileExists(atPath: source.path) else { continue }
      try requireRegularFile(source)
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

  private static func requireDirectoryOrMissing(
    _ url: URL, fileManager: FileManager
  ) throws {
    guard fileManager.fileExists(atPath: url.path) else { return }
    try requireDirectory(url, fileManager: fileManager)
  }

  private static func requireDirectory(
    _ url: URL, fileManager: FileManager
  ) throws {
    let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard values.isDirectory == true, values.isSymbolicLink != true else {
      throw DevelopmentEngineError.unsafePath(url.path)
    }
  }

  private static func requireRegularFile(_ url: URL) throws {
    let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true else {
      throw DevelopmentEngineError.nonRegularFile(url.lastPathComponent)
    }
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
  case unsafePath(String)
  case nonRegularFile(String)
  case cancelled

  public var errorDescription: String? {
    switch self {
    case .missingInput:
      "App Group job에 input.fcpxml이 없습니다"
    case .stagedInputChanged(let name):
      "재개하려는 외부 작업의 입력이 바뀌었습니다: \(name)"
    case .unsafePath(let path):
      "작업 폴더가 symlink이거나 허용 범위 밖입니다: \(path)"
    case .nonRegularFile(let name):
      "동기화 대상이 일반 파일이 아닙니다: \(name)"
    case .cancelled:
      "취소된 작업입니다 — 새 호출·동기화·import를 하지 않습니다"
    }
  }
}
