import CryptoKit
import Foundation

/// 북마크 접근의 좁은 원시 조작 — live 조립이 받는 문(S2A-2 마지막 단위).
///
/// 실물 저장소와 시험 가짜가 **같은 조립**을 지나므로 배선이 두 벌로 갈라지지
/// 않는다. peek(1단계 복원)과 settle(2단계 검증·갱신)의 순서 계약은 이 문을
/// 통해서만 시험된다.
public protocol BookmarkAccess: Sendable {
  func peekStoredBookmarks(prefix: String) throws -> [SecurityScopedBookmarkStore.PeekedBookmark]
  func peek(key: String) throws -> SecurityScopedBookmarkStore.PeekedBookmark?
  func settle(_ bookmark: SecurityScopedBookmarkStore.PeekedBookmark) throws -> URL
}

/// 북마크 저장소 — 실물 `BookmarkAccess`.
///
/// `@unchecked Sendable`의 근거: 유일한 상태가 `UserDefaults`에 대한 불변
/// 참조뿐이고 UserDefaults는 문서화된 스레드 안전 저장소다(내부 잠금).
public struct SecurityScopedBookmarkStore: BookmarkAccess, @unchecked Sendable {
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

  /// 1단계 복원의 결과 — URL만 돌려받았고, 아래의 명시적 검증·갱신은
  /// 아직 일어나지 않았다.
  public struct PeekedBookmark: Equatable, Sendable {
    public let key: String
    public let url: URL
    public let isStale: Bool

    public init(key: String, url: URL, isStale: Bool) {
      self.key = key
      self.url = url
      self.isStale = isStale
    }
  }

  /// 북마크에서 URL만 되살린다(S2A-2 마지막 단위).
  ///
  /// 이 단계가 하지 **않는** 것이 계약이다: stale 갱신(fileExists·북마크
  /// 재생성·저장)과 symlink 검사(resourceValues) 같은 명시적 메타데이터
  /// 읽기·쓰기는 scope가 걸린 뒤 `settle`에서만 한다. 되살리기 자체의
  /// 플랫폼 내부 동작까지 순수 계산이라 말하지는 않는다 — 보장은 '명시적
  /// 검증·갱신 연산이 scope 뒤로 물러났다'까지다.
  public func peek(key: String) throws -> PeekedBookmark? {
    guard let data = defaults.data(forKey: key) else { return nil }
    var stale = false
    let url = try URL(
      resolvingBookmarkData: data,
      options: .withSecurityScope,
      relativeTo: nil,
      bookmarkDataIsStale: &stale
    )
    return PeekedBookmark(key: key, url: url, isStale: stale)
  }

  /// 접두어로 묶은 북마크 전체의 1단계 복원 — 미디어 루트 목록의 준비 단계.
  public func peekStoredBookmarks(prefix: String) throws -> [PeekedBookmark] {
    try storedKeys(prefix: prefix).compactMap { try peek(key: $0) }
  }

  /// 2단계 정착 — scope **안에서** 검증·갱신한다.
  ///
  /// 낡았으면 되살린 자리가 실재하는지 보고 갱신 저장한다(예전 `resolve`의
  /// 계약 그대로). symlink 검사도 이 자리다. 1단계와 나눈 이유는 순서다:
  /// 이 메서드를 부르는 호출자는 이미 scope를 걸었어야 한다.
  public func settle(_ bookmark: PeekedBookmark) throws -> URL {
    if bookmark.isStale {
      guard FileManager.default.fileExists(atPath: bookmark.url.path) else {
        throw BookmarkError.staleUnresolvable(bookmark.key)
      }
      try save(bookmark.url, key: bookmark.key)
    }
    try requireNotSymlink(bookmark.url)
    return bookmark.url
  }

  /// 북마크를 되살린다 — stale이면 갱신하고, symlink면 거부한다.
  ///
  /// 낡은 UI 호출자(폴더 선택·라벨 갱신)용 그대로의 한 덩어리 길이다 — 앱
  /// 실행 경로는 `peek` → scope → `settle`의 두 단계로만 간다.
  public func resolve(key: String) throws -> URL? {
    guard let peeked = try peek(key: key) else { return nil }
    return try settle(peeked)
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
