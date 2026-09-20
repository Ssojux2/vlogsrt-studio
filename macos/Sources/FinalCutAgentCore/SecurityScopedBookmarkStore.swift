import Foundation

public struct SecurityScopedBookmarkStore {
  public let defaults: UserDefaults

  public init(suiteName: String = AppGroupWorkspace.identifier) throws {
    guard let defaults = UserDefaults(suiteName: suiteName) else {
      throw BookmarkError.defaultsUnavailable(suiteName)
    }
    self.defaults = defaults
  }

  public func save(_ url: URL, key: String) throws {
    let data = try url.bookmarkData(
      options: .withSecurityScope,
      includingResourceValuesForKeys: nil,
      relativeTo: nil
    )
    defaults.set(data, forKey: key)
  }

  public func resolve(key: String) throws -> URL? {
    guard let data = defaults.data(forKey: key) else { return nil }
    var stale = false
    let url = try URL(
      resolvingBookmarkData: data,
      options: .withSecurityScope,
      relativeTo: nil,
      bookmarkDataIsStale: &stale
    )
    if stale { try save(url, key: key) }
    return url
  }
}

public enum BookmarkError: Error {
  case defaultsUnavailable(String)
}
