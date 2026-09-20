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
    guard isSafeComponent(id) else { throw WorkspaceError.invalidJobID(id) }
    let jobs = rootURL.appendingPathComponent("Jobs", isDirectory: true)
    let job = jobs.appendingPathComponent(id, isDirectory: true)
    try fileManager.createDirectory(at: job, withIntermediateDirectories: true)
    return job
  }

  private func isSafeComponent(_ value: String) -> Bool {
    guard !value.isEmpty, value != ".", value != ".." else { return false }
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
    return value.unicodeScalars.allSatisfy(allowed.contains) && !value.contains("..")
  }
}

public enum WorkspaceError: Error, Equatable {
  case appGroupUnavailable(String)
  case invalidJobID(String)
}
