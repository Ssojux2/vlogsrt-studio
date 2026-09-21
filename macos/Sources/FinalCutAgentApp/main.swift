import AppKit
import FinalCutAgentCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  private static let developmentWorkspaceBookmark = "developmentEngineWorkspace"
  private var window: NSWindow?
  private var statusLabel: NSTextField?
  private var roundTripCoordinator: RoundTripCoordinator?

  func applicationDidFinishLaunching(_ notification: Notification) {
    let label = NSTextField(labelWithString: "Final Cut Agent 작업 프로세스")
    label.alignment = .center
    let choose = NSButton(
      title: "개발용 작업 폴더 선택",
      target: self,
      action: #selector(chooseDevelopmentWorkspace)
    )
    let stack = NSStackView(views: [label, choose])
    stack.orientation = .vertical
    stack.spacing = 16
    let controller = NSViewController()
    controller.view = stack
    let window = NSWindow(contentViewController: controller)
    window.setContentSize(NSSize(width: 460, height: 220))
    window.title = "Final Cut Agent"
    window.makeKeyAndOrderFront(nil)
    self.window = window
    statusLabel = label
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
    do {
      switch try AgentURL.request(url) {
      case .roundtrip:
        handleRoundTripURL(url)
      case .analyze(let jobID):
        handleEngine(operation: .analyze, jobID: jobID)
      case .roughcut(let jobID):
        handleEngine(operation: .roughcut, jobID: jobID)
      }
    } catch {
      NSAlert(error: error).runModal()
    }
  }

  private func handleRoundTripURL(_ url: URL) {
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

  @objc private func chooseDevelopmentWorkspace() {
    let panel = NSOpenPanel()
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.allowsMultipleSelection = false
    panel.prompt = "작업 폴더 선택"
    panel.begin { [weak self] response in
      guard response == .OK, let url = panel.url else { return }
      do {
        try SecurityScopedBookmarkStore().save(
          url,
          key: Self.developmentWorkspaceBookmark
        )
        self?.statusLabel?.stringValue = "작업 폴더: \(url.path)"
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }

  private func handleEngine(operation: DevelopmentEngineOperation, jobID: String) {
    Task { @MainActor in
      do {
        let bookmark = try SecurityScopedBookmarkStore()
        guard let externalRoot = try bookmark.resolve(key: Self.developmentWorkspaceBookmark)
        else { throw AppError.noDevelopmentWorkspace }
        let accessed = externalRoot.startAccessingSecurityScopedResource()
        defer { if accessed { externalRoot.stopAccessingSecurityScopedResource() } }
        let workspace = try AppGroupWorkspace.live()
        let job = try RoundTripJob(workspace: workspace, id: jobID)
        let selectionRevision = try job.readProjectSelection().selectionRevision
        let registry = try ActiveSelectionRegistry.live()
        try registry.requireCurrent(selectionRevision)
        let externalJob = try DevelopmentJobStager.stage(
          jobID: jobID,
          appGroupJob: job.directoryURL,
          externalRoot: externalRoot
        )
        let scripts = try FileManager.default.url(
          for: .applicationScriptsDirectory,
          in: .userDomainMask,
          appropriateFor: nil,
          create: true
        )
        let invocation = try DevelopmentEngineInvocation.create(
          operation: operation,
          jobID: jobID,
          externalJob: externalJob,
          applicationScriptsDirectory: scripts
        )
        _ = try await DevelopmentEngineLauncher().run(invocation)
        try registry.requireCurrent(selectionRevision)
        try DevelopmentJobStager.syncResults(from: externalJob, to: job.directoryURL)
        if operation == .roughcut {
          try registry.requireCurrent(selectionRevision)
          try await FinalCutImporter().sendOpenDocument(
            job.directoryURL.appendingPathComponent("roughcut.fcpxml")
          )
        }
        statusLabel?.stringValue = "\(operation.rawValue) 완료: \(jobID)"
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }
}

enum AppError: Error {
  case noActiveRoughCutRevision
  case noDevelopmentWorkspace
}

// Xcode는 main.swift 안의 @main을 "top-level code와 충돌"로 거부하고
// SPM은 top-level 진입점만 받는다. 두 빌드가 함께 쓰는 형태가 이 파일이다.
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
