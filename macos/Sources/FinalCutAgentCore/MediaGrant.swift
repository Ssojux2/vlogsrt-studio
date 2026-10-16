import CryptoKit
import Foundation

/// 미디어 권한 문서 — 앱이 스테이징하고 엔진의 비용 추정이 묶는 `media-grant.json`.
public struct MediaGrantDocument: Codable, Equatable, Sendable {
  public static let fileName = "media-grant.json"

  public let roots: [String]
  public let grantRevision: String

  /// 통신키는 소비자의 철자다 — 파이썬(`_staged_app_grant_revision`)은
  /// `grant_revision`(snake_case)을 읽는다. Swift 프로퍼티 이름이 그대로
  /// 나가면 앱이 스테이징한 문서가 엔진 쪽에서 '키가 없다'로 죽는다.
  enum CodingKeys: String, CodingKey {
    case roots
    case grantRevision = "grant_revision"
  }

  /// 낡은 camelCase 문서도 **읽는다** — 이미 잡 디렉터리에 남아 있는 기록의
  /// 정본을 잃지 않게. 쓰기는 snake_case 하나뿐이다: 두 철자가 함께 나가면
  /// 읽는 쪽이 어느 것이 정본인지 말할 수 없다.
  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    self.roots = try container.decode([String].self, forKey: .roots)
    if let revision = try container.decodeIfPresent(String.self, forKey: .grantRevision) {
      self.grantRevision = revision
      return
    }
    enum LegacyKeys: String, CodingKey {
      case grantRevision
    }
    let legacy = try decoder.container(keyedBy: LegacyKeys.self)
    self.grantRevision = try legacy.decode(String.self, forKey: .grantRevision)
  }

  public init(roots: [URL]) {
    let paths = roots
      .map { $0.resolvingSymlinksInPath().standardizedFileURL.path }
      .sorted()
    self.roots = paths
    self.grantRevision = Self.revision(rootPaths: paths)
  }

  /// 루트 경로 목록의 지문 — 엔진은 이 값을 media_grant_revision에 섞는다.
  /// (지문 재료의 키는 이 함수 안의 사정이지 통신 철자와 무관하다.)
  public static func revision(rootPaths: [String]) -> String {
    let payload = GrantRevisionPayload(rootPaths: rootPaths.sorted())
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    guard let data = try? encoder.encode(payload) else {
      preconditionFailure("GrantRevisionPayload은 항상 인코딩된다")
    }
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }

  public func write(to job: URL, fileManager: FileManager = .default) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(self)
    try data.write(to: job.appendingPathComponent(Self.fileName), options: .atomic)
  }
}

private struct GrantRevisionPayload: Codable {
  let rootPaths: [String]
}

public enum MediaGrantError: Error, Equatable, LocalizedError {
  case noAllowedRoots
  case mediaOutsideAllowedRoots([String])
  case unresolvableMediaReference(String)

  public var errorDescription: String? {
    switch self {
    case .noAllowedRoots:
      "허용된 미디어 루트가 없습니다 — 먼저 미디어 루트를 추가하세요"
    case .mediaOutsideAllowedRoots(let paths):
      "루트 밖 미디어가 있습니다: \(paths.joined(separator: ", "))"
    case .unresolvableMediaReference(let source):
      "미디어 참조를 파일로 해석할 수 없습니다: \(source)"
    }
  }
}

/// FCPXML이 참조하는 미디어 전체가 활성 루트 아래인지 검사한다.
///
/// 분석 시작 전에만 부른다 — 통과하면 루트들의 security scope를 걸고 엔진이
/// 끝날 때까지 풀지 않는다(scope 수명은 호출자가 관리한다). symlink·루트
/// 밖 참조는 실패다.
public enum MediaGrantValidator {
  public static func referencedMediaURLs(_ fcpxml: URL) throws -> [URL] {
    let data = try Data(contentsOf: fcpxml)
    let document = try XMLDocument(data: data, options: [])
    let nodes = try document.nodes(forXPath: "//media-rep[@src]")
    var urls: [URL] = []
    for node in nodes {
      guard let element = node as? XMLElement,
        let source = element.attribute(forName: "src")?.stringValue,
        !source.isEmpty
      else { continue }
      guard let url = URL(string: source), url.isFileURL else {
        throw MediaGrantError.unresolvableMediaReference(source)
      }
      urls.append(url)
    }
    return urls
  }

  /// 참조 전체가 루트 안인지 — 실패하면 예외, 성공하면 권한 문서를 돌려준다.
  public static func validate(fcpxml: URL, roots: [URL], fileManager: FileManager = .default)
    throws -> MediaGrantDocument
  {
    guard !roots.isEmpty else { throw MediaGrantError.noAllowedRoots }
    let referenced = try referencedMediaURLs(fcpxml)
    var escaped: [String] = []
    for url in referenced {
      if !SecurityScopedBookmarkStore.contains(url, under: roots, fileManager: fileManager) {
        escaped.append(url.path)
      }
    }
    if !escaped.isEmpty {
      throw MediaGrantError.mediaOutsideAllowedRoots(escaped)
    }
    return MediaGrantDocument(roots: roots)
  }
}
