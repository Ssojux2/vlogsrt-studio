import AppKit
import FinalCutAgentCore
import UniformTypeIdentifiers

@MainActor
public final class WorkflowViewController: NSViewController {
  private let status = NSTextField(labelWithString: "프로젝트를 여기에 드래그하세요")
  private let confirmButton = NSButton(
    title: "왕복 확인", target: nil, action: nil
  )
  private var job: RoundTripJob?

  public override func loadView() {
    let root = DropView(frame: NSRect(x: 0, y: 0, width: 420, height: 520))
    root.onDrop = { [weak self] data in self?.receive(data) ?? false }
    confirmButton.target = self
    confirmButton.action = #selector(confirmRoundTrip)
    confirmButton.isEnabled = false
    let choose = NSButton(title: "FCPXML 직접 선택", target: self, action: #selector(chooseFile))
    let stack = NSStackView(views: [status, choose, confirmButton])
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
  }

  @objc private func chooseFile() {
    let panel = NSOpenPanel()
    panel.allowedContentTypes = ["fcpxml", "fcpxmld"].compactMap {
      UTType(filenameExtension: $0)
    }
    panel.allowsMultipleSelection = false
    guard panel.runModal() == .OK, let url = panel.url,
      let resolved = try? FCPXMLDocument.resolve(url),
      let data = try? Data(contentsOf: resolved)
    else { return }
    _ = receive(data)
  }

  private func receive(_ data: Data) -> Bool {
    do {
      let workspace = try AppGroupWorkspace.live()
      let id = "roundtrip-\(UUID().uuidString)"
      _ = try workspace.createJob(id: id)
      let roundTripJob = try RoundTripJob(workspace: workspace, id: id)
      try roundTripJob.writeInput(data)
      job = roundTripJob
      status.stringValue = "수신 완료 (\(RoundTripStatus.received.rawValue)) · 왕복 대기"
      confirmButton.isEnabled = true
      return true
    } catch {
      status.stringValue = "수신 실패: \(error.localizedDescription)"
      return false
    }
  }

  @objc private func confirmRoundTrip() {
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
