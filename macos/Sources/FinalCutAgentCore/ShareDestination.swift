import CryptoKit
import Foundation

/// AI Captions 사용자 지정 공유 대상(.fcpxdest)의 번들 리소스와 설치 상태.
///
/// 대상 번들은 앱 Resources에 싣고 사용자 폴더의 설치 위치와 대조해 상태를
/// 보여준다. 설치는 사람이 한다 — 앱이 몰래 설치하지 않는다(공유 대상이 곧
/// 유료 호출의 출발점이기 때문이다).
public struct ShareDestination: Sendable {
  public static let bundleName = "AI Captions.fcpxdest"

  /// 번들 리소스의 대상 번들 URL — 앱 번들 안의 정본.
  public let bundledURL: URL

  /// 사용자 도메인의 설치 위치 — 커스텀 공유 대상이 사는 폴더.
  public let installedDirectory: URL

  public init(
    bundledURL: URL,
    installedDirectory: URL = Self.defaultInstalledDirectory()
  ) {
    self.bundledURL = bundledURL
    self.installedDirectory = installedDirectory
  }

  public static func defaultInstalledDirectory(home: URL = FileManager.default.homeDirectoryForCurrentUser)
    -> URL
  {
    home
      .appendingPathComponent("Library", isDirectory: true)
      .appendingPathComponent("Application Support", isDirectory: true)
      .appendingPathComponent("ProApps", isDirectory: true)
      .appendingPathComponent("ShareTemplates", isDirectory: true)
  }

  public var installedURL: URL {
    installedDirectory.appendingPathComponent(Self.bundleName, isDirectory: true)
  }

  public enum InstallState: Equatable, Sendable {
    case notInstalled
    case installed(matchesBundledRevision: Bool)
  }

  /// 설치 상태 — 설치돼 있으면 번들 정본과 같은 개정인지까지 보여준다.
  public func installState(fileManager: FileManager = .default) -> InstallState {
    guard fileManager.fileExists(atPath: installedURL.path) else {
      return .notInstalled
    }
    let bundled = (try? revision(of: bundledURL)) ?? nil
    let installed = (try? revision(of: installedURL)) ?? nil
    switch (bundled, installed) {
    case (let left?, let right?):
      return .installed(matchesBundledRevision: left == right)
    default:
      return .installed(matchesBundledRevision: false)
    }
  }

  /// 대상 번들의 개정 — Info.plist 바이트의 해시.
  func revision(of destination: URL) throws -> String {
    let info = destination.appendingPathComponent("Info.plist")
    let data = try Data(contentsOf: info)
    let digest = CryptoKit.SHA256.hash(data: data)
    return digest.map { String(format: "%02x", $0) }.joined()
  }
}
