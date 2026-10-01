import AppKit
import FinalCutAgentCore
import UniformTypeIdentifiers

// Workflow SDK는 XPC 응답 큐에서 initWithNibName:bundle:로 주 클래스를 만든다
// (2026-09-21 .ips). MainActor 서브클래스의 @objc init thunk에는 Swift 6가
// dispatch_assert_queue를 심으므로 상속 격리를 유지하면 그 큐에서 프로세스가
// 죽는다. 이 클래스만 격리를 내리고, UI는 구성원 단위 @MainActor로 지킨다.
// 뷰 구성은 AppKit이 메인에서 부르는 loadView에서 처음 일어난다.
public nonisolated final class WorkflowViewController: NSViewController {
  private var status: NSTextField!
  private var activeProjectLabel: NSTextField!
  private var confirmButton: NSButton!
  private var analyzeButton: NSButton!
  private var roughcutButton: NSButton!
  // 러프컷 배달 수명주기(S2C) — 재시도는 앱에, 확인은 관찰된 프로젝트 신원으로.
  private var importRetryButton: NSButton!
  private var importConfirmButton: NSButton!
  private var candidatesStack: NSStackView!
  // 자막 흐름(S4B 패널 계약) — 받은 내보내기 폴링·승인·재시도가 전부 패널에 있다.
  private var captionStatusLabel: NSTextField!
  private var captionEstimateButton: NSButton!
  private var captionApproveButton: NSButton!
  private var captionRetryButton: NSButton!
  private var openAIKeyField: NSSecureTextField!
  private var openAIKeyStatusLabel: NSTextField!
  private var openAIKeyModel: CredentialEntryModel?
  private var captionModel: CaptionPanelModel?
  private var captionPollTimer: Timer?
  /// 진행 중인 자막 job — App Group 기본값에 남아 패널을 닫아도 이어진다.
  private var captionJobID: String? {
    didSet {
      let defaults = UserDefaults(suiteName: AppGroupWorkspace.identifier)
      defaults?.set(captionJobID, forKey: "captionPanel.activeJobID")
    }
  }
  private var candidateRows: [CandidateReviewRow] = []
  private var job: RoundTripJob?
  private var activeProject: ActiveProjectSelection?
  private var timelineBridge: FinalCutTimelineBridge?
  /// 실패 안내를 이미 보여줬다 — 다음 요청은 같은 작업의 명시적 재시도다.
  private var failureRetryArmed = false
  /// 자동 새로고침 세션(S2A-3) — loadView 뒤 MainActor에서 만들어 쓴다.
  /// SDK 초기화 회귀(nonisolated init)를 그대로 두기 위해 저장값은 값 타입·
  /// nil 뿐이다.
  private var refreshSession: WorkflowRefreshSession?
  private var refreshGeneration = 0
  private var selectionModel = CandidateSelectionModel()

  @MainActor public override func loadView() {
    let root = DropView(frame: NSRect(x: 0, y: 0, width: 420, height: 520))
    status = NSTextField(labelWithString: "프로젝트를 여기에 드래그하세요")
    activeProjectLabel = NSTextField(labelWithString: "활성 프로젝트: 없음")
    activeProjectLabel.maximumNumberOfLines = 3
    confirmButton = NSButton(title: "왕복 확인", target: nil, action: nil)
    analyzeButton = NSButton(title: "AI 분석 요청", target: self, action: #selector(requestAnalysis))
    roughcutButton = NSButton(
      title: "승인 후보로 러프컷 생성",
      target: self,
      action: #selector(requestRoughcut)
    )
    roughcutButton.isEnabled = false
    importRetryButton = NSButton(
      title: "가져오기 재시도",
      target: self,
      action: #selector(retryRoughcutImport)
    )
    importRetryButton.isEnabled = false
    importConfirmButton = NSButton(
      title: "가져오기 완료로 표시",
      target: self,
      action: #selector(confirmRoughcutImport)
    )
    importConfirmButton.isEnabled = false
    // 자막 패널(S4B) — 인박스 폴링 상태·추정 요청·승인 실행·재시도의 네 버튼과
    // 상태 문면. SRT는 자동으로 가져가지 않는다: 안내 문구만 남는다.
    captionStatusLabel = NSTextField(labelWithString: "자막: 대기 중")
    captionStatusLabel.maximumNumberOfLines = 4
    captionEstimateButton = NSButton(
      title: "자막 추정 요청",
      target: self,
      action: #selector(requestCaptionEstimate)
    )
    captionEstimateButton.isEnabled = false
    captionApproveButton = NSButton(
      title: "승인하고 자막 생성",
      target: self,
      action: #selector(approveCaptionRun)
    )
    captionApproveButton.isEnabled = false
    captionRetryButton = NSButton(
      title: "자막 재시도",
      target: self,
      action: #selector(retryCaptionRun)
    )
    captionRetryButton.isEnabled = false
    // 단일 공유 OpenAI 키 설정 — STT·비전이 같은 키를 공식 OpenAI 엔드포인트로
    // 쓴다. 값은 어느 문면에도 다시 나오지 않는다(상태는 있음/없음/실패).
    openAIKeyField = NSSecureTextField()
    openAIKeyField.placeholderString = "OpenAI API 키 (STT·비전 공유)"
    let saveOpenAIKey = NSButton(
      title: "저장", target: self, action: #selector(saveOpenAIKey))
    let clearOpenAIKey = NSButton(
      title: "지우기", target: self, action: #selector(clearOpenAIKey))
    openAIKeyStatusLabel = NSTextField(labelWithString: "")
    openAIKeyStatusLabel.maximumNumberOfLines = 2
    openAIKeyModel = CredentialEntryModel(
      storage: KeychainStore(),
      account: EngineRoleCredentialStore.sharedOpenAIAccount,
      roleNoun: "OpenAI 키")
    openAIKeyModel?.refresh()
    refreshOpenAIKeyStatus()
    candidatesStack = NSStackView()
    candidatesStack.orientation = .vertical
    candidatesStack.spacing = 8
    root.onDrop = { [weak self] data in self?.receive(data) ?? false }
    confirmButton.target = self
    confirmButton.action = #selector(confirmRoundTrip)
    confirmButton.isEnabled = false
    analyzeButton.isEnabled = false
    let choose = NSButton(title: "FCPXML 직접 선택", target: self, action: #selector(chooseFile))
    let stack = NSStackView(
      views: [
        activeProjectLabel,
        status,
        choose,
        analyzeButton,
        candidatesStack,
        roughcutButton,
        NSStackView(views: [importRetryButton, importConfirmButton]),
        confirmButton,
        captionStatusLabel!,
        NSStackView(
          views: [captionEstimateButton, captionApproveButton, captionRetryButton]),
        NSStackView(views: [openAIKeyField, saveOpenAIKey, clearOpenAIKey]),
        openAIKeyStatusLabel,
      ]
    )
    stack.orientation = .vertical
    stack.spacing = 16
    stack.translatesAutoresizingMaskIntoConstraints = false
    root.addSubview(stack)
    NSLayoutConstraint.activate([
      stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
      stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
      stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 24),
    ])
    root.registerForDraggedTypes(FinalCutPasteboard.supportedTypes)
    view = root
    timelineBridge = FinalCutTimelineBridge { [weak self] selection in
      self?.activeProjectDidChange(selection)
    }
    // 자막 패널 모델 — App Group에 산 job·인박스 기록이 정본이다. 진행 중 job는
    // 기본값에서 되살려 패널을 닫았다 열어도 흐름이 이어진다.
    if let workspace = try? AppGroupWorkspace.live() {
      captionModel = CaptionPanelModel(workspace: workspace)
    }
    captionJobID = UserDefaults(suiteName: AppGroupWorkspace.identifier)?
      .string(forKey: "captionPanel.activeJobID")
    refreshCaptionPanel()
  }

  @MainActor public override func viewDidAppear() {
    super.viewDidAppear()
    timelineBridge?.start()
    if job != nil {
      // 나타난 시점의 내구 상태를 스스로 새로고친다 — 엔진 호출은 없다(S2A-3).
      startRefreshing(.analysis)
    }
    // 자막 인박스 폴링(S4B) — 공유 받기는 호스트가 스테이징만 하므로 패널이
    // 내구 기록을 읽어 변화를 안다. 폴링은 읽기만 한다.
    startCaptionPolling()
  }

  @MainActor public override func viewWillDisappear() {
    stopCaptionPolling()
    stopRefreshing()
    timelineBridge?.stop()
    super.viewWillDisappear()
  }

  @objc @MainActor private func chooseFile() {
    let panel = NSOpenPanel()
    panel.allowedContentTypes = ["fcpxml", "fcpxmld"].compactMap {
      UTType(filenameExtension: $0)
    }
    panel.allowsMultipleSelection = false
    panel.begin { [weak self] response in
      Task { @MainActor in
        guard let self else { return }
        guard response == .OK, let url = panel.url else {
          self.status.stringValue = "파일 선택을 취소했습니다"
          return
        }
        do {
          let resolved = try FCPXMLDocument.resolve(url)
          let accessed = url.startAccessingSecurityScopedResource()
          defer { if accessed { url.stopAccessingSecurityScopedResource() } }
          let data = try Data(contentsOf: resolved)
          _ = self.receive(data)
        } catch {
          self.status.stringValue = "FCPXML 읽기 실패: \(error.localizedDescription)"
        }
      }
    }
  }

  @MainActor private func receive(_ data: Data) -> Bool {
    do {
      guard let activeProject else { throw ActiveProjectError.noActiveProject }
      _ = try FCPXMLProjectGate.validate(data, activeProject: activeProject)
      let workspace = try AppGroupWorkspace.live()
      let id = "roundtrip-\(UUID().uuidString)"
      _ = try workspace.createJob(id: id)
      let roundTripJob = try RoundTripJob(workspace: workspace, id: id)
      try roundTripJob.writeProjectSelection(activeProject)
      try roundTripJob.writeInput(data)
      job = roundTripJob
      status.stringValue = "수신 완료 (\(RoundTripStatus.received.rawValue)) · 왕복 대기"
      confirmButton.isEnabled = true
      analyzeButton.isEnabled = true
      roughcutButton.isEnabled = false
      clearCandidates()
      selectionModel = CandidateSelectionModel()
      startRefreshing(.analysis)
      return true
    } catch {
      status.stringValue = "수신 실패: \(error.localizedDescription)"
      return false
    }
  }

  @MainActor private func activeProjectDidChange(_ selection: ActiveProjectSelection?) {
    let oldUID = activeProject?.projectUID
    activeProject = selection
    guard let selection else {
      try? ActiveSelectionRegistry.live().invalidate()
      activeProjectLabel.stringValue = "활성 프로젝트: 없음"
      stopRefreshing()
      job = nil
      confirmButton.isEnabled = false
      analyzeButton.isEnabled = false
      roughcutButton.isEnabled = false
      clearCandidates()
      status.stringValue = "Final Cut 타임라인에 프로젝트를 열어주세요"
      return
    }
    do {
      try ActiveSelectionRegistry.live().activate(selection)
    } catch {
      status.stringValue = "활성 프로젝트 세대 기록 실패: \(error.localizedDescription)"
      return
    }
    activeProjectLabel.stringValue =
      "활성 프로젝트: \(selection.projectName)\nUID: \(selection.projectUID)\n"
      + "시작 \(selection.timelineStart.secondsText) · 길이 \(selection.duration.secondsText)"
      + " · 프레임 \(selection.frameDuration.secondsText)"
    // 활성 UID가 자막 인박스의 키다 — 프로젝트가 바뀌면 받은 내보내기 대기도
    // 다시 잰다(S4B).
    refreshCaptionPanel()
    if let oldUID, oldUID != selection.projectUID {
      // 분석 job의 드리프트 규율은 그대로다 — 단 배달 확인 대기 중이라면 새로
      // 열린 프로젝트가 러프컷 결과일 수 있으니 확인 자격만 다시 잰다(S2C).
      if let job, let record = RoughcutDeliveryStore.current(jobURL: job.directoryURL),
        record.state == .importRequested || record.state == .importSent
      {
        refreshImportConfirmEligibility(job)
        return
      }
      stopRefreshing()
      job = nil
      confirmButton.isEnabled = false
      analyzeButton.isEnabled = false
      roughcutButton.isEnabled = false
      clearCandidates()
      status.stringValue = "활성 프로젝트가 바뀌어 기존 분석 작업을 폐기했습니다"
    }
  }

  @MainActor private func movePlayheadToCandidate(_ start: RationalTime) {
    timelineBridge?.movePlayhead(to: start)
  }

  @objc @MainActor private func requestAnalysis() {
    guard let job else { return }
    // 취소된 작업은 다시 부르지 않는다 — 취소 뒤 새 호출·sync·import 차단.
    if CancellationStore.isCancelled(jobURL: job.directoryURL) {
      status.stringValue = "취소된 작업입니다 — 새 분석을 시작하려면 새 작업을 만드세요"
      analyzeButton.isEnabled = false
      return
    }
    do {
      // 기록 상태를 공유 라우터로 정규화한다 — 모호한 기록(낡음·진행 중·취소·
      // 깨진 추정/시도)은 후보 검토도 재실행도 켜지 않고 정직한 요약으로 정지한다.
      let route = AnalysisReviewRouter.route(
        record: job.readAnalysisRecord(),
        candidatesFileExists: FileManager.default.fileExists(atPath: job.candidatesURL.path),
        retryArmed: failureRetryArmed
      )
      switch route {
      case .freshJob:
        failureRetryArmed = false
        if FileManager.default.fileExists(atPath: job.analysisEstimateURL.path),
          !FileManager.default.fileExists(atPath: job.analysisAuthorizationURL.path)
        {
          try presentAuthorization(job)
          return
        }
        try openAgentURL(
          AgentURL.analyzeURL(jobID: job.id),
          pending: "비용 추정·분석 요청됨",
          monitor: .analysis,
          baselineAttemptID: currentAttemptID())
      case .reviewCandidates(let outcome):
        failureRetryArmed = false
        if AnalysisOutcome.shouldLoadCandidates(
          outcome: outcome,
          candidatesFileExists: FileManager.default.fileExists(atPath: job.candidatesURL.path)
        ) {
          try loadCandidates(job)
        } else {
          clearCandidates()
          roughcutButton.isEnabled = false
        }
        status.stringValue = outcome.userFacingSummary
      case .showFailure(let outcome):
        // 검증된 실패의 재시도는 명시적이다 — 첫 실패는 안내만, 다시 누르면 엔진을
        // 다시 부른다(위 취소 게이트·승인 게이트를 그대로 지난다).
        failureRetryArmed = true
        clearCandidates()
        roughcutButton.isEnabled = false
        status.stringValue =
          outcome.userFacingSummary + " · 다시 시도하려면 AI 분석 요청을 누르세요"
      case .retryEngine:
        failureRetryArmed = false
        try openAgentURL(
          AgentURL.analyzeURL(jobID: job.id),
          pending: "분석 재시도 요청됨",
          monitor: .analysis,
          baselineAttemptID: currentAttemptID())
      case .showUnknown:
        failureRetryArmed = false
        clearCandidates()
        roughcutButton.isEnabled = false
        status.stringValue = AnalysisOutcome.legacyUnknown.userFacingSummary
      }
    } catch {
      status.stringValue = "분석 요청 실패: \(error.localizedDescription)"
      analyzeButton.isEnabled = true
    }
  }

  @MainActor private func presentAuthorization(_ job: RoundTripJob) throws {
    let estimate = try job.readAnalysisEstimate()
    guard let cost = estimate.costUpperBound else {
      status.stringValue = "비용 가정이 없어 온라인 분석을 승인할 수 없습니다"
      return
    }
    let alert = NSAlert()
    alert.messageText = "AI 분석 비용 승인"
    alert.informativeText =
      "모델 \(estimate.model) · 전사 상한 \(estimate.transcriptionSeconds)초 · "
      + "창 \(estimate.maxWindows)개 · 이미지 \(estimate.maxImages)장 · 비용 상한 \(cost)"
    alert.addButton(withTitle: "승인하고 분석")
    alert.addButton(withTitle: "취소")
    guard let window = view.window else { return }
    // 시트의 응답도 늦은 배달이다 — 열린 순간의 job·세대로 문지기를 잡는다.
    let gate = WorkflowDeliveryGate(jobID: job.id, generation: refreshGeneration)
    alert.beginSheetModal(for: window) { [weak self] response in
      Task { @MainActor in
        guard response == .alertFirstButtonReturn, let self else { return }
        // 시트가 열려 있는 사이 활성 프로젝트가 바뀌어 job이 갈렸거나 같은 job의
        // 새 요청이 세대를 올렸으면 승인을 쓰지 않는다 — 남의 요청에 승인·실행을
        // 남기는 길이다(S2A-3).
        guard gate.accepts(currentJobID: self.job?.id, currentGeneration: self.refreshGeneration)
        else {
          // 낡은 응답은 어떤 UI 손대기 없이 조용히 버린다 — 새 job·요청의
          // 상태를 덮는 길이 여기 없어야 한다(S2A-3).
          return
        }
        do {
          try job.writeAnalysisAuthorization(
            AnalysisAuthorizationMessage(
              inputRevision: estimate.inputRevision,
              estimateRevision: estimate.estimateRevision,
              pricingSnapshotRevision: estimate.pricingSnapshotRevision,
              mediaGrantRevision: estimate.mediaGrantRevision,
              authorizedAt: ISO8601DateFormatter().string(from: Date())
            )
          )
          try self.openAgentURL(
            AgentURL.analyzeURL(jobID: job.id),
            pending: "승인된 AI 분석 요청됨",
            monitor: .analysis,
            baselineAttemptID: self.currentAttemptID()
          )
        } catch {
          self.status.stringValue = "승인 기록 실패: \(error.localizedDescription)"
        }
      }
    }
  }

  @MainActor private func loadCandidates(_ job: RoundTripJob) throws {
    // 잠시 못 읽는 스냅샷이면 이 호출이 실패로 올라가 지금 줄·선택이 그대로
    // 남는다 — 같은 리비전의 새로고침에서 선택을 보존한다(S2A-3).
    let candidates = try job.readCandidates()
    selectionModel.recordUserChoices(
      Set(candidateRows.filter(\.isApproved).map(\.candidate.candidateID)))
    let approved = selectionModel.effectiveSelection(
      resultRevision: job.readAnalysisResult()?.resultRevision,
      candidates: candidates)
    clearCandidates()
    for candidate in candidates {
      let row = CandidateReviewRow(
        candidate: candidate,
        approved: approved.contains(candidate.candidateID)
      ) { [weak self] candidate in
        guard let start = Self.rational(candidate.timelineRange.start) else { return }
        self?.movePlayheadToCandidate(start)
      }
      candidateRows.append(row)
      candidatesStack.addArrangedSubview(row.view)
    }
    roughcutButton.isEnabled = true
    status.stringValue = "컷 후보 \(candidates.count)건 · 승인할 항목만 체크하세요"
  }

  @objc @MainActor private func requestRoughcut() {
    guard let job else { return }
    if CancellationStore.isCancelled(jobURL: job.directoryURL) {
      status.stringValue = "취소된 작업입니다 — 러프컷을 만들 수 없습니다"
      roughcutButton.isEnabled = false
      return
    }
    do {
      try job.writeApprovedCandidateIDs(
        candidateRows.filter(\.isApproved).map { $0.candidate.candidateID }
      )
      try openAgentURL(
        AgentURL.roughcutURL(jobID: job.id),
        pending: "승인 후보로 러프컷 요청됨",
        monitor: .roughcut
      )
      roughcutButton.isEnabled = false
    } catch {
      status.stringValue = "러프컷 요청 실패: \(error.localizedDescription)"
    }
  }

  // ─── 러프컷 배달 수명주기(S2C) — 전송 성공은 확인이 아니다 ──────

  /// 마지막 배달 기록을 읽어 버튼·문구를 맞춘다.
  @MainActor private func refreshRoughcutDelivery(_ job: RoundTripJob) {
    guard let record = RoughcutDeliveryStore.current(jobURL: job.directoryURL) else {
      importRetryButton.isEnabled = false
      importConfirmButton.isEnabled = false
      status.stringValue = "러프컷 완료 — Final Cut에서 확인하세요"
      return
    }
    switch record.state {
    case .generated, .importRequested, .importSent:
      importRetryButton.isEnabled = true
      refreshImportConfirmEligibility(job)
      status.stringValue =
        record.state == .generated
        ? "러프컷 생성 완료 — Final Cut으로 가져갑니다(실패하면 재시도 버튼)"
        : "가져오기 요청됨 — Final Cut에서 프로젝트를 연 뒤 완료로 표시하세요"
    case .importFailed:
      importRetryButton.isEnabled = true
      importConfirmButton.isEnabled = false
      status.stringValue = "가져오기 실패 — 재시도 버튼으로 다시 보낼 수 있습니다"
    case .importConfirmed:
      importRetryButton.isEnabled = false
      importConfirmButton.isEnabled = false
      status.stringValue =
        "가져오기 확인됨 — 최종 편집 뒤 WAV+FCPXML을 공유하면 자막 job이 이어집니다"
    }
  }

  /// 확인 자격 — 활성 프로젝트가 관찰되고 원본(입력) 프로젝트가 아니어야 한다.
  @MainActor private func refreshImportConfirmEligibility(_ job: RoundTripJob) {
    let sourceUID = (try? job.readProjectSelection())?.projectUID
    importConfirmButton.isEnabled = RoughcutConfirmation.eligible(
      activeProjectUID: activeProject?.projectUID, sourceProjectUID: sourceUID)
    if importConfirmButton.isEnabled, let active = activeProject {
      status.stringValue +=
        "\n확인 대상: \(active.projectName) (\(active.projectUID.prefix(12))…)"
    }
  }

  /// 가져오기 재시도 — 앱이 같은 출력(해시 검증)을 다시 Final Cut으로 보낸다.
  @objc @MainActor private func retryRoughcutImport() {
    guard let job else { return }
    do {
      try openAgentURL(
        AgentURL.importURL(jobID: job.id),
        pending: "가져오기 재시도 요청됨"
      )
    } catch {
      status.stringValue = "재시도 요청 실패: \(error.localizedDescription)"
    }
  }

  /// 가져오기 확인 — 관찰된 결과 프로젝트 UID를 원본 job/리비전에 잇는다(S4B).
  @objc @MainActor private func confirmRoughcutImport() {
    guard let job, let active = activeProject else { return }
    do {
      let sourceUID = (try? job.readProjectSelection())?.projectUID
      guard
        RoughcutConfirmation.eligible(
          activeProjectUID: active.projectUID, sourceProjectUID: sourceUID)
      else {
        status.stringValue = "지금 열린 프로젝트로는 확인할 수 없다 — 러프컷 결과 프로젝트를 열어주세요"
        return
      }
      try RoughcutConfirmation.confirm(
        jobID: job.id,
        jobURL: job.directoryURL,
        activeProjectUID: active.projectUID,
        originMap: OriginMapStore(workspace: try AppGroupWorkspace.live())
      )
      refreshRoughcutDelivery(job)
    } catch {
      status.stringValue = "확인 기록 실패: \(error.localizedDescription)"
    }
  }

  // ─── 자막 패널(S4B) — 폴링·승인·재시도, 유료 실행은 동작으로만 ────

  /// 인박스·job 기록 폴링 — 읽기만 한다. 공유 받기 자체는 아무 것도 실행하지
  /// 않으므로 패널이 여기서 변화를 발견한다.
  @MainActor private func startCaptionPolling() {
    captionPollTimer?.invalidate()
    captionPollTimer = Timer.scheduledTimer(
      withTimeInterval: 1.5, repeats: true
    ) { [weak self] _ in
      Task { @MainActor [weak self] in self?.refreshCaptionPanel() }
    }
  }

  @MainActor private func stopCaptionPolling() {
    captionPollTimer?.invalidate()
    captionPollTimer = nil
  }

  /// 자막 상태 갱신 — 관측은 모델이 파일에서 읽는다(엔진 호출 없음).
  @MainActor private func refreshCaptionPanel() {
    guard let captionModel else { return }
    var captionJob: RoundTripJob? = nil
    if let captionJobID, let workspace = try? AppGroupWorkspace.live() {
      captionJob = try? RoundTripJob(workspace: workspace, id: captionJobID)
    }
    let phase = captionModel.observe(
      activeProjectUID: activeProject?.projectUID, job: captionJob)
    captionStatusLabel.stringValue = phase.userFacingText
    captionEstimateButton.isEnabled = {
      if case .intakeReady = phase { return true }
      return false
    }()
    captionApproveButton.isEnabled = {
      if case .estimateReady = phase { return true }
      return false
    }()
    captionRetryButton.isEnabled = {
      if case .failed = phase { return true }
      return false
    }()
    // 끝난 job는 패널에서 내린다 — 다음 공유 받기가 새 흐름으로 시작된다.
    switch phase {
    case .succeededNoSpeech, .succeededSrt:
      captionJobID = nil
    default:
      break
    }
  }

  /// 추정 요청 — 받은 내보내기를 job로 스테이징하고 추정(무료)을 호스트에 연다.
  @objc @MainActor private func requestCaptionEstimate() {
    guard let captionModel, let active = activeProject else { return }
    do {
      let action = try captionModel.requestEstimate(activeProjectUID: active.projectUID)
      guard case .openCaptionEstimate(let jobID) = action, let url = action.url else {
        captionStatusLabel.stringValue = "추정 요청을 만들지 못했다 — 다시 시도하세요"
        return
      }
      captionJobID = jobID
      try openCaptionAgentURL(url, pending: "자막 추정 요청됨")
    } catch {
      captionStatusLabel.stringValue = error.localizedDescription
    }
  }

  /// 승인 실행 — 방금 고정된 추정에 묶인 승인 문서를 쓰고 실행(유료)을 연다.
  @objc @MainActor private func approveCaptionRun() {
    guard let captionModel, let captionJobID,
      let workspace = try? AppGroupWorkspace.live(),
      let job = try? RoundTripJob(workspace: workspace, id: captionJobID)
    else { return }
    do {
      let action = try captionModel.authorizeRun(job: job)
      guard let url = action.url else {
        captionStatusLabel.stringValue = "실행 요청을 만들지 못했다 — 다시 승인하세요"
        return
      }
      try openCaptionAgentURL(url, pending: "승인됨 — 자막 실행을 시작합니다")
    } catch {
      captionStatusLabel.stringValue = error.localizedDescription
    }
  }

  /// 재시도 — 같은 유효 승인으로 다시 실행한다(승인 문서를 다시 쓰지 않는다).
  @objc @MainActor private func retryCaptionRun() {
    guard let captionModel, let captionJobID,
      let workspace = try? AppGroupWorkspace.live(),
      let job = try? RoundTripJob(workspace: workspace, id: captionJobID)
    else { return }
    do {
      let action = try captionModel.retryRun(job: job)
      guard let url = action.url else { return }
      try openCaptionAgentURL(url, pending: "자막 재시도 요청됨")
    } catch {
      captionStatusLabel.stringValue = error.localizedDescription
    }
  }

  /// 호스트 실행기 URL 열기 — 문지기는 현재 자막 job에 묶는다.
  @MainActor private func openCaptionAgentURL(_ url: URL, pending: String) throws {
    guard let context = extensionContext else {
      throw RoundTripExtensionError.noExtensionContext
    }
    captionStatusLabel.stringValue = pending
    context.open(url) { [weak self] opened in
      guard let self else { return }
      Task { @MainActor in
        if !opened {
          self.captionStatusLabel.stringValue =
            "컨테이너 앱 열기 실패 — 앱이 실행 중인지 확인하세요"
        }
        self.refreshCaptionPanel()
      }
    }
  }

  // ─── 공유 OpenAI 키 설정 — STT·비전이 같은 키를 쓴다 ────────────

  @objc @MainActor private func saveOpenAIKey() {
    guard let model = openAIKeyModel, let field = openAIKeyField else { return }
    model.updateSubmitted(field.stringValue)
    model.save()
    field.stringValue = model.submittedText
    refreshOpenAIKeyStatus()
  }

  @objc @MainActor private func clearOpenAIKey() {
    guard let model = openAIKeyModel else { return }
    model.clear()
    openAIKeyField?.stringValue = model.submittedText
    refreshOpenAIKeyStatus()
  }

  @MainActor private func refreshOpenAIKeyStatus() {
    guard let model = openAIKeyModel else { return }
    openAIKeyStatusLabel.stringValue = "\(model.status.message) · 공식 OpenAI 엔드포인트 사용"
  }

  @MainActor private func openAgentURL(
    _ url: URL,
    pending: String,
    monitor: WorkflowRefreshModel.Flavor? = nil,
    baselineAttemptID: String? = nil
  ) throws {
    guard let context = extensionContext else {
      throw RoundTripExtensionError.noExtensionContext
    }
    status.stringValue = pending + " · 진행을 자동으로 새로고칩니다"
    if let monitor {
      // 요청 세션 — 요청 순간의 마지막 시도를 baseline으로 묶어, 새 엔진이
      // 시작을 알리기 전에 낡은 종료 상태를 성공으로 소비하지 않는다(S2A-3).
      startRefreshing(monitor, baselineAttemptID: baselineAttemptID)
    }
    // 문지기는 모니터 시작 **뒤에** 잡는다 — 세대는 그 시작이 올린 값이다.
    let gate = WorkflowDeliveryGate(jobID: job?.id ?? "", generation: refreshGeneration)
    context.open(url) { [weak self] opened in
      guard let self else { return }
      Task { @MainActor in
        // 늦은 열기 결과가 새 job·새 요청의 상태를 덮는 길을 닫는다(S2A-3).
        guard
          gate.accepts(currentJobID: self.job?.id, currentGeneration: self.refreshGeneration)
        else { return }
        self.analyzeButton.isEnabled = true
        if !opened {
          self.stopRefreshing()
          self.status.stringValue = "컨테이너 앱 열기 실패"
        }
      }
    }
  }

  @MainActor private func clearCandidates() {
    candidateRows.removeAll()
    for view in candidatesStack?.arrangedSubviews ?? [] {
      candidatesStack.removeArrangedSubview(view)
      view.removeFromSuperview()
    }
  }

  // ─────────────────────────────────────────────────────────────
  //  STAB-S2A-3 — 자동 새로고침(내구 상태 읽기 전용)
  // ─────────────────────────────────────────────────────────────

  /// 요청 순간의 마지막 시도 — 재시도 세션의 baseline 재료. 진행 파일이 없는
  /// 낡은 job은 묶음 검증 기록, 그다음 시도 원장에서 얻는다(유령 완료 금지).
  @MainActor private func currentAttemptID() -> String? {
    guard let job else { return nil }
    if let attempt = WorkflowProgressStore.read(jobURL: job.directoryURL)?.attemptID {
      return attempt
    }
    if case .valid(let result) = job.readAnalysisRecord() {
      return result.attemptID
    }
    return RoundTripJob.readLatestAttemptID(directoryURL: job.directoryURL)
  }

  /// 관측을 멈추고 무효화한다 — 늦게 도착할 알림의 세대까지 끊는다.
  @MainActor private func stopRefreshing() {
    refreshGeneration += 1
    refreshSession?.stop()
  }

  /// 관측을 시작(또는 교체)한다 — 시작·교체는 이전 과제와 세대를 끊는다. 엔진
  /// 호출도 승인도 하지 않는다: 읽는 것은 job 폴더의 내구 상태뿐이다.
  @MainActor private func startRefreshing(
    _ flavor: WorkflowRefreshModel.Flavor,
    baselineAttemptID: String? = nil
  ) {
    guard let job else { return }
    refreshGeneration += 1
    let generation = refreshGeneration
    refreshSession?.stop()
    let jobID = job.id
    let jobURL = job.directoryURL
    let expectedInputRevision = RoundTripJob.readAnalysisEstimateIfPresent(
      directoryURL: jobURL
    )?.inputRevision
    let session = WorkflowRefreshSession(
      observe: { url in WorkflowDurableObservation.live(jobURL: url) },
      notify: { [weak self] state in
        Task { @MainActor in
          self?.refreshDidUpdate(state, jobID: jobID, generation: generation)
        }
      })
    refreshSession = session
    session.start(
      jobURL: jobURL,
      jobID: jobID,
      flavor: flavor,
      expectedInputRevision: expectedInputRevision,
      baselineAttemptID: baselineAttemptID)
  }

  /// 세션의 알림 — 늦게 온 옛 알림은 현재 job·세대와 신원이 다르면 버린다.
  /// 같은 job의 새 요청 세대까지 갈라 본다(옛 알림이 새 상태를 덮는 금지).
  @MainActor private func refreshDidUpdate(
    _ state: WorkflowRefreshState, jobID: String, generation: Int
  ) {
    guard let job, job.id == jobID, generation == refreshGeneration else { return }
    switch state {
    case .analyzing(let progress):
      status.stringValue = progress.userFacingProgressText
    case .lastKnown(let progress):
      status.stringValue = progress.userFacingProgressText
        + WorkflowRefreshModel.lastKnownSuffix
    case .finalizing(let progress):
      status.stringValue = progress.userFacingProgressText + " · 결과 정리 대기 중"
    case .estimateReady:
      // 비용 추정만 완료 — 승인 UI를 노출할 뿐 자동 승인·유료 실행은 없다.
      failureRetryArmed = false
      analyzeButton.isEnabled = true
      try? presentAuthorization(job)
    case .completed:
      failureRetryArmed = false
      applyAnalysisRecordRoute(job)
    case .terminated(let progress):
      failureRetryArmed = progress.status == "failed"
      clearCandidates()
      roughcutButton.isEnabled = false
      status.stringValue = progress.status == "cancelled"
        ? "분석이 취소되었습니다 — 취소된 작업은 다시 부르지 않습니다"
        : "분석이 실패했습니다 · 다시 시도하려면 AI 분석 요청을 누르세요"
    case .roughcutCompleted:
      roughcutButton.isEnabled = !candidateRows.isEmpty
      refreshRoughcutDelivery(job)
    case .cancelledJob:
      analyzeButton.isEnabled = false
      status.stringValue = "취소된 작업입니다 — 새 분석을 시작하려면 새 작업을 만드세요"
    case .manualRecovery(let text):
      analyzeButton.isEnabled = true
      status.stringValue = text
    }
  }

  /// 기록 상태를 화면에 반영하는 공유 경로 — 재실행 분기(요청) 없이 검토·실패·
  /// 모호만 다룬다. 자동 새로고침이 완료를 볼 때 쓰는 자리다.
  @MainActor private func applyAnalysisRecordRoute(_ job: RoundTripJob) {
    let route = AnalysisReviewRouter.route(
      record: job.readAnalysisRecord(),
      candidatesFileExists: FileManager.default.fileExists(atPath: job.candidatesURL.path),
      retryArmed: false)
    switch route {
    case .freshJob:
      break
    case .reviewCandidates(let outcome):
      do {
        if AnalysisOutcome.shouldLoadCandidates(
          outcome: outcome,
          candidatesFileExists: FileManager.default.fileExists(atPath: job.candidatesURL.path)
        ) {
          try loadCandidates(job)
        } else {
          clearCandidates()
          roughcutButton.isEnabled = false
        }
        status.stringValue = outcome.userFacingSummary
      } catch {
        // 잠시 못 읽는 스냅샷 — 지금 줄·선택을 그대로 두고 다음 관측을 기다린다.
        status.stringValue = "결과를 다시 읽지 못했습니다: \(error.localizedDescription)"
      }
    case .showFailure(let outcome):
      failureRetryArmed = true
      clearCandidates()
      roughcutButton.isEnabled = false
      status.stringValue = outcome.userFacingSummary + " · 다시 시도하려면 AI 분석 요청을 누르세요"
    case .retryEngine:
      break  // 새로고침 경로는 엔진을 부르지 않는다 — 재시도는 사용자의 누름이다.
    case .showUnknown:
      clearCandidates()
      roughcutButton.isEnabled = false
      status.stringValue = AnalysisOutcome.legacyUnknown.userFacingSummary
    }
  }

  private static func rational(_ text: String) -> RationalTime? {
    let parts = text.split(separator: "/")
    guard parts.count == 2, let value = Int64(parts[0]), let scale = Int32(parts[1]), scale > 0
    else { return nil }
    return RationalTime(value: value, timescale: scale)
  }

  @objc @MainActor private func confirmRoundTrip() {
    guard let job else { return }
    do {
      // extension은 XML 변환도 Final Cut 전송도 하지 않는다 —
      // 요청을 쓰고 container를 여는 것만이 extension의 일이다.
      try job.writeRequest(
        RoundTripRequest(
          jobID: job.id,
          inputFileName: RoundTripJob.inputFileName,
          outputFileName: RoundTripJob.outputFileName
        )
      )
      status.stringValue = "왕복 요청됨 (\(RoundTripStatus.roundtripRequested.rawValue))"
      confirmButton.isEnabled = false
      let url = try AgentURL.roundTripURL(jobID: job.id)
      guard let context = extensionContext else {
        throw RoundTripExtensionError.noExtensionContext
      }
      context.open(url) { [weak self] opened in
        guard !opened, let self else { return }
        Task { @MainActor in
          self.status.stringValue = "컨테이너 앱 열기 실패 — 앱을 직접 실행해 왕복을 마쳐주세요"
        }
      }
    } catch {
      status.stringValue = "요청 실패: \(error.localizedDescription)"
    }
  }
}

@MainActor
private enum RoundTripExtensionError: Error {
  case noExtensionContext
}

@MainActor
private final class CandidateReviewRow: NSObject {
  let candidate: CutCandidateMessage
  let view: NSStackView
  private let checkbox: NSButton
  private let onMove: @MainActor (CutCandidateMessage) -> Void

  var isApproved: Bool { checkbox.state == .on }

  init(
    candidate: CutCandidateMessage,
    approved: Bool,
    onMove: @escaping @MainActor (CutCandidateMessage) -> Void
  ) {
    self.candidate = candidate
    self.onMove = onMove
    checkbox = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    checkbox.state = approved ? .on : .off
    let label = NSTextField(
      wrappingLabelWithString:
        "\(candidate.reason) · confidence \(candidate.confidence) · "
        + "\(candidate.timelineRange.start)–\(candidate.timelineRange.end)\n"
        + candidate.evidence.joined(separator: ", ")
    )
    let move = NSButton(title: "playhead", target: nil, action: nil)
    view = NSStackView(views: [checkbox, label, move])
    view.orientation = .horizontal
    view.spacing = 8
    super.init()
    move.target = self
    move.action = #selector(movePlayhead)
  }

  @objc private func movePlayhead() {
    onMove(candidate)
  }
}

@MainActor
private final class DropView: NSView {
  var onDrop: ((Data) -> Bool)?

  override func draggingEntered(_ sender: any NSDraggingInfo) -> NSDragOperation {
    FinalCutPasteboard.bestData(on: sender.draggingPasteboard) == nil ? [] : .copy
  }

  override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
    guard let data = FinalCutPasteboard.bestData(on: sender.draggingPasteboard) else {
      return false
    }
    return onDrop?(data) ?? false
  }
}
