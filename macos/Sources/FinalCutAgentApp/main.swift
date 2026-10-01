import AppKit
import Carbon.HIToolbox
import CryptoKit
import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  창 없는 호스트 실행기(S4B 패널 계약)
//
//  사용자 겉면은 Final Cut 확장 패널(WorkflowViewController)이 전부다. 이
//  앱은 공유 받기 스테이징과 에이전트 URL 실행만 한다:
//
//  - 공유 받기(AI Captions 메뉴)는 WAV+FCPXML을 검증해 App Group의 내구
//    인박스(CaptionInbox)에 UID 하나당 한 벌으로 남긴다 — job·추정·유료
//    실행이 일어나지 않는다. 흐름은 패널의 명시적 동작으로만 열린다.
//  - vlogsrt-agent:// URL(analyze·roughcut·import·caption-estimate·
//    caption-run)은 각 조정자를 통해 엔진을 실행하고 결과를 job 폴더에
//    동기화한다 — 상태도 패널이 그 기록을 읽어 보여준다. URL 자체는
//    사라질 수 있어 깨움 신호일 뿐이고, 실행할 동작은 확장이 열기 전에
//    남긴 App Group 우편함(HostActionMailbox)에서 배타적으로 나온다.
// ─────────────────────────────────────────────────────────────

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  /// 같은 job의 동시 요청을 막는 앱 게이트(S2A-1) — 청구는 스테이징·상태 쓰기
  /// 보다 먼저고, 해제는 어떤 끝에서든 일어난다.
  private let engineRequestGate = EngineRequestGate()

  /// ExtensionContext.open은 host가 막 시작될 때 일반 URL delegate를 건너뛸 수 있다.
  /// URL launch Apple event를 launch 전부터 직접 받아 우편함 소비의 깨움 신호로 쓴다.
  func applicationWillFinishLaunching(_ notification: Notification) {
    NSAppleEventManager.shared().setEventHandler(
      self,
      andSelector: #selector(handleGetURLAppleEvent(_:withReplyEvent:)),
      forEventClass: AEEventClass(kInternetEventClass),
      andEventID: AEEventID(kAEGetURL)
    )
  }

  func applicationDidFinishLaunching(_ notification: Notification) {
    preserveLegacyJobsIfNeeded()
    // 찬 시작에서 URL이 사라진 경우 — 우편함이 곧 배달 경로다.
    drainHostActionMailbox()
  }

  @objc private func handleGetURLAppleEvent(
    _ event: NSAppleEventDescriptor,
    withReplyEvent replyEvent: NSAppleEventDescriptor
  ) {
    guard
      let raw = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
      let url = URL(string: raw)
    else { return }
    wakeUpFromAgentURL(url)
  }

  func application(_ application: NSApplication, open urls: [URL]) {
    let fileURLs = urls.filter { $0.isFileURL }
    let customURLs = urls.filter { !$0.isFileURL }
    if !fileURLs.isEmpty {
      handleSharedFiles(fileURLs)
    }
    for url in customURLs {
      wakeUpFromAgentURL(url)
    }
  }

  // ─── 확장 동작 우편함 — URL은 깨움일 뿐 권위는 우편함에 있다 ────

  /// ExtensionContext.open이 호스트를 깨울 때 URL 자체는 사라질 수 있다
  /// (찬·따뜻한 시작 모두 실측 — 권한 선택기가 뜨지 않았다). 그래서 URL
  /// 도착은 소비 신호로만 쓰고, 실행할 동작은 App Group 우편함에서
  /// 배타적으로 꺼내 기존 핸들러로 돌린다.
  private func wakeUpFromAgentURL(_ url: URL) {
    NSLog("에이전트 URL 깨움: %@", url.absoluteString)
    drainHostActionMailbox()
  }

  private func drainHostActionMailbox() {
    let drain: HostActionDrain
    do {
      drain = try HostActionMailbox.drain(workspace: try AppGroupWorkspace.live())
    } catch {
      NSLog("호스트 동작 우편함 소비 실패: %@", String(describing: error))
      return
    }
    for discard in drain.discarded {
      NSLog("호스트 동작 폐기: %@ (%@)", discard.id, discard.reason.rawValue)
    }
    for envelope in drain.delivered {
      guard let url = URL(string: envelope.url) else { continue }
      handleAgentURL(url)
    }
  }

  // ─── 공유 받기 — 스테이징만 한다, 실행은 패널 동작으로만 ────────

  /// 공유의 종류로 갈라 스테이징한다: FCPXML 하나는 AI Rough Cut 내보내기,
  /// WAV+FCPXML 짝은 AI Captions 내보내기다. 어느 쪽도 job·분석·실행을 만들지
  /// 않는다 — 흐름은 패널의 명시적 동작으로만 열린다.
  private func handleSharedFiles(_ urls: [URL]) {
    do {
      let workspace = try AppGroupWorkspace.live()
      let audio = urls.filter { $0.pathExtension.lowercased() == "wav" }
      if audio.isEmpty {
        let entry = try RoughCutInbox.stage(urls: urls, workspace: workspace)
        NSLog(
          "AI Rough Cut 내보내기 스테이징: 프로젝트 %@ · 인박스 대기(자동 실행 없음)",
          entry.projectUID)
      } else {
        let entry = try CaptionInbox.stage(urls: urls, workspace: workspace)
        NSLog(
          "AI Captions 내보내기 스테이징: 프로젝트 %@ · 인박스 대기(자동 실행 없음)",
          entry.projectUID)
      }
    } catch {
      // 창 없는 실행기 — 패널에 보일 상태가 없는 실패는 로그로 남는다.
      NSLog("공유 받기 스테이징 실패: \(error.localizedDescription)")
    }
  }

  // ─── 에이전트 URL — 패널 동작이 여기 도착한다 ──────────────────

  private func handleAgentURL(_ url: URL) {
    do {
      switch try AgentURL.request(url) {
      case .roundtrip:
        handleRoundTripURL(url)
      case .analyze(let jobID):
        handleEngine(operation: .analyze, jobID: jobID)
      case .roughcut(let jobID):
        handleEngine(operation: .roughcut, jobID: jobID)
      case .importJob(let jobID):
        handleImportRetry(jobID: jobID)
      case .captionEstimate(let jobID):
        runCaptionOperation(.captionEstimate, jobID: jobID)
      case .captionRun(let jobID):
        runCaptionOperation(.captionRun, jobID: jobID)
      case .captionDeliver(let jobID):
        deliverCaptionSRT(jobID: jobID)
      case .libraryGrant:
        grantLibraryParent()
      }
    } catch {
      NSAlert(error: error).runModal()
    }
  }

  // ─── 라이브러리 부모 권한(SRT 배달의 쓰기 자리)과 SRT 배달 ──────

  /// 첫 실행 권한 흐름의 실행 몸체 — 폴더 선택·북마크·내구 기록까지 한 문에서.
  ///
  /// 선택은 사람이 한다(호스트는 창 없는 실행기지만 파워박스는 이 프로세스의
  /// 몫이다 — 확장은 쓰기 권한을 받을 수 없다). 폴더 안에 단일 .fcpbundle가
  /// 있어야 기록으로 남는다: 라이브러리 **부모**가 아니면 폐쇄다.
  private func grantLibraryParent() {
    let panel = NSOpenPanel()
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.allowsMultipleSelection = false
    panel.message = "Final Cut 라이브러리(.fcpbundle)가 있는 폴더를 선택하세요 — SRT를 그 옆에 저장합니다"
    NSApp.activate(ignoringOtherApps: true)
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do {
      let name = try LibraryGrantStore.bundleName(
        inRoot: url.standardizedFileURL.path, fileManager: .default)
      let store = try SecurityScopedBookmarkStore()
      try store.save(url, key: LibraryGrantStore.bookmarkKey)
      try LibraryGrantStore.save(
        LibraryGrant(
          rootPath: url.standardizedFileURL.path,
          bundleName: name,
          grantedAt: ISO8601DateFormatter().string(from: Date())),
        workspace: try AppGroupWorkspace.live())
      NSLog("라이브러리 폴더 권한 기록: %@ (번들 %@)", url.path, name)
    } catch {
      NSAlert(error: error).runModal()
    }
  }

  /// SRT 배달 — job의 성공 SRT를 검증된 권한 폴더(번들 옆)에 원자적으로 놓는다.
  /// 가져오기는 하지 않는다: 사람이 File > Import > Captions로 직접 한다.
  private func deliverCaptionSRT(jobID: String) {
    Task { @MainActor in
      do {
        let workspace = try AppGroupWorkspace.live()
        let job = try RoundTripJob(workspace: workspace, id: jobID)
        let store = try SecurityScopedBookmarkStore()
        let granted = LibraryGrantStore.bookmarkedRoot(bookmarkStore: store)
        let grant = try LibraryGrantStore.validate(
          workspace: workspace, bookmarkedRoot: granted)
        let accessing = granted?.startAccessingSecurityScopedResource() ?? false
        defer {
          if accessing, let granted { granted.stopAccessingSecurityScopedResource() }
        }
        let placed = try SRTDelivery.placeBesideBundle(job: job, grant: grant)
        NSLog("SRT 배달: %@ — Final Cut Pro: File > Import > Captions → Relative to Timeline", placed.path)
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }

  private func handleRoundTripURL(_ url: URL) {
    let coordinator: RoundTripCoordinator
    do {
      coordinator = RoundTripCoordinator(
        workspace: try AppGroupWorkspace.live(),
        importer: FinalCutImporter())
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
        // 준비 본문은 core의 조정자가 가진다(S2A-2 마지말 단위) — 이 앱은 배선만
        // 한다. 같은 배선이 시험을 통과한 조정자다(복사한 시험 논리가 아니다).
        let bookmarkStore = try SecurityScopedBookmarkStore()
        let dependencies = EngineRequestCoordinator.Dependencies.live(
          bookmarkAccess: bookmarkStore,
          executor: ProcessEngineExecutor(),
          // 러프컷 배달은 기록을 남기는 앱 경계를 지난다(S2C) — 전송 성공만으로
          // 확인이라 말하지 않는다.
          importRoughcut: { url in
            try await Self.deliverRoughcut(output: url)
          }
        )
        let coordinator = EngineRequestCoordinator(dependencies: dependencies)
        let outcome = try await engineRequestGate.perform(jobURL: job.directoryURL) {
          try await coordinator.run(
            operation: operation,
            job: job,
            configuration: Self.buildConfiguration,
            engineRoot: Bundle.main.resourceURL?
              .appendingPathComponent("Engine", isDirectory: true),
            applicationScriptsDirectory: try FileManager.default.url(
              for: .applicationScriptsDirectory,
              in: .userDomainMask,
              appropriateFor: nil,
              create: true
            ),
            registry: try ActiveSelectionRegistry.live()
          )
        }
        NSLog(
          "엔진 %@ 완료: %@ — %@",
          operation.rawValue, jobID, Self.describe(outcome, operation: operation, jobID: jobID))
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

  // ─── 자막 job 실행(S4B) — 패널 동작이 연 문만 실행한다 ───────────

  /// 자막 조정자 호출 — 중복 게이트는 분석과 같은 모양이다. 상태 표면은 패널이
  /// job 기록에서 읽는다(이 앱은 결과를 로그로만 남긴다).
  private func runCaptionOperation(
    _ operation: DevelopmentEngineOperation, jobID: String
  ) {
    Task { @MainActor in
      do {
        let workspace = try AppGroupWorkspace.live()
        let job = try RoundTripJob(workspace: workspace, id: jobID)
        let dependencies = CaptionJobCoordinator.Dependencies.live(
          executor: ProcessEngineExecutor()
        )
        let coordinator = CaptionJobCoordinator(dependencies: dependencies)
        let outcome = try await engineRequestGate.perform(jobURL: job.directoryURL) {
          try await coordinator.run(
            operation: operation,
            job: job,
            configuration: Self.buildConfiguration,
            engineRoot: Bundle.main.resourceURL?
              .appendingPathComponent("Engine", isDirectory: true),
            applicationScriptsDirectory: try FileManager.default.url(
              for: .applicationScriptsDirectory,
              in: .userDomainMask,
              appropriateFor: nil,
              create: true
            )
          )
        }
        NSLog(
          "자막 %@ 완료: %@ — %@",
          operation.rawValue, jobID, Self.describe(outcome, operation: operation, jobID: jobID))
      } catch {
        NSAlert(error: error).runModal()
      }
    }
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

  // ─── 러프컷 배달(S2C) — 생성 기록 → 요청 → 전송 → 결과 기록 ────

  /// 가져오기 재시도 요청(확장의 재시도 버튼) — 재분석 없이 같은 출력을 다시
  /// 보낸다. 해시가 바뀐 출력은 여기서 거부된다(새 검토가 필요하다).
  private func handleImportRetry(jobID: String) {
    Task { @MainActor in
      do {
        let workspace = try AppGroupWorkspace.live()
        let job = try RoundTripJob(workspace: workspace, id: jobID)
        if CancellationStore.isCancelled(jobURL: job.directoryURL) {
          NSLog("취소된 작업이다 — 가져오기를 다시 하지 않는다: %@", jobID)
          return
        }
        let output = job.directoryURL.appendingPathComponent(RoundTripJob.roughcutFileName)
        _ = try await engineRequestGate.perform(jobURL: job.directoryURL) {
          try await Self.deliverRoughcut(output: output)
          return .completed
        }
        NSLog("러프컷 가져오기 재요청: %@", jobID)
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }

  /// 배달의 앱 경계 — 세션 안 자동 전송과 재시도가 같은 문을 쓴다(S2C).
  static func deliverRoughcut(output: URL) async throws {
    let jobURL = output.deletingLastPathComponent()
    let jobID = jobURL.lastPathComponent
    let inputRevision =
      EngineSessionRunner.stagedInputRevision(job: jobURL, fileManager: .default) ?? ""
    let session = RoughcutImportSession(jobID: jobID, jobURL: jobURL)
    try session.recordGenerated(inputRevision: inputRevision)
    let sent = try await session.send(inputRevision: inputRevision) { url in
      try await FinalCutImporter().sendOpenDocument(url)
    }
    if !sent {
      throw AppError.engineUnavailable(
        "가져올 러프컷이 없거나 이미 확인된 출력이다 — 새로 분석해야 한다: \(jobID)")
    }
  }

  // ─── 낡은 job 보존 ────────────────────────────────────────────

  private func preserveLegacyJobsIfNeeded() {
    do {
      let workspace = try AppGroupWorkspace.live()
      let store = LegacyJobStore(workspace: workspace)
      let jobsRoot = workspace.rootURL.appendingPathComponent("Jobs", isDirectory: true)
      for jobID in try store.pendingLegacyJobIDs(in: jobsRoot) {
        try store.preserve(jobID: jobID, from: jobsRoot)
      }
    } catch {
      // 보존 실패는 시작을 막지 않는다 — 로그로만 남긴다.
      NSLog("LegacyJob 보존 실패: \(error.localizedDescription)")
    }
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

enum AppError: LocalizedError {
  case engineUnavailable(String)

  var errorDescription: String? {
    switch self {
    case .engineUnavailable(let reason):
      reason
    }
  }
}

// Xcode는 main.swift 안의 @main을 "top-level code와 충돌"로 거부하고
// SPM은 top-level 진입점만 받는다. 두 빌드가 함께 쓰는 형태가 이 파일이다.
// 창 없는 실행기다 — 사용자 겉면은 Final Cut 확장 패널에 있다.
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
