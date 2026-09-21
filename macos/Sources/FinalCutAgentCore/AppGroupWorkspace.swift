import Foundation

public struct AppGroupWorkspace: Sendable {
  public static let identifier = "group.com.ssojux2.vlogsrt"
  public let rootURL: URL

  public init(rootURL: URL) {
    self.rootURL = rootURL.standardizedFileURL
  }

  public static func live(fileManager: FileManager = .default) throws -> Self {
    guard
      let root = fileManager.containerURL(
        forSecurityApplicationGroupIdentifier: identifier
      )
    else {
      throw WorkspaceError.appGroupUnavailable(identifier)
    }
    return Self(rootURL: root)
  }

  public func createJob(id: String, fileManager: FileManager = .default) throws -> URL {
    guard Self.isSafeJobID(id) else { throw WorkspaceError.invalidJobID(id) }
    let jobs = rootURL.appendingPathComponent("Jobs", isDirectory: true)
    let job = jobs.appendingPathComponent(id, isDirectory: true)
    try fileManager.createDirectory(at: job, withIntermediateDirectories: true)
    return job
  }

  /// job ID는 안전한 파일명 조각만 허용한다 — App Group 밖으로 나갈 수 없다.
  public static func isSafeJobID(_ value: String) -> Bool {
    isSafeComponent(value)
  }

  private static func isSafeComponent(_ value: String) -> Bool {
    guard !value.isEmpty, value != ".", value != ".." else { return false }
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
    return value.unicodeScalars.allSatisfy(allowed.contains) && !value.contains("..")
  }
}

public enum WorkspaceError: Error, Equatable {
  case appGroupUnavailable(String)
  case invalidJobID(String)
}
