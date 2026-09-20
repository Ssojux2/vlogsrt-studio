import AppKit
import FinalCutAgentCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  private var window: NSWindow?

  func applicationDidFinishLaunching(_ notification: Notification) {
    let label = NSTextField(labelWithString: "Final Cut Agent 작업 프로세스")
    label.alignment = .center
    let controller = NSViewController()
    controller.view = label
    let window = NSWindow(contentViewController: controller)
    window.setContentSize(NSSize(width: 460, height: 220))
    window.title = "Final Cut Agent"
    window.makeKeyAndOrderFront(nil)
    self.window = window
  }

  func application(_ application: NSApplication, open urls: [URL]) {
    do {
      let export = try ShareInbox.classify(urls)
      let workspace = try AppGroupWorkspace.live()
      let job = try workspace.createJob(id: "caption-\(UUID().uuidString)")
      guard let defaults = UserDefaults(suiteName: AppGroupWorkspace.identifier),
        let originRevision = defaults.string(forKey: "activeOriginRevision")
      else { throw AppError.noActiveRoughCutRevision }
      _ = try ShareInbox.stageCaptionRequest(
        export,
        originRevision: originRevision,
        in: job
      )
    } catch {
      NSAlert(error: error).runModal()
    }
  }
}

enum AppError: Error {
  case noActiveRoughCutRevision
}

@main
enum FinalCutAgentMain {
  @MainActor
  static func main() {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
    withExtendedLifetime(delegate) {}
  }
}
