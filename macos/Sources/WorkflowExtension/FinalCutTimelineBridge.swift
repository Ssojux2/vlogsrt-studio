import Foundation
import FinalCutAgentCore

#if canImport(ProExtensionHost)
  import CoreMedia
  @preconcurrency import ProExtensionHost

  @MainActor
  final class FinalCutTimelineBridge: NSObject, FCPXTimelineObserver {
    private let onSelection: @MainActor (ActiveProjectSelection?) -> Void
    private var timeline: FCPXTimeline?

    init(onSelection: @escaping @MainActor (ActiveProjectSelection?) -> Void) {
      self.onSelection = onSelection
      super.init()
    }

    func start() {
      guard let host = ProExtensionHostSingleton() as? FCPXHost else {
        onSelection(nil)
        return
      }
      timeline = host.timeline
      timeline?.add(self)
      refresh()
    }

    func stop() {
      timeline?.remove(self)
      timeline = nil
    }

    func movePlayhead(to time: RationalTime) {
      guard time.timescale > 0 else { return }
      _ = timeline?.movePlayhead(
        to: CMTime(value: time.value, timescale: time.timescale)
      )
    }

    nonisolated func activeSequenceChanged() {
      Task { @MainActor [weak self] in self?.refresh() }
    }

    nonisolated func sequenceTimeRangeChanged() {
      Task { @MainActor [weak self] in self?.refresh() }
    }

    private func refresh() {
      guard
        let sequence = timeline?.activeSequence,
        let project = sequence.container as? FCPXProject,
        let selection = try? ActiveProjectSelection.validated(
          projectUID: project.uid,
          projectName: project.name,
          timelineStart: Self.rational(sequence.startTime),
          duration: Self.rational(sequence.duration),
          frameDuration: Self.rational(sequence.frameDuration)
        )
      else {
        onSelection(nil)
        return
      }
      onSelection(selection)
    }

    private static func rational(_ time: CMTime) -> RationalTime {
      RationalTime(value: time.value, timescale: time.timescale)
    }
  }
#else
  /// SwiftPM 검증에는 별도 Workflow Extension SDK가 연결되지 않는다.
  @MainActor
  final class FinalCutTimelineBridge {
    private let onSelection: @MainActor (ActiveProjectSelection?) -> Void

    init(onSelection: @escaping @MainActor (ActiveProjectSelection?) -> Void) {
      self.onSelection = onSelection
    }

    func start() { onSelection(nil) }
    func stop() {}
    func movePlayhead(to time: RationalTime) { _ = time }
  }
#endif
