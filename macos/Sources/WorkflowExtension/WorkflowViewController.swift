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
  private var candidatesStack: NSStackView!
  private var candidateRows: [CandidateReviewRow] = []
  private var job: RoundTripJob?
  private var activeProject: ActiveProjectSelection?
  private var timelineBridge: FinalCutTimelineBridge?

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
        confirmButton,
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
  }

  @MainActor public override func viewDidAppear() {
    super.viewDidAppear()
    timelineBridge?.start()
  }

  @MainActor public override func viewWillDisappear() {
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
    if let oldUID, oldUID != selection.projectUID {
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
    do {
      if FileManager.default.fileExists(atPath: job.candidatesURL.path) {
        try loadCandidates(job)
        return
      }
      if FileManager.default.fileExists(atPath: job.analysisEstimateURL.path),
        !FileManager.default.fileExists(atPath: job.analysisAuthorizationURL.path)
      {
        try presentAuthorization(job)
        return
      }
      try openAgentURL(AgentURL.analyzeURL(jobID: job.id), pending: "비용 추정·분석 요청됨")
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
    alert.beginSheetModal(for: window) { [weak self] response in
      Task { @MainActor in
        guard response == .alertFirstButtonReturn, let self else { return }
        do {
          try job.writeAnalysisAuthorization(
            AnalysisAuthorizationMessage(
              inputRevision: estimate.inputRevision,
              estimateRevision: estimate.estimateRevision,
              authorizedAt: ISO8601DateFormatter().string(from: Date())
            )
          )
          try self.openAgentURL(
            AgentURL.analyzeURL(jobID: job.id),
            pending: "승인된 AI 분석 요청됨"
          )
        } catch {
          self.status.stringValue = "승인 기록 실패: \(error.localizedDescription)"
        }
      }
    }
  }

  @MainActor private func loadCandidates(_ job: RoundTripJob) throws {
    clearCandidates()
    let candidates = try job.readCandidates()
    for candidate in candidates {
      let row = CandidateReviewRow(candidate: candidate) { [weak self] candidate in
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
    do {
      try job.writeApprovedCandidateIDs(
        candidateRows.filter(\.isApproved).map { $0.candidate.candidateID }
      )
      try openAgentURL(
        AgentURL.roughcutURL(jobID: job.id),
        pending: "승인 후보로 러프컷 요청됨"
      )
      roughcutButton.isEnabled = false
    } catch {
      status.stringValue = "러프컷 요청 실패: \(error.localizedDescription)"
    }
  }

  @MainActor private func openAgentURL(_ url: URL, pending: String) throws {
    guard let context = extensionContext else {
      throw RoundTripExtensionError.noExtensionContext
    }
    status.stringValue = pending + " · 완료 후 AI 분석 요청을 다시 눌러 새로고침"
    context.open(url) { [weak self] opened in
      guard let self else { return }
      Task { @MainActor in
        self.analyzeButton.isEnabled = true
        if !opened { self.status.stringValue = "컨테이너 앱 열기 실패" }
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
    onMove: @escaping @MainActor (CutCandidateMessage) -> Void
  ) {
    self.candidate = candidate
    self.onMove = onMove
    checkbox = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    checkbox.state = candidate.selectedByDefault ? .on : .off
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
