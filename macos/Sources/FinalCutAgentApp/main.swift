import AppKit
import CryptoKit
import FinalCutAgentCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  private static let developmentWorkspaceBookmark = "developmentEngineWorkspace"
  private static let apiKeyAccount = "engine"
  private var window: NSWindow?
  private var statusLabel: NSTextField?
  private var mediaRootsLabel: NSTextField?
  private var shareDestinationLabel: NSTextField?
  private var currentJobLabel: NSTextField?
  private var roundTripCoordinator: RoundTripCoordinator?
  private var currentEngineJob: (jobID: String, jobURL: URL)?

  func applicationDidFinishLaunching(_ notification: Notification) {
    preserveLegacyJobsIfNeeded()
    let label = NSTextField(labelWithString: "Final Cut Agent 작업 프로세스")
    label.alignment = .center
    let choose = NSButton(
      title: "개발용 작업 폴더 선택",
      target: self,
      action: #selector(chooseDevelopmentWorkspace)
    )
    mediaRootsLabel = NSTextField(labelWithString: "")
    mediaRootsLabel?.maximumNumberOfLines = 4
    let addRoot = NSButton(
      title: "미디어 루트 추가",
      target: self,
      action: #selector(addMediaRoot)
    )
    let removeRoot = NSButton(
      title: "선택 미디어 루트 삭제",
      target: self,
      action: #selector(removeMediaRoot)
    )
    shareDestinationLabel = NSTextField(labelWithString: "")
    shareDestinationLabel?.maximumNumberOfLines = 2
    let revealDestination = NSButton(
      title: "AI Captions 공유 대상 번들 열기",
      target: self,
      action: #selector(revealShareDestination)
    )
    currentJobLabel = NSTextField(labelWithString: "현재 작업: 없음")
    currentJobLabel?.maximumNumberOfLines = 2
    let cancelButton = NSButton(
      title: "현재 작업 취소",
      target: self,
      action: #selector(cancelCurrentJob)
    )
    let stack = NSStackView(
      views: [
        label,
        choose,
        mediaRootsLabel!,
        NSStackView(views: [addRoot, removeRoot]),
        shareDestinationLabel!,
        revealDestination,
        currentJobLabel!,
        cancelButton,
      ]
    )
    stack.orientation = .vertical
    stack.spacing = 12
    let controller = NSViewController()
    controller.view = stack
    let window = NSWindow(contentViewController: controller)
    window.setContentSize(NSSize(width: 460, height: 420))
    window.title = "Final Cut Agent"
    window.makeKeyAndOrderFront(nil)
    self.window = window
    statusLabel = label
    refreshMediaRootsLabel()
    refreshShareDestinationLabel()
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

  // ─── 공유 받기 — 검증이 통과해야 job이 생긴다 ───────────────────

  private func handleSharedFiles(_ urls: [URL]) {
    do {
      guard let defaults = UserDefaults(suiteName: AppGroupWorkspace.identifier),
        let originRevision = defaults.string(forKey: "activeOriginRevision")
      else { throw AppError.noActiveRoughCutRevision }
      let workspace = try AppGroupWorkspace.live()
      _ = try ShareInbox.receive(
        urls: urls,
        workspace: workspace,
        jobID: "caption-\(UUID().uuidString)",
        originRevision: originRevision
      )
      statusLabel?.stringValue = "공유 받기 완료 — 자막 작업이 스테이징됐습니다"
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

  // ─── 엔진 실행 — Release는 번들만, Debug만 개발 폴백 ─────────────

  private func handleEngine(operation: DevelopmentEngineOperation, jobID: String) {
    Task { @MainActor in
      do {
        let workspace = try AppGroupWorkspace.live()
        let job = try RoundTripJob(workspace: workspace, id: jobID)
        // 미디어 권한: 참조 전체가 활성 루트 안인지 보고 문서를 스테이징한다.
        // 현재 작업 표시는 이 검증이 **통과한 뒤에만** 남긴다 — 실패가 stale
        // 상태를 남기지 않는다.
        let bookmarkStore = try SecurityScopedBookmarkStore()
        let roots = try bookmarkStore.allowedRoots()
        let grant = try MediaGrantValidator.validate(
          fcpxml: job.inputURL, roots: roots
        )
        try grant.write(to: job.directoryURL)

        // scope는 엔진이 끝날 때까지 유지한다. 하나라도 시작에 실패하면 이미
        // 걸은 scope를 전부 풀고 실패로 닫는다 — 부분 권한으로 계속하지 않는다.
        var scopes: [URL] = []
        for root in roots {
          guard root.startAccessingSecurityScopedResource() else {
            for started in scopes {
              started.stopAccessingSecurityScopedResource()
            }
            throw AppError.securityScopeUnavailable(root.path)
          }
          scopes.append(root)
        }
        defer {
          for scope in scopes {
            scope.stopAccessingSecurityScopedResource()
          }
        }
        currentEngineJob = (jobID, job.directoryURL)
        currentJobLabel?.stringValue = "현재 작업: \(jobID) (\(operation.rawValue))"
        defer {
          currentEngineJob = nil
          currentJobLabel?.stringValue = "현재 작업: 없음"
        }

        let selectionRevision = try job.readProjectSelection().selectionRevision
        let registry = try ActiveSelectionRegistry.live()

        var externalJob: URL?
        if Self.buildConfiguration == .debug {
          if let externalRoot = try bookmarkStore.resolve(
            key: Self.developmentWorkspaceBookmark
          ) {
            externalJob = try DevelopmentJobStager.stage(
              jobID: jobID,
              appGroupJob: job.directoryURL,
              externalRoot: externalRoot
            )
          }
        }

        let engineRoot = Bundle.main.resourceURL?
          .appendingPathComponent("Engine", isDirectory: true)
        let scripts = try FileManager.default.url(
          for: .applicationScriptsDirectory,
          in: .userDomainMask,
          appropriateFor: nil,
          create: true
        )
        let apiKey = try KeychainStore().load(account: Self.apiKeyAccount)
        let plan = EngineRuntime.resolvePlan(
          configuration: Self.buildConfiguration,
          operation: operation,
          jobID: jobID,
          appGroupJob: job.directoryURL,
          engineRoot: engineRoot,
          applicationScriptsDirectory: scripts,
          developmentExternalJob: externalJob,
          apiKey: apiKey
        )
        let runner = EngineSessionRunner(executor: ProcessEngineExecutor())
        let outcome = try await runner.run(
          EngineSessionContext(
            operation: operation,
            jobID: jobID,
            appGroupJob: job.directoryURL,
            externalJob: externalJob,
            plan: plan,
            selectionRevision: selectionRevision,
            registry: registry
          ),
          importRoughcut: { url in
            try await FinalCutImporter().sendOpenDocument(url)
          }
        )
        statusLabel?.stringValue = Self.describe(outcome, operation: operation, jobID: jobID)
        if case .unavailable = outcome {
          NSAlert(
            error: AppError.engineUnavailable(
              Self.describe(outcome, operation: operation, jobID: jobID)
            )
          ).runModal()
        }
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }

  static var buildConfiguration: EngineBuildConfiguration {
    #if DEBUG
      return .debug
    #else
      return .release
    #endif
  }

  static func describe(
    _ outcome: EngineSessionOutcome, operation: DevelopmentEngineOperation, jobID: String
  ) -> String {
    switch outcome {
    case .completed:
      "\(operation.rawValue) 완료: \(jobID)"
    case .engineFailed(let status):
      "\(operation.rawValue) 실패 — 엔진 종료코드 \(status): \(jobID)"
    case .cancelledBeforeRun:
      "취소된 작업이라 실행하지 않았다: \(jobID)"
    case .stoppedBeforeSync:
      "엔진은 끝났지만 취소되어 결과를 가져오지 않았다: \(jobID)"
    case .stoppedBeforeImport:
      "결과 동기화 뒤 취소되어 Final Cut으로 보내지 않았다: \(jobID)"
    case .staleSelection:
      "활성 프로젝트가 바뀌어 취소 기록을 남겼다: \(jobID)"
    case .unavailable(let reason):
      reason
    }
  }

  // ─── UI 동작 ──────────────────────────────────────────────────

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

  @objc private func addMediaRoot() {
    let panel = NSOpenPanel()
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.allowsMultipleSelection = false
    panel.prompt = "미디어 루트 추가"
    panel.begin { [weak self] response in
      guard response == .OK, let url = panel.url else { return }
      do {
        // 키는 경로의 안전한 해시 — 같은 루트의 재추가는 갱신이 된다.
        let store = try SecurityScopedBookmarkStore()
        let key = "media-root." + Self.storageKey(for: url)
        try store.save(url, key: key)
        self?.refreshMediaRootsLabel()
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }

  @objc private func removeMediaRoot() {
    do {
      let store = try SecurityScopedBookmarkStore()
      let keys = store.storedKeys(prefix: "media-root.")
      guard let key = keys.last else {
        statusLabel?.stringValue = "지울 미디어 루트가 없습니다"
        return
      }
      store.delete(key: key)
      refreshMediaRootsLabel()
    } catch {
      NSAlert(error: error).runModal()
    }
  }

  @objc private func revealShareDestination() {
    guard
      let bundled = Bundle.main.url(
        forResource: "AI Captions", withExtension: "fcpxdest"
      )
    else {
      statusLabel?.stringValue = "번들에 AI Captions.fcpxdest가 없습니다"
      return
    }
    // Open Document — 설치는 사람이 한다: 앱이 몰래 설치하지 않는다.
    NSWorkspace.shared.open(bundled)
  }

  @objc private func cancelCurrentJob() {
    guard let current = currentEngineJob else {
      statusLabel?.stringValue = "취소할 실행 중 작업이 없습니다"
      return
    }
    do {
      let revision = EngineSessionRunner.stagedInputRevision(
        job: current.jobURL, fileManager: .default
      ) ?? String(repeating: "0", count: 64)
      let requestedAt = ISO8601DateFormatter().string(from: Date())
      let external = try SecurityScopedBookmarkStore()
        .resolve(key: Self.developmentWorkspaceBookmark)?
        .appendingPathComponent("Jobs", isDirectory: true)
        .appendingPathComponent(current.jobID, isDirectory: true)
      let message = CancellationRequestMessage(
        inputRevision: revision,
        attemptID: "*",
        requestedAt: requestedAt,
        reason: "user",
        cancellationRevision: CancellationCodec.revision(
          inputRevision: revision,
          attemptID: "*",
          requestedAt: requestedAt,
          reason: "user"
        )
      )
      try CancellationStore(
        appGroupJob: current.jobURL, externalJob: external
      ).record(message)
      statusLabel?.stringValue = "취소를 기록했습니다: \(current.jobID)"
    } catch {
      NSAlert(error: error).runModal()
    }
  }

  // ─── 표시 갱신 ────────────────────────────────────────────────

  private func refreshMediaRootsLabel() {
    do {
      let store = try SecurityScopedBookmarkStore()
      let roots = try store.allowedRoots()
      mediaRootsLabel?.stringValue = roots.isEmpty
        ? "미디어 루트: 없음 — 분석 전 추가가 필요하다"
        : "미디어 루트:\n" + roots.map { "· \($0.path)" }.joined(separator: "\n")
    } catch {
      mediaRootsLabel?.stringValue = "미디어 루트를 읽지 못했다: \(error.localizedDescription)"
    }
  }

  private func refreshShareDestinationLabel() {
    guard
      let bundled = Bundle.main.url(
        forResource: "AI Captions", withExtension: "fcpxdest"
      )
    else {
      shareDestinationLabel?.stringValue = "공유 대상 번들이 없습니다"
      return
    }
    let destination = ShareDestination(bundledURL: bundled)
    switch destination.installState() {
    case .notInstalled:
      shareDestinationLabel?.stringValue =
        "AI Captions 공유 대상: 미설치 — 버튼으로 번들을 열어 설치하세요"
    case .installed(let matches):
      shareDestinationLabel?.stringValue = matches
        ? "AI Captions 공유 대상: 설치됨(번들과 동일)"
        : "AI Captions 공유 대상: 설치됨(번들과 다른 개정)"
    }
  }

  private func preserveLegacyJobsIfNeeded() {
    do {
      let workspace = try AppGroupWorkspace.live()
      let store = LegacyJobStore(workspace: workspace)
      let jobsRoot = workspace.rootURL.appendingPathComponent("Jobs", isDirectory: true)
      for jobID in try store.pendingLegacyJobIDs(in: jobsRoot) {
        try store.preserve(jobID: jobID, from: jobsRoot)
      }
    } catch {
      // 보존 실패는 시작을 막지 않는다 — 화면에만 남긴다(아래 라벨은 아직 없다).
      NSLog("LegacyJob 보존 실패: \(error.localizedDescription)")
    }
  }

  private static func storageKey(for url: URL) -> String {
    let path = url.resolvingSymlinksInPath().standardizedFileURL.path
    let digest = CryptoKitSHA256.digest(path)
    return String(digest.prefix(24))
  }
}

/// Process 실행기를 EngineSessionRunner에 연결한다 — 앱 프로세스에서만 산다.
/// 번들 실행은 동기 봉쇄라 detached 태스크에서 돌려 메인 액터를 지킨다.
struct ProcessEngineExecutor: EngineExecuting {
  func run(_ plan: EngineLaunchPlan) async throws -> Int32 {
    switch plan {
    case .bundled(let invocation):
      let launcher = BundledEngineLauncher()
      return try await Task.detached(priority: .userInitiated) {
        try launcher.run(invocation)
      }.value
    case .development(let invocation):
      return try await DevelopmentEngineLauncher().run(invocation)
    case .unavailable(let reason):
      throw AppError.engineUnavailable(reason)
    }
  }
}

enum CryptoKitSHA256 {
  static func digest(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
  }
}

enum AppError: Error {
  case noActiveRoughCutRevision
  case noDevelopmentWorkspace
  case engineUnavailable(String)
  case securityScopeUnavailable(String)
}

// Xcode는 main.swift 안의 @main을 "top-level code와 충돌"로 거부하고
// SPM은 top-level 진입점만 받는다. 두 빌드가 함께 쓰는 형태가 이 파일이다.
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
