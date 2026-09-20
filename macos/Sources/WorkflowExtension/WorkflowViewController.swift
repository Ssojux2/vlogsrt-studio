import AppKit
import FinalCutAgentCore
import UniformTypeIdentifiers

@MainActor
public final class WorkflowViewController: NSViewController {
  private let status = NSTextField(labelWithString: "프로젝트를 여기에 드래그하세요")

  public override func loadView() {
    let root = DropView(frame: NSRect(x: 0, y: 0, width: 420, height: 520))
    root.onDrop = { [weak self] data in self?.receive(data) ?? false }
    let choose = NSButton(title: "FCPXML 직접 선택", target: self, action: #selector(chooseFile))
    let stack = NSStackView(views: [status, choose])
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
      let data = try? Data(contentsOf: url)
    else { return }
    _ = receive(data)
  }

  private func receive(_ data: Data) -> Bool {
    do {
      let workspace = try AppGroupWorkspace.live()
      let job = try workspace.createJob(id: UUID().uuidString)
      try data.write(to: job.appendingPathComponent("input.fcpxml"), options: .atomic)
      status.stringValue = "프로젝트 받기 완료 · 분석 대기"
      return true
    } catch {
      status.stringValue = "수신 실패: \(error.localizedDescription)"
      return false
    }
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
