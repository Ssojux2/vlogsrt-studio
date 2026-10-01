import AppKit
import CryptoKit
import FinalCutAgentCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  private static let developmentWorkspaceBookmark = "developmentEngineWorkspace"
  private var window: NSWindow?
  private var statusLabel: NSTextField?
  private var mediaRootsLabel: NSTextField?
  private var shareDestinationLabel: NSTextField?
  private var currentJobLabel: NSTextField?
  private var apiKeyField: NSSecureTextField?
  private var apiKeyStatusLabel: NSTextField?
  private var apiKeyModel: CredentialEntryModel?
  private var visionKeyField: NSSecureTextField?
  private var visionKeyStatusLabel: NSTextField?
  private var visionKeyModel: CredentialEntryModel?
  private var visionEndpointField: NSTextField?
  private var visionEndpointStatusLabel: NSTextField?
  private var visionEndpointModel: VisionEndpointEntryModel?
  private var roundTripCoordinator: RoundTripCoordinator?
  /// 같은 job의 동시 요청을 막는 앱 게이트(S2A-1) — 청구는 스테이징·상태 쓰기
  /// 보다 먼저고, 해제는 어떤 끝에서든 일어난다. 실행 중 job의 취소 문맥은
  /// job별 판(engineJobBoard)이 든다 — 한 job이 끝나도 다른 job의 취소는 산다.
  private let engineRequestGate = EngineRequestGate()
  private let engineJobBoard = EngineJobBoard()
  // 자막 job(S4B) — 공유 받기로 생기고, 추정→승인→실행이 앱 창에서 이어진다.
  private var captionJob: RoundTripJob?
  private var captionStatusLabel: NSTextField?
  private var captionApproveButton: NSButton?
  private var captionRevealButton: NSButton?
  private var captionSRTURL: URL?

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
    // 자막 흐름(S4B) — 공유 받기가 추정을 자동으로 돌리고, 승인 버튼이 실행을
    // 연다. SRT는 자동으로 가져오지 않는다: 보이기 버튼과 안내 문구만 있다.
    let captionHeader = NSTextField(labelWithString: "자막: 공유 받기(WAV+FCPXML)로 시작합니다")
    captionHeader.maximumNumberOfLines = 3
    captionStatusLabel = captionHeader
    let approve = NSButton(
      title: "자막 승인·실행",
      target: self,
      action: #selector(approveCaptionRun)
    )
    approve.isEnabled = false
    captionApproveButton = approve
    let reveal = NSButton(
      title: "SRT Finder에 보이기",
      target: self,
      action: #selector(revealCaptionSRT)
    )
    reveal.isEnabled = false
    captionRevealButton = reveal
    // 역할 자격 증명 입력(S2A-2 단위2) — STT·비전 키는 각자 보안 필드·모델·
    // 계정을 가진다. 저장된 값은 화면 어디에도 다시 나오지 않는다: 상태는
    // 있음/없음/실패뿐이다. 한 역할을 지워도 다른 역할은 그대로 산다.
    let secureStore = KeychainStore()
    let keyField = NSSecureTextField()
    keyField.placeholderString = "OpenAI API 키 (STT·유료 실행용)"
    apiKeyField = keyField
    let saveKey = NSButton(
      title: "저장",
      target: self,
      action: #selector(saveApiKey)
    )
    let clearKey = NSButton(
      title: "지우기",
      target: self,
      action: #selector(clearApiKey)
    )
    let keyStatus = NSTextField(labelWithString: "")
    keyStatus.maximumNumberOfLines = 2
    apiKeyStatusLabel = keyStatus
    apiKeyModel = CredentialEntryModel(
      storage: secureStore, account: EngineRoleCredentialStore.sttAccount
    )
    apiKeyModel?.refresh()
    refreshApiKeyStatus()

    let visionField = NSSecureTextField()
    visionField.placeholderString = "Google API 키 (비전·유료 분석용)"
    visionKeyField = visionField
    let saveVisionKey = NSButton(
      title: "저장",
      target: self,
      action: #selector(saveVisionKey)
    )
    let clearVisionKey = NSButton(
      title: "지우기",
      target: self,
      action: #selector(clearVisionKey)
    )
    let visionStatus = NSTextField(labelWithString: "")
    visionStatus.maximumNumberOfLines = 2
    visionKeyStatusLabel = visionStatus
    visionKeyModel = CredentialEntryModel(
      storage: secureStore,
      account: EngineRoleCredentialStore.visionAccount,
      roleNoun: "비전 키"
    )
    visionKeyModel?.refresh()
    refreshVisionKeyStatus()

    // 비전 base URL — 사람이 직접 입력한다(기본값·프리셋 없음). https여야 하고
    // 저장 전에 검증한다. 저장된 값은 앱 재시작에도 살지만 문면에는 유무만
    // 나간다 — 오류 문면은 URL을 절대 되뇌지 않는다.
    let endpointLabel = NSTextField(labelWithString: "비전 API base URL (https)")
    let endpointField = NSTextField()
    endpointField.placeholderString = "https://… (예: 호스트/v1 — 유료 분석이 쓸 엔드포인트)"
    visionEndpointField = endpointField
    let saveEndpoint = NSButton(
      title: "저장",
      target: self,
      action: #selector(saveVisionEndpoint)
    )
    let clearEndpoint = NSButton(
      title: "지우기",
      target: self,
      action: #selector(clearVisionEndpoint)
    )
    let endpointStatus = NSTextField(labelWithString: "")
    endpointStatus.maximumNumberOfLines = 2
    visionEndpointStatusLabel = endpointStatus
    visionEndpointModel = VisionEndpointEntryModel(
      storage: secureStore,
      knownSecrets: { [weak self] in try self?.gatherEndpointSecretMaterial() ?? [] }
    )
    visionEndpointModel?.refresh()
    refreshVisionEndpointStatus()
    let stack = NSStackView(
      views: [
        label,
        choose,
        mediaRootsLabel!,
        NSStackView(views: [addRoot, removeRoot]),
        shareDestinationLabel!,
        revealDestination,
        NSStackView(views: [keyField, saveKey, clearKey]),
        keyStatus,
        NSStackView(views: [visionField, saveVisionKey, clearVisionKey]),
        visionStatus,
        endpointLabel,
        NSStackView(views: [endpointField, saveEndpoint, clearEndpoint]),
        endpointStatus,
        currentJobLabel!,
        cancelButton,
        captionHeader,
        NSStackView(views: [approve, reveal]),
      ]
    )
    stack.orientation = .vertical
    stack.spacing = 12
    let controller = NSViewController()
    controller.view = stack
    let window = NSWindow(contentViewController: controller)
    window.setContentSize(NSSize(width: 520, height: 700))
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

  /// 자막 공유 받기(S4B): 최종 프로젝트 UID로 원본 러프컷 연결을 찾고, 매핑이
  /// 확정돼 있을 때만 job을 만든다. 모르는·모호한 UID는 조용히 붙지 않고 실패로
  /// 닫힌다 — 마지막 job에 몰래 연결되는 길이 없다.
  private func handleSharedFiles(_ urls: [URL]) {
    do {
      let workspace = try AppGroupWorkspace.live()
      let export = try ShareInbox.classify(urls)
      let xmlData = try Data(contentsOf: FCPXMLDocument.resolve(export.fcpxml))
      guard let uid = ShareInbox.finalProjectUID(xmlData) else {
        throw AppError.captionOriginUnresolved("최종 FCPXML에 프로젝트 UID가 없다")
      }
      let mapping = OriginMapStore(workspace: workspace)
      guard let origin = try mapping.lookup(projectUID: uid) else {
        throw AppError.captionOriginUnresolved(
          "이 프로젝트(\(uid.prefix(12))…)의 원본 러프컷 연결이 없다 — Final Cut 확장에서"
            + " 가져오기 확인을 먼저 끝내주세요")
      }
      let jobID = "caption-\(UUID().uuidString)"
      _ = try ShareInbox.receive(
        urls: urls, workspace: workspace, jobID: jobID, origin: origin)
      captionJob = try RoundTripJob(workspace: workspace, id: jobID)
      captionSRTURL = nil
      captionRevealButton?.isEnabled = false
      captionApproveButton?.isEnabled = false
      captionStatusLabel?.stringValue =
        "공유 받기 완료(원본: \(origin.originJobID)) — 자막 추정을 시작합니다"
      runCaptionOperation(.captionEstimate, job: captionJob!)
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
      case .importJob(let jobID):
        handleImportRetry(jobID: jobID)
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
        let ownershipKey = EngineRequestGate.ownershipKey(for: job.directoryURL)
        // 준비 본문은 core의 조정자가 가진다(S2A-2 마지말 단위) — 앱은 배선과
        // 표시만 한다. 같은 배선이 시험을 통과한 조정자다(복사한 시험 논리가 아니다).
        let bookmarkStore = try SecurityScopedBookmarkStore()
        var sessionStarted = false
        let dependencies = EngineRequestCoordinator.Dependencies.live(
          bookmarkAccess: bookmarkStore,
          loadCredentials: {
            EngineRoleCredentialStore(storage: KeychainStore()).loadSnapshot()
          },
          executor: ProcessEngineExecutor(),
          // 러프컷 배달은 기록을 남기는 앱 경계를 지난다(S2C) — 전송 성공만으로
          // 확인이라 말하지 않는다.
          importRoughcut: { url in
            try await Self.deliverRoughcut(output: url)
          }
        ).attachingSessionStart { context in
          sessionStarted = true
          self.currentJobLabel?.stringValue = "현재 작업: \(jobID) (\(operation.rawValue))"
          // 실행 마커는 계획이 정해진 뒤 — 취소 기록이 실행 경로와 미러 대상을
          // 실행 때의 값으로만 알아야 한다(S1A-R1). job별 판에 올린다 — 다른
          // job이 함께 돌아도 이 취소 문맥은 이 job이 끝날 때만 사라진다.
          self.engineJobBoard.begin(
            key: ownershipKey,
            context: EngineJobBoard.Context(
              jobID: jobID,
              jobURL: job.directoryURL,
              runningJob: context.runningJobURL,
              externalJob: context.externalJob
            )
          )
        }
        let coordinator = EngineRequestCoordinator(dependencies: dependencies)
        defer {
          // 세션이 시작됐던 job만 판에서 내린다 — 준비 실패는 아무 표시도 남기지
          // 않고, 다른 job이 돌고 있으면 그 취소 문맥과 표시는 그대로 산다(S2A-1).
          if sessionStarted {
            engineJobBoard.end(key: ownershipKey)
            if let running = engineJobBoard.current {
              currentJobLabel?.stringValue = "현재 작업: \(running.jobID)"
            } else {
              currentJobLabel?.stringValue = "현재 작업: 없음"
            }
          }
        }
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
        statusLabel?.stringValue = Self.describe(outcome, operation: operation, jobID: jobID)
        // 종료 코드 0만으로 전체 성공으로 말하지 않는다 — 완료한 분석의 요약은
        // 집계가 말한다: 성공·부분 실패·알 수 없음 모두 그 요약으로 보여준다(S1B-R1).
        if case .completed = outcome, operation == .analyze {
          switch job.readAnalysisRecord() {
          case .valid(let result):
            statusLabel?.stringValue =
              "\(Self.describe(outcome, operation: operation, jobID: jobID)) · "
              + AnalysisOutcome.make(result).userFacingSummary
          case .unknown:
            statusLabel?.stringValue =
              "\(Self.describe(outcome, operation: operation, jobID: jobID)) · "
              + AnalysisOutcome.legacyUnknown.userFacingSummary
          case .fresh:
            break  // 기록이 없는 새 작업 — 일반 완료 문구 그대로 둔다.
          }
        }
        if case .unavailable = outcome {
          NSAlert(
            error: AppError.engineUnavailable(
              Self.describe(outcome, operation: operation, jobID: jobID)
            )
          ).runModal()
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

  // ─── 자막 job 실행(S4B) — 추정과 실행이 같은 문을 지난다 ─────────

  /// 자막 조정자 호출 — 중복 게이트·job 판 배선은 분석과 같은 모양이다.
  private func runCaptionOperation(
    _ operation: DevelopmentEngineOperation, job: RoundTripJob
  ) {
    Task { @MainActor in
      do {
        let ownershipKey = EngineRequestGate.ownershipKey(for: job.directoryURL)
        var sessionStarted = false
        let dependencies = CaptionJobCoordinator.Dependencies.live(
          executor: ProcessEngineExecutor()
        ).attachingSessionStart { context in
          sessionStarted = true
          self.currentJobLabel?.stringValue = "현재 작업: \(job.id) (\(operation.rawValue))"
          self.engineJobBoard.begin(
            key: ownershipKey,
            context: EngineJobBoard.Context(
              jobID: job.id,
              jobURL: job.directoryURL,
              runningJob: context.runningJobURL,
              externalJob: context.externalJob))
        }
        let coordinator = CaptionJobCoordinator(dependencies: dependencies)
        defer {
          if sessionStarted {
            engineJobBoard.end(key: ownershipKey)
            if let running = engineJobBoard.current {
              currentJobLabel?.stringValue = "현재 작업: \(running.jobID)"
            } else {
              currentJobLabel?.stringValue = "현재 작업: 없음"
            }
          }
        }
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
        reportCaptionOutcome(outcome, operation: operation, job: job)
      } catch {
        NSAlert(error: error).runModal()
      }
    }
  }

  /// 자막 결과의 사용자 표면 — 추정은 승인을 기다리고, 실행은 상태를 말한다.
  private func reportCaptionOutcome(
    _ outcome: EngineSessionOutcome,
    operation: DevelopmentEngineOperation,
    job: RoundTripJob
  ) {
    switch outcome {
    case .completed:
      if operation == .captionEstimate {
        do {
          let estimate = try job.readCaptionEstimate()
          captionStatusLabel?.stringValue =
            "자막 추정: 오디오 \(estimate.audioSeconds)초 · 상한 $\(estimate.costUpperBound)"
            + " · 승인 대기"
          captionApproveButton?.title = "자막 승인·실행"
          captionApproveButton?.isEnabled = true
        } catch {
          captionStatusLabel?.stringValue =
            "추정 기록을 읽지 못했다: \(error.localizedDescription)"
        }
      } else {
        reportCaptionRunResult(job: job)
      }
    case .engineFailed(let status):
      let reason = job.readLastCaptionAttemptIfPresent()?.error
      if let reason, operation == .captionRun {
        captionStatusLabel?.stringValue =
          "자막 실패(엔진 \(status)): \(reason) — 같은 승인으로 다시 시도할 수 있습니다"
      } else {
        captionStatusLabel?.stringValue = "자막 실패 — 엔진 종료코드 \(status): \(job.id)"
      }
      if operation == .captionRun {
        captionApproveButton?.title = "자막 재시도"
        captionApproveButton?.isEnabled = true
      }
    case .alreadyRunning:
      captionStatusLabel?.stringValue =
        "이미 실행 중인 자막 작업이다 — 중복 요청은 실행하지 않았다: \(job.id)"
    case .cancelledBeforeRun:
      captionStatusLabel?.stringValue = "취소된 자막 작업이다: \(job.id)"
    case .stoppedBeforeSync:
      captionStatusLabel?.stringValue =
        "엔진은 끝났지만 취소되어 자막 결과를 가져오지 않았다: \(job.id)"
    case .stoppedBeforeImport, .staleSelection:
      // 자막 세션에는 가져오기·선택 드리프트가 없다 — 도달하지 않는다.
      captionStatusLabel?.stringValue = "자막 실행이 끝나지 않았다: \(job.id)"
    case .unavailable(let reason):
      captionStatusLabel?.stringValue = reason
    }
  }

  /// 실행 결과 — 성공한 무발화와 실패 전사를 갈라 보인다(S4A 계약 그대로).
  private func reportCaptionRunResult(job: RoundTripJob) {
    guard let result = job.readCaptionResultIfPresent() else {
      captionStatusLabel?.stringValue =
        "자막 실행이 끝났지만 결과 기록이 없다 — 시도 기록을 확인하세요: \(job.id)"
      return
    }
    if result.srtPath == nil {
      captionStatusLabel?.stringValue = "자막 완료: 발화 없음(성공)"
      return
    }
    // 결과의 경로는 엔진이 실행한 자리(외부 job)를 가리킨다 — 앱이 보는 산물은
    // 같은 이름으로 App Group job의 captions/ 에 동기화돼 있다(S4A).
    let name = ((result.srtPath ?? "") as NSString).lastPathComponent
    let srt = job.captionsDirectoryURL.appendingPathComponent(name)
    captionSRTURL = srt
    captionRevealButton?.isEnabled = FileManager.default.fileExists(atPath: srt.path)
    var text = "자막 완료: \(name.isEmpty ? "SRT" : name)"
    if !result.warnings.isEmpty {
      text += " · 경고 \(result.warnings.count)건: \(result.warnings.joined(separator: " · "))"
    }
    text += "\nFinal Cut Pro: File > Import > Captions → Relative to Timeline"
    captionStatusLabel?.stringValue = text
  }

  /// 승인 버튼 — 방금 읽은 추정에 묶인 승인 문서를 쓰고 실행을 연다. 재시도는
  /// 이미 승인 문서가 있으므로 그냥 다시 실행한다(엔진 저널은 승인 상태다).
  @objc private func approveCaptionRun() {
    guard let job = captionJob else { return }
    do {
      if !FileManager.default.fileExists(atPath: job.captionAuthorizationURL.path) {
        let estimate = try job.readCaptionEstimate()
        try job.writeCaptionAuthorization(
          CaptionAuthorizationMessage(
            requestRevision: estimate.requestRevision,
            estimateRevision: estimate.estimateRevision,
            pricingSnapshotRevision: estimate.pricingSnapshotRevision,
            authorizedAt: ISO8601DateFormatter().string(from: Date())
          ))
      }
      captionApproveButton?.isEnabled = false
      captionStatusLabel?.stringValue = "승인됨 — 자막 실행을 시작합니다"
      runCaptionOperation(.captionRun, job: job)
    } catch {
      NSAlert(error: error).runModal()
    }
  }

  /// SRT를 Finder에서 보여줄 뿐 가져오지 않는다 — import는 사람이 한다.
  @objc private func revealCaptionSRT() {
    guard let url = captionSRTURL else { return }
    NSWorkspace.shared.activateFileViewerSelecting([url])
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
          statusLabel?.stringValue = "취소된 작업이다 — 가져오기를 다시 하지 않는다: \(jobID)"
          return
        }
        let output = job.directoryURL.appendingPathComponent(RoundTripJob.roughcutFileName)
        _ = try await engineRequestGate.perform(jobURL: job.directoryURL) {
          try await Self.deliverRoughcut(output: output)
          return .completed
        }
        statusLabel?.stringValue =
          "러프컷 가져오기 재요청: \(jobID) — Final Cut에서 열렸는지 확인하세요"
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

  @objc private func clearApiKey() {
    guard let model = apiKeyModel, let field = apiKeyField else { return }
    model.clear()
    field.stringValue = model.submittedText
    refreshApiKeyStatus()
  }

  private func refreshApiKeyStatus() {
    guard let model = apiKeyModel else { return }
    apiKeyStatusLabel?.stringValue = "STT 키: \(model.status.message)"
  }

  @objc private func saveVisionKey() {
    guard let model = visionKeyModel, let field = visionKeyField else { return }
    model.updateSubmitted(field.stringValue)
    model.save()
    field.stringValue = model.submittedText
    refreshVisionKeyStatus()
  }

  @objc private func clearVisionKey() {
    guard let model = visionKeyModel, let field = visionKeyField else { return }
    model.clear()
    field.stringValue = model.submittedText
    refreshVisionKeyStatus()
  }

  private func refreshVisionKeyStatus() {
    guard let model = visionKeyModel else { return }
    visionKeyStatusLabel?.stringValue = model.status.message
  }

  @objc private func saveVisionEndpoint() {
    guard let model = visionEndpointModel, let field = visionEndpointField else { return }
    model.updateSubmitted(field.stringValue)
    model.save()
    field.stringValue = model.submittedText
    refreshVisionEndpointStatus()
  }

  @objc private func clearVisionEndpoint() {
    guard let model = visionEndpointModel, let field = visionEndpointField else { return }
    model.clear()
    field.stringValue = model.submittedText
    refreshVisionEndpointStatus()
  }

  private func refreshVisionEndpointStatus() {
    guard let model = visionEndpointModel else { return }
    visionEndpointStatusLabel?.stringValue = model.status.message
  }

  /// 엔드포인트 검증이 견주는 키 재료 — 저장된 두 역할 값 **와** 방금 제출된
  /// 보안 필드 값까지(S2A-2 단위2). 저장 읽기에 실패하면 던진다: 재료를 모르는
  /// 채 검증을 지나가는 것보다 실패로 닫는 쪽이 안전하다(모델이 정해진 글로
  /// 받는다). 값은 이 문 안에서만 산다.
  private func gatherEndpointSecretMaterial() throws -> [String] {
    let store = KeychainStore()
    let storedSTT = try store.load(account: EngineRoleCredentialStore.sttAccount) ?? ""
    let storedVision = try store.load(account: EngineRoleCredentialStore.visionAccount) ?? ""
    var values = [storedSTT, storedVision]
    if let submitted = apiKeyField?.stringValue, !submitted.isEmpty { values.append(submitted) }
    if let submitted = visionKeyField?.stringValue, !submitted.isEmpty {
      values.append(submitted)
    }
    return values.filter { !$0.isEmpty }
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

enum AppError: LocalizedError {
  case captionOriginUnresolved(String)
  case noDevelopmentWorkspace
  case engineUnavailable(String)
  case securityScopeUnavailable(String)
  case credentialStorageUnavailable
  case visionEndpointInvalid(String)

  var errorDescription: String? {
    switch self {
    case .captionOriginUnresolved(let message):
      message
    case .credentialStorageUnavailable:
      EngineRoleSnapshot.storageFailedMessage
    case .visionEndpointInvalid(let message):
      message
    default:
      nil  // 낡은 사례의 문면 표시 계약은 그대로 둔다
    }
  }
}

// Xcode는 main.swift 안의 @main을 "top-level code와 충돌"로 거부하고
// SPM은 top-level 진입점만 받는다. 두 빌드가 함께 쓰는 형태가 이 파일이다.
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
