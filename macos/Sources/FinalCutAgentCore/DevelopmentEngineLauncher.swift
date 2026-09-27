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
    "analysis-result.jsonl",
    "edit-plan.jsonl",
    "roughcut.fcpxml",
    "roughcut.fcpxml.review.json",
    "caption-request.jsonl",
    "caption-estimate.jsonl",
    "caption-result.jsonl",
    "cancellation.jsonl",
  ]
  /// 실패한 실행이 앱으로 나갈 수 있는 진단 증표 — 성공 산물(후보·러프컷)은 없다.
  /// 실패를 보여주려고 성공 실행을 다시 요구하지 않는다(S1B).
  private static let diagnostics = [
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "vision-decisions.jsonl",
    "analysis-result.jsonl",
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
    diagnosticsOnly: Bool = false,
    fileManager: FileManager = .default
  ) throws {
    // 취소된 작업의 결과는 동기화하지 않는다 — 취소 뒤 새 호출·sync·import 차단.
    guard !CancellationStore.isCancelled(jobURL: externalJob, fileManager: fileManager) else {
      throw DevelopmentEngineError.cancelled
    }
    for name in diagnosticsOnly ? diagnostics : results {
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
    // NSUserUnixTask는 비0 종료·신호를 오류로만 알린다. 이 실행의 안쪽 종료
    // 코드는 엔진 진입점이 남기는 소유된 기록으로만 읽는다(S2A-1). 계약: 진입점은
    // 상태 전달이 끝난 뒤에만 0으로 끝난다 — 그러므로 기록은 **오류가 없을 때만**
    // 받아들인다. 오류 문면을 해석하는 휴리스틱은 아예 없다.
    let capture = try EngineStatusCapture.make()
    defer { capture.discard() }  // 시작 실패·취소·오류 어떤 끝에서도 치운다.
    let task = try NSUserUnixTask(url: invocation.executableURL)
    return try await withCheckedThrowingContinuation { continuation in
      task.execute(withArguments: invocation.arguments + [capture.fileURL.path]) { error in
        continuation.resume(with: Result {
          // 오류(비0 종료·신호·시작 실패·낡은 래퍼)는 기록이 있어도 재분류하지
          // 않는다 — 없는 기록과 함께 오류로 나간다.
          if let error { throw error }
          return try capture.readExitStatus()
        })
      }
    }
  }
}

/// 호출 한 번의 종료 상태 기록 — 실행기가 만든 전용 임시 경로에 엔진이 남긴다.
///
/// 경로는 job 상태 밖의 고유한 임시 디렉터리 안에 있고 호출마다 새로 만든다:
/// 낡은 기록·남의 기록이 이 실행으로 읽힐 자리가 없다(token은 파일 이름 줄기).
/// 읽기는 상한(256바이트+1)까지만 — 큰 기록이 메모리를 채우는 길이 없다.
/// 기록이 없으면(낡은 래퍼·시작 실패·비정상 종료) 오류다 — 상태를 추측하지 않는다.
struct EngineStatusCapture {
  static let prefix = "VLOGSRT-ENGINE-STATUS"
  static let maximumByteCount = 256

  let directory: URL
  let token: String

  var fileURL: URL { directory.appendingPathComponent("\(token).status") }

  static func make(fileManager: FileManager = .default) throws -> Self {
    let token = UUID().uuidString
    let directory = fileManager.temporaryDirectory
      .appendingPathComponent("vlogsrt-status-\(token)", isDirectory: true)
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: false)
    return Self(directory: directory, token: token)
  }

  func readExitStatus() throws -> Int32 {
    guard let handle = FileHandle(forReadingAtPath: fileURL.path) else {
      throw DevelopmentEngineError.missingStatusRecord
    }
    defer { try? handle.close() }
    // 상한보다 한 바이트 더 읽는다 — 그 한 바이트가 차면 과대 기록이다.
    guard
      let head = try handle.read(upToCount: Self.maximumByteCount + 1),
      head.count <= Self.maximumByteCount,
      var line = String(data: head, encoding: .utf8),
      line.hasSuffix("\n")
    else { throw DevelopmentEngineError.malformedStatusRecord }
    line.removeLast()
    let parts = line.split(separator: " ")
    guard parts.count == 3, parts[0] == Self.prefix, parts[1] == Substring(token),
      let code = Int32(parts[2]), (0...255).contains(code)
    else { throw DevelopmentEngineError.malformedStatusRecord }
    return code
  }

  func discard(fileManager: FileManager = .default) {
    try? fileManager.removeItem(at: directory)
  }
}

public enum DevelopmentEngineError: Error, Equatable, LocalizedError {
  case missingInput
  case stagedInputChanged(String)
  case unsafePath(String)
  case nonRegularFile(String)
  case cancelled
  case missingStatusRecord
  case malformedStatusRecord

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
    case .missingStatusRecord:
      "엔진이 종료 상태 기록을 남기지 않았습니다 — 상태를 추측하지 않습니다"
    case .malformedStatusRecord:
      "엔진의 종료 상태 기록이 이 실행의 것이 아닙니다"
    }
  }
}
