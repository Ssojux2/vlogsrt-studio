import CryptoKit
import Foundation

public struct SecurityScopedBookmarkStore {
  public let defaults: UserDefaults

  public init(suiteName: String = AppGroupWorkspace.identifier) throws {
    guard let defaults = UserDefaults(suiteName: suiteName) else {
      throw BookmarkError.defaultsUnavailable(suiteName)
    }
    self.defaults = defaults
  }

  /// 북마크를 저장(갱신)한다 — 같은 키의 낡은 값은 조용히 덮어쓴다.
  public func save(_ url: URL, key: String) throws {
    try requireNotSymlink(url)
    let data = try url.bookmarkData(
      options: .withSecurityScope,
      includingResourceValuesForKeys: nil,
      relativeTo: nil
    )
    defaults.set(data, forKey: key)
  }

  /// 북마크를 되살린다 — stale이면 갱신하고, symlink면 거부한다.
  public func resolve(key: String) throws -> URL? {
    guard let data = defaults.data(forKey: key) else { return nil }
    var stale = false
    let url = try URL(
      resolvingBookmarkData: data,
      options: .withSecurityScope,
      relativeTo: nil,
      bookmarkDataIsStale: &stale
    )
    if stale {
      guard FileManager.default.fileExists(atPath: url.path) else {
        throw BookmarkError.staleUnresolvable(key)
      }
      try save(url, key: key)
    }
    try requireNotSymlink(url)
    return url
  }

  public func delete(key: String) {
    defaults.removeObject(forKey: key)
  }

  public func storedKeys(prefix: String) -> [String] {
    defaults
      .dictionaryRepresentation()
      .keys
      .filter { $0.hasPrefix(prefix) }
      .sorted()
  }

  /// 허용된 루트 목록 — FCPXML이 참조하는 미디어는 이 루트들 하위여야 한다.
  public func allowedRoots() throws -> [URL] {
    try storedKeys(prefix: "media-root.").compactMap { try resolve(key: $0) }
  }

  /// 미디어 경로가 허용된 루트 하위인지 — symlink와 루트 탈출을 거부한다.
  public static func contains(
    _ mediaURL: URL, under roots: [URL], fileManager: FileManager = .default
  ) -> Bool {
    let resolved = mediaURL.resolvingSymlinksInPath().standardizedFileURL
    for root in roots {
      let confined = root.resolvingSymlinksInPath().standardizedFileURL
      if resolved.path.hasPrefix(confined.path + "/") {
        return fileManager.fileExists(atPath: resolved.path)
      }
    }
    return false
  }

  /// 북마크 grant revision — 승인·캐시 키에 묶이는 미디어 권한의 지문.
  ///
  /// 북마크 데이터 자체가 아니라 **되살린 루트 + 미디어 해시**의 해시다:
  /// 북마크 바이트는 저장 시점마다 흔들리지만 권한의 의미는 경로와 내용에 있다.
  public static func grantRevision(
    roots: [URL], mediaSHA256s: [String]
  ) -> String {
    let payload = GrantDigestPayload(
      mediaSHA256s: mediaSHA256s.sorted(),
      rootPaths: roots.map { $0.resolvingSymlinksInPath().standardizedFileURL.path }.sorted()
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    guard let data = try? encoder.encode(payload) else {
      preconditionFailure("GrantDigestPayload은 항상 인코딩된다")
    }
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }

  private func requireNotSymlink(_ url: URL) throws {
    let values = try url.resourceValues(forKeys: [.isSymbolicLinkKey])
    if values.isSymbolicLink == true {
      throw BookmarkError.symbolicLink(url.path)
    }
  }
}

private struct GrantDigestPayload: Codable {
  let mediaSHA256s: [String]
  let rootPaths: [String]
}

public enum BookmarkError: Error, Equatable {
  case defaultsUnavailable(String)
  case symbolicLink(String)
  case staleUnresolvable(String)

  public var localizedDescription: String {
    switch self {
    case .defaultsUnavailable(let suite):
      "App Group 기본 저장소를 열 수 없습니다: \(suite)"
    case .symbolicLink(let path):
      "북마크 대상이 symlink입니다 — 허용하지 않습니다: \(path)"
    case .staleUnresolvable(let key):
      "북마크가 낡았고 원본을 다시 찾을 수 없습니다: \(key)"
    }
  }
}
