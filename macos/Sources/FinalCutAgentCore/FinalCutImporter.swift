import AppKit
import Foundation

@MainActor
public final class FinalCutImporter {
  public static let bundleIdentifier = "com.apple.FinalCut"

  public init() {}

  public func sendOpenDocument(_ fcpxml: URL) async throws {
    guard
      let appURL = NSWorkspace.shared.urlForApplication(
        withBundleIdentifier: Self.bundleIdentifier
      )
    else { throw FinalCutImportError.applicationNotFound }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = true
    try await NSWorkspace.shared.open(
      [fcpxml],
      withApplicationAt: appURL,
      configuration: configuration
    )
  }
}

public enum FinalCutImportError: Error {
  case applicationNotFound
}
