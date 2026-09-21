import AppKit
import FinalCutAgentCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  private var window: NSWindow?
  private var roundTripCoordinator: RoundTripCoordinator?

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
    let fileURLs = urls.filter { $0.isFileURL }
    let customURLs = urls.filter { !$0.isFileURL }
    if !fileURLs.isEmpty {
      handleSharedFiles(fileURLs)
    }
    for url in customURLs {
      handleAgentURL(url)
    }
  }

  private func handleSharedFiles(_ urls: [URL]) {
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

  private func handleAgentURL(_ url: URL) {
    let coordinator: RoundTripCoordinator
    do {
      if let existing = roundTripCoordinator {
        coordinator = existing
      } else {
        coordinator = RoundTripCoordinator(
          workspace: try AppGroupWorkspace.live(),
          importer: FinalCutImporter()
        )
        roundTripCoordinator = coordinator
      }
    } catch {
      NSAlert(error: error).runModal()
      return
    }
    // URL/Job 검증 실패만 여기서 보이고, job 안의 실패는 Result로 남는다.
    Task {
      do {
        _ = try await coordinator.handle(url: url)
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }
}

enum AppError: Error {
  case noActiveRoughCutRevision
}

// Xcode는 main.swift 안의 @main을 "top-level code와 충돌"로 거부하고
// SPM은 top-level 진입점만 받는다. 두 빌드가 함께 쓰는 형태가 이 파일이다.
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
