import Foundation

/// 번들 엔진 실행 — Resources/Engine의 PyInstaller onedir 실행기를 Process로 띄운다.
///
/// 기본은 번들 실행기이고 Debug 빌드에서 NSUserUnixTask 개발 실행기로 되돌릴 수
/// 있다(개발 중에는 앱 서명 없이 ~/Library/Application Scripts에 스크립트를 둔다).
/// API 키는 자식 프로세스 환경변수로만 흘러간다 — argv·JSONL·로그에 절대 실리지
/// 않는다. `network.client` entitlement가 샌드박스 안의 이 경로를 연다.
public struct BundledEngineInvocation: Equatable, Sendable {
  public let executableURL: URL
  public let arguments: [String]
  public let currentDirectory: URL
  public let environment: [String: String]

  /// 키를 환경변수 한 통으로만 전달한다 — 그 밖의 경로는 만들지 않는다.
  /// 진입점 CLI 계약은 개발 래퍼와 같다: `<operation> <job_id> <job_dir>`.
  ///
  /// 앱이 만드는 호출은 **항상** 운반자를 실는다(S2A-2): 키가 있으면 그 값,
  /// 없으면 빈 값. 빈 운반자는 '이 실행에는 키가 없다'는 명시적 선언이라 자식
  /// 안에서 상속·dotenv의 낡은 키가 다시 살아나는 길을 닫는다 — 운반자가
  /// 아예 없으면 자식이 그 차이를 못 본다.
  ///
  /// PATH는 번들 `bin/`(ffmpeg·ffprobe)과 시스템 최소 경로뿐이다 — Homebrew
  /// (/opt/homebrew)는 없으므로 Release 번들 실행이 조용히 시스템 ffmpeg로
  /// 되돌아가는 길이 없다. 도구가 없으면 `require_ffmpeg`가 실패로 닫는다.
  public static func create(
    operation: DevelopmentEngineOperation,
    jobID: String,
    jobDirectory: URL,
    engineRoot: URL,
    apiKey: String?
  ) throws -> Self {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw WorkspaceError.invalidJobID(jobID)
    }
    let executable = engineRoot
      .appendingPathComponent("Contents", isDirectory: true)
      .appendingPathComponent("MacOS", isDirectory: true)
      .appendingPathComponent(DevelopmentEngineInvocation.scriptName)
    let environment = [
      Self.pathEnvironmentName: Self.bundledToolPath(engineRoot: engineRoot),
      Self.apiKeyEnvironmentName: apiKey ?? "",
    ]
    return Self(
      executableURL: executable,
      arguments: [operation.rawValue, jobID, jobDirectory.standardizedFileURL.path],
      currentDirectory: engineRoot,
      environment: environment
    )
  }

  public static let apiKeyEnvironmentName = "VLOGSRT_ENGINE_API_KEY"
  public static let pathEnvironmentName = "PATH"

  /// 번들의 도구 경로 — bin/ffmpeg·bin/ffprobe가 여기서 발견된다.
  public static func bundledToolPath(engineRoot: URL) -> String {
    "\(engineRoot.standardizedFileURL.path)/bin:/usr/bin:/bin"
  }
}

public enum EngineLaunchMode: String, Sendable {
  case bundled
  case development

  /// 번들이 있으면 번들, 없으면 개발 실행기 — 정확히 이 순서뿐이다.
  public static func resolve(bundleURL: URL?, fileManager: FileManager = .default) -> Self {
    guard let bundleURL else { return .development }
    let executable = bundleURL
      .appendingPathComponent("Contents", isDirectory: true)
      .appendingPathComponent("MacOS", isDirectory: true)
      .appendingPathComponent(DevelopmentEngineInvocation.scriptName)
    return fileManager.isExecutableFile(atPath: executable.path) ? .bundled : .development
  }
}

public final class BundledEngineLauncher: @unchecked Sendable {
  public init() {}

  /// 엔진을 띄워 끝나기를 기다린다. 환경은 invocation이 준 것만 물려받는다 —
  /// 부모의 환경(키 포함)을 그대로 상속하지 않는 것이 이 계약의 요점이다.
  public func run(_ invocation: BundledEngineInvocation) throws -> Int32 {
    let process = Process()
    process.executableURL = invocation.executableURL
    process.arguments = invocation.arguments
    process.currentDirectoryURL = invocation.currentDirectory
    process.environment = invocation.environment
    process.standardOutput = FileHandle.standardOutput
    process.standardError = FileHandle.standardError
    try process.run()
    process.waitUntilExit()
    return process.terminationStatus
  }
}
