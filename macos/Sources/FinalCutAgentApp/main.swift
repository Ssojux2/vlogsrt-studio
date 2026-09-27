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
  private var apiKeyField: NSSecureTextField?
  private var apiKeyStatusLabel: NSTextField?
  private var apiKeyModel: CredentialEntryModel?
  private var roundTripCoordinator: RoundTripCoordinator?
  /// 같은 job의 동시 요청을 막는 앱 게이트(S2A-1) — 청구는 스테이징·상태 쓰기
  /// 보다 먼저고, 해제는 어떤 끝에서든 일어난다. 실행 중 job의 취소 문맥은
  /// job별 판(engineJobBoard)이 든다 — 한 job이 끝나도 다른 job의 취소는 산다.
  private let engineRequestGate = EngineRequestGate()
  private let engineJobBoard = EngineJobBoard()

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
    // API 키 입력(S2A-2) — 보안 필드가 값을 감싸고, 모델이 문면·수명을 정한다.
    // 저장된 값은 화면 어디에도 다시 나오지 않는다: 상태는 있음/없음/실패뿐.
    let keyField = NSSecureTextField()
    keyField.placeholderString = "OpenAI API 키 (유료 실행용)"
    apiKeyField = keyField
    let saveKey = NSButton(
      title: "API 키 저장",
      target: self,
      action: #selector(saveApiKey)
    )
    let keyStatus = NSTextField(labelWithString: "")
    keyStatus.maximumNumberOfLines = 2
    apiKeyStatusLabel = keyStatus
    apiKeyModel = CredentialEntryModel(
      storage: KeychainStore(), account: Self.apiKeyAccount
    )
    apiKeyModel?.refresh()
    refreshApiKeyStatus()
    let stack = NSStackView(
      views: [
        label,
        choose,
        mediaRootsLabel!,
        NSStackView(views: [addRoot, removeRoot]),
        shareDestinationLabel!,
        revealDestination,
        NSStackView(views: [keyField, saveKey]),
        keyStatus,
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
        // 소유권 청구가 모든 스테이징·상태 쓰기보다 앞선다(S2A-1). job 디렉터리는
        // URL 계산으로만 정한다 — 청구 전에 아무것도 만들지 않는다. 같은 job의
        // 동시 요청은 본문을 시작하지 않고 alreadyRunning으로 물러나며, 첫 소유권·
        // 취소 기록·실행을 건드리지 않는다.
        let outcome = try await engineRequestGate.perform(jobURL: job.directoryURL) {
          let ownershipKey = EngineRequestGate.ownershipKey(for: job.directoryURL)
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
          currentJobLabel?.stringValue = "현재 작업: \(jobID) (\(operation.rawValue))"
          defer {
            // 이 job만 판에서 내린다 — 다른 job이 돌고 있으면 그 취소 문맥과
            // '현재 작업' 표시는 그대로 산다(S2A-1).
            engineJobBoard.end(key: ownershipKey)
            if let running = engineJobBoard.current {
              currentJobLabel?.stringValue = "현재 작업: \(running.jobID)"
            } else {
              currentJobLabel?.stringValue = "현재 작업: 없음"
            }
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
          let context = EngineSessionContext(
            operation: operation,
            jobID: jobID,
            appGroupJob: job.directoryURL,
            externalJob: externalJob,
            plan: plan,
            selectionRevision: selectionRevision,
            registry: registry
          )
          // 실행 마커는 계획이 정해진 뒤 — 취소 기록이 실행 경로와 미러 대상을
          // 실행 때의 값으로만 알아야 한다(S1A-R1). 취소 시점에 북마크를 다시 풀면
          // 바뀐·없는 북마크가 번들 취소 기록조차 막는다. job별 판에 올린다 — 다른
          // job이 함께 돌아도 이 취소 문맥은 이 job이 끝날 때만 사라진다.
          engineJobBoard.begin(
            key: ownershipKey,
            context: EngineJobBoard.Context(
              jobID: jobID,
              jobURL: job.directoryURL,
              runningJob: context.runningJobURL,
              externalJob: context.externalJob
            )
          )
          let sessionOutcome = try await runner.run(
            context,
            importRoughcut: { url in
              try await FinalCutImporter().sendOpenDocument(url)
            }
          )
          statusLabel?.stringValue = Self.describe(sessionOutcome, operation: operation, jobID: jobID)
          // 종료 코드 0만으로 전체 성공으로 말하지 않는다 — 완료한 분석의 요약은
          // 집계가 말한다: 성공·부분 실패·알 수 없음 모두 그 요약으로 보여준다(S1B-R1).
          if case .completed = sessionOutcome, operation == .analyze {
            switch job.readAnalysisRecord() {
            case .valid(let result):
              statusLabel?.stringValue =
                "\(Self.describe(sessionOutcome, operation: operation, jobID: jobID)) · "
                + AnalysisOutcome.make(result).userFacingSummary
            case .unknown:
              statusLabel?.stringValue =
                "\(Self.describe(sessionOutcome, operation: operation, jobID: jobID)) · "
                + AnalysisOutcome.legacyUnknown.userFacingSummary
            case .fresh:
              break  // 기록이 없는 새 작업 — 일반 완료 문구 그대로 둔다.
            }
          }
          if case .unavailable = sessionOutcome {
            NSAlert(
              error: AppError.engineUnavailable(
                Self.describe(sessionOutcome, operation: operation, jobID: jobID)
              )
            ).runModal()
          }
          return sessionOutcome
        }
        if case .alreadyRunning = outcome {
          statusLabel?.stringValue = Self.describe(outcome, operation: operation, jobID: jobID)
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
    case .alreadyRunning:
      "이미 실행 중인 작업이다 — 중복 요청은 실행하지 않았다: \(jobID)"
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

  @objc private func saveApiKey() {
    guard let model = apiKeyModel, let field = apiKeyField else { return }
    // 필드가 모델의 유일한 입력원 — 성공 저장 뒤 모델이 지운 값을 필드에 되돌린다.
    model.updateSubmitted(field.stringValue)
    model.save()
    field.stringValue = model.submittedText
    refreshApiKeyStatus()
  }

  private func refreshApiKeyStatus() {
    guard let model = apiKeyModel else { return }
    apiKeyStatusLabel?.stringValue = "API 키: \(model.status.message)"
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
    guard let current = engineJobBoard.current else {
      statusLabel?.stringValue = "취소할 실행 중 작업이 없습니다"
      return
    }
    do {
      // 묶음 대상은 실행 때의 문맥 그대로 — 북마크를 다시 풀지 않는다(S1A-R1).
      // 바뀐·없는 북마크가 번들 취소 기록을 막거나 엉뚱한 미러로 보내면 안 된다.
      try RunningJobCancellation(
        appGroupJob: current.jobURL,
        externalJob: current.externalJob,
        runningJob: current.runningJob
      ).record(
        reason: "user",
        requestedAt: ISO8601DateFormatter().string(from: Date())
      )
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
