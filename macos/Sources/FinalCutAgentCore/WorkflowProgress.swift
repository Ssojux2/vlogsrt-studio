import CryptoKit
import Foundation

/// 파이썬 `AnalysisProgress` 메시지(S2A-3) — 시도 하나의 진행 경계 한 장.
///
/// 신원은 job·입력·추정·시도(·호출)다. 계수는 vision 단계에서만 실리며 단위 수가
/// 전체 백분율인 척하지 않는다. 이 메시지는 **관측이지 결과가 아니다** — 신뢰할
/// 수 있는 후보 내용은 언제나 기존 결과/시도/추정 검증(readAnalysisRecord)을
/// 지나야 한다. 부재·깨짐·무관 신원은 전부 '없음'으로 읽혀 보수적으로 흘러간다.
public struct AnalysisProgressMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let estimateRevision: String
  public let attemptID: String
  public let jobID: String?
  /// 잠금 소유자만 아는 호출 신원 — 중복(3) 호출은 자기 신원을 절대 실지 못한다.
  public let invocationID: String?
  /// `preparation` | `audio` | `vision` | `publication`
  public let stage: String
  /// `running` | `succeeded` | `failed` | `cancelled`
  public let status: String
  public let completed: Int?
  public let total: Int?
  public let recordedAt: String
  public let progressRevision: String

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case estimateRevision = "estimate_revision"
    case attemptID = "attempt_id"
    case jobID = "job_id"
    case invocationID = "invocation_id"
    case stage
    case status
    case completed
    case total
    case recordedAt = "recorded_at"
    case progressRevision = "progress_revision"
  }

  public var isTerminal: Bool { status == "succeeded" || status == "failed" || status == "cancelled" }

  /// 사용자에게 보이는 진행 문장 — 계수를 아는 창 단계에서만 숫자를 말한다.
  public var userFacingProgressText: String {
    let stageName: String
    switch stage {
    case "preparation": stageName = "준비 중"
    case "audio": stageName = "오디오 처리 중"
    case "vision": stageName = "영상 판정 중"
    case "publication": stageName = "결과 정리 중"
    default: stageName = "진행 중"
    }
    if stage == "vision", let completed, let total {
      return "AI 분석 \(stageName) · 창 \(completed)/\(total)"
    }
    return "AI 분석 \(stageName)"
  }
}

/// 파이썬 `AnalysisProgress` 검증의 Swift 짝(S2A-3) — 같은 행렬을 거절한다:
/// 알 수 없는 단계·상태, 깨진 신원, 음수·짝이 아닌 계수, 완료가 전체보다 큰
/// 계수, vision 밖의 계수, 발행 앞의 succeeded, 그리고 정준 이해가 어긋나는
/// 리비전. 진행은 신뢰 내용이 아니지만 깨진 스냅샷이 UI·릴레이를 오도하지
/// 않게 **읽는 자리에서** 걸러낸다.
public enum AnalysisProgressValidator {
  public static let stages = ["preparation", "audio", "vision", "publication"]
  public static let statuses = ["running", "succeeded", "failed", "cancelled"]

  public static func validate(_ message: AnalysisProgressMessage) -> Bool {
    isSHA256(message.inputRevision)
      && isSHA256(message.estimateRevision)
      && isSHA256(message.progressRevision)
      && !message.attemptID.isEmpty
      && !message.recordedAt.isEmpty
      && message.jobID != ""
      && message.invocationID != ""
      && stages.contains(message.stage)
      && statuses.contains(message.status)
      && countsAreCoherent(message)
      && !(message.status == "succeeded" && message.stage != "publication")
      && digestMatchesPayload(of: message)
  }

  /// 정준 이해의 지문 — 파이썬 `_canonical_json`(sort_keys·콤팩트·슬래시
  /// 무이스케이프·null 보존)과 같은 바이트에서 SHA-256을 낸다. 부호가 갈라지는
  /// 즉시 검증은 실패한다.
  public static func canonicalDigest(of message: AnalysisProgressMessage) -> String? {
    let payload: [String: Any] = [
      "attempt_id": message.attemptID,
      "completed": message.completed.map { $0 as Any } ?? NSNull(),
      "estimate_revision": message.estimateRevision,
      "input_revision": message.inputRevision,
      "invocation_id": message.invocationID.map { $0 as Any } ?? NSNull(),
      "job_id": message.jobID.map { $0 as Any } ?? NSNull(),
      "recorded_at": message.recordedAt,
      "stage": message.stage,
      "status": message.status,
      "total": message.total.map { $0 as Any } ?? NSNull(),
    ]
    guard
      JSONSerialization.isValidJSONObject(payload),
      let data = try? JSONSerialization.data(
        withJSONObject: payload, options: [.sortedKeys, .withoutEscapingSlashes])
    else { return nil }
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }

  public static func digestMatchesPayload(of message: AnalysisProgressMessage) -> Bool {
    guard let digest = canonicalDigest(of: message) else { return false }
    return digest == message.progressRevision
  }

  private static func countsAreCoherent(_ message: AnalysisProgressMessage) -> Bool {
    switch (message.completed, message.total) {
    case (nil, nil):
      return true
    case let (completed?, total?):
      return completed >= 0 && total >= 0 && completed <= total && message.stage == "vision"
    default:
      return false  // 짝이 아닌 계수 — 계수는 완료·전체가 함께 온다
    }
  }

  private static func isSHA256(_ value: String) -> Bool {
    value.count == 64 && value.allSatisfy { $0.isHexDigit && !$0.isUppercase }
  }
}

/// job 폴더의 진행 스냅샷 한 장을 상한 안에서 읽는 독자 — 없거나 깨졌거나
/// 과대면 nil이다. 낡은 작업공간(S2A-2 이전)에는 파일 자체가 없다.
public enum WorkflowProgressStore {
  public static let maximumByteCount = 16_384

  public static func read(
    jobURL: URL, fileManager: FileManager = .default
  ) -> AnalysisProgressMessage? {
    let url = jobURL.appendingPathComponent(RoundTripJob.progressFileName)
    guard let handle = FileHandle(forReadingAtPath: url.path) else { return nil }
    defer { try? handle.close() }
    // 상한보다 한 바이트 더 읽는다 — 그 한 바이트가 차면 과대 기록이다.
    guard
      let head = try? handle.read(upToCount: Self.maximumByteCount + 1),
      head.count <= Self.maximumByteCount,
      var line = String(data: head, encoding: .utf8),
      line.hasSuffix("\n")
    else { return nil }
    line.removeLast()
    guard
      let message = try? ProtocolCodec.decodeAnalysis(
        AnalysisProgressMessage.self, type: "AnalysisProgress", from: line)
    else { return nil }
    return AnalysisProgressValidator.validate(message) ? message : nil
  }
}

/// 한 틱에서 job 폴더가 말하는 것 전부 — 엔진 호출은 없고 내구 상태 읽기뿐이다.
/// `record`는 기존 묶음 검증(추정·시도·집계의 신원 일치)을 그대로 지나 오고,
/// `estimate`도 파일 존재가 아니라 **해석된 메시지**여야 승인 준비의 근거가 된다.
public struct WorkflowDurableObservation: Equatable, Sendable {
  public let progress: AnalysisProgressMessage?
  public let estimate: AnalysisEstimateMessage?
  public let authorizationPresent: Bool
  public let record: AnalysisRecordState
  public let roughcutPresent: Bool
  public let cancelled: Bool

  public init(
    progress: AnalysisProgressMessage?,
    estimate: AnalysisEstimateMessage?,
    authorizationPresent: Bool,
    record: AnalysisRecordState,
    roughcutPresent: Bool,
    cancelled: Bool
  ) {
    self.progress = progress
    self.estimate = estimate
    self.authorizationPresent = authorizationPresent
    self.record = record
    self.roughcutPresent = roughcutPresent
    self.cancelled = cancelled
  }

  /// App Group job 폴더에서 실제로 읽는 관측 — 새로고침이 쓰는 실물 경로다.
  public static func live(
    jobURL: URL, fileManager: FileManager = .default
  ) -> WorkflowDurableObservation {
    WorkflowDurableObservation(
      progress: WorkflowProgressStore.read(jobURL: jobURL, fileManager: fileManager),
      estimate: RoundTripJob.readAnalysisEstimateIfPresent(directoryURL: jobURL),
      authorizationPresent: fileManager.fileExists(
        atPath: jobURL.appendingPathComponent(RoundTripJob.analysisAuthorizationFileName).path),
      record: RoundTripJob.readAnalysisRecord(directoryURL: jobURL, fileManager: fileManager),
      roughcutPresent: fileManager.fileExists(
        atPath: jobURL.appendingPathComponent(RoundTripJob.roughcutFileName).path),
      cancelled: CancellationStore.isCancelled(jobURL: jobURL, fileManager: fileManager))
  }
}

/// 새로고침이 화면에 알리는 상태 — 종료 계열은 재실행을 유도하지 않는다.
public enum WorkflowRefreshState: Equatable, Sendable {
  case analyzing(AnalysisProgressMessage)
  /// 마지막으로 본 진행이 상한 넘게 그대로 있다 — 정직한 '마지막 신호' 문장.
  /// 관측은 계속된다: 변하지 않은 스냅샷은 죽음도 생존도 증명하지 못한다(S2A-3).
  case lastKnown(AnalysisProgressMessage)
  /// 종료 진행은 봤으나 결과 기록이 아직 정착하지 않았다 — 유한하게 기다리는 중.
  case finalizing(AnalysisProgressMessage)
  /// 비용 추정만 완료된 상태 — 승인 UI를 노출할 준비(자동 승인·유료 실행 없음).
  case estimateReady
  /// 검증된 결과 기록과 함께 종료 — 후보 다시 불러오기의 근거.
  case completed
  /// 실패·취소로 종료 — 요약만 보이고 재시도는 사용자의 다음 누름이다.
  case terminated(AnalysisProgressMessage)
  case roughcutCompleted
  case cancelledJob
  /// 유한 대기 상한 — 수동 회복 안내. 재실행은 하지 않는다.
  case manualRecovery(String)
}

/// 새로고침 판정의 순수 모델(S2A-3) — 관측 한 장을 상태 전이로 바꾼다.
///
/// 묶음 규칙: job 신원·입력 리비전이 어긋나는 스냅샷은 무관(없음 취급)이고,
/// 요청 세션(baselineAttemptID 있음)은 **요청 순간의 마지막 시도** 스냅샷을
/// 무시한다 — 명시적 재시도가 새 엔진 시작을 기다리는 사이 낡은 종료 상태를
/// 성공으로 소비하는 간극을 닫는다. 같은 이유로 묶음 검증을 통과한 낡은 결과
/// 기록도 재시도 대기 중에는 완료로 읽히지 않는다. appear/receive 갱신 세션은
/// baseline 없이 현재 상태를 그대로 보고한다.
public struct WorkflowRefreshModel: Sendable {
  public enum Flavor: Equatable, Sendable {
    case analysis
    case roughcut
  }

  public enum Transition: Equatable, Sendable {
    case none
    case report(WorkflowRefreshState)
    case reportAndStop(WorkflowRefreshState)
  }

  public static let absentProgressRecoveryText =
    "진행 상태를 시간 안에 받지 못했습니다 — AI 분석 요청을 다시 눌러 새로고침하세요"
    + " (자동 재실행은 하지 않습니다)"
  public static let finalizingRecoveryText =
    "완료 신호는 받았지만 결과 기록이 아직 정착하지 않았습니다 —"
    + " AI 분석 요청을 다시 눌러 새로고침하세요"
  /// 마지막으로 본 진행의 정직한 문장 뒤에 붙는 한정 — 같은 스냅샷이 오래
  /// 이어져도 그것이 죽음의 증거는 아니다(긴 오디오 단계의 정상 모양).
  public static let lastKnownSuffix =
    " · 마지막 신호 이후 갱신이 없습니다 — 긴 단계일 수 있고, 종료 증거가 없는 한"
    + " 실행 여부를 단정하지 않습니다"

  private let flavor: Flavor
  private let jobID: String?
  private let expectedInputRevision: String?
  private let baselineAttemptID: String?
  private let waitingTickLimit: Int
  private let finalizingTickLimit: Int
  private var waitingTicks = 0
  private var finalizingTicks = 0
  private var unchangedRunningTicks = 0
  private var lastRunningRevision: String?

  public init(
    flavor: Flavor,
    jobID: String? = nil,
    expectedInputRevision: String? = nil,
    baselineAttemptID: String? = nil,
    waitingTickLimit: Int,
    finalizingTickLimit: Int
  ) {
    self.flavor = flavor
    self.jobID = jobID
    self.expectedInputRevision = expectedInputRevision
    self.baselineAttemptID = baselineAttemptID
    self.waitingTickLimit = waitingTickLimit
    self.finalizingTickLimit = finalizingTickLimit
  }

  public mutating func observe(
    _ observation: WorkflowDurableObservation
  ) -> Transition {
    if observation.cancelled { return .reportAndStop(.cancelledJob) }
    if flavor == .roughcut { return observeRoughcut(observation) }
    return observeAnalysis(observation)
  }

  private mutating func observeRoughcut(
    _ observation: WorkflowDurableObservation
  ) -> Transition {
    if observation.roughcutPresent { return .reportAndStop(.roughcutCompleted) }
    return boundedWaiting()
  }

  private mutating func observeAnalysis(
    _ observation: WorkflowDurableObservation
  ) -> Transition {
    if let progress = observation.progress, isBound(progress, estimate: observation.estimate) {
      // 신원이 정확히 맞는 완성 기록은 무엇보다 우선한다 — 마지막 종료 발행이
      // 무해하게 실패해 스냅샷이 running에 멈춰 있어도 정직한 종료로 닫는다
      // (S2A-3). 무관한 낡은 기록은 이 문을 못 지난다.
      if coherentRecord(observation.record, for: progress) != nil {
        return .reportAndStop(.completed)
      }
      // 부재 대기 카운터는 진행이 보이는 순간 되돌린다 — '같은 running' 계수는
      // 별도의 자루(unchangedRunningTicks)에 쌓아 스위치 앞 초기화에 흔들리지
      // 않게 한다.
      waitingTicks = 0
      switch progress.status {
      case "running":
        finalizingTicks = 0
        // 같은 스냅샷이 오래 이어져도 멈추지 않는다 — 긴 오디오 단계의 정상
        // 모양이고, 변하지 않은 신호는 죽음도 생존도 증명하지 못한다(S2A-3).
        // 상한을 넘으면 '마지막 신호' 문장으로 정직하게 말하고 관측은 계속한다.
        if progress.progressRevision == lastRunningRevision {
          unchangedRunningTicks += 1
          if unchangedRunningTicks > waitingTickLimit {
            return .report(.lastKnown(progress))
          }
          return .report(.analyzing(progress))
        }
        lastRunningRevision = progress.progressRevision
        unchangedRunningTicks = 0
        return .report(.analyzing(progress))
      case "succeeded":
        // 종료 진행은 결과 내용이 아니다 — 정착을 기다리다 상한을 넘으면
        // 수동 회복 안내로 닫는다(엔진은 부르지 않는다).
        finalizingTicks += 1
        if finalizingTicks > finalizingTickLimit {
          return .reportAndStop(.manualRecovery(Self.finalizingRecoveryText))
        }
        return .report(.finalizing(progress))
      case "failed":
        // 정합 실패 기록을 유한하게 기다리다 상한 뒤에는 종료 상태를 있는 그대로
        // 보인다 — 낡은 결과를 실패로 둔갑시키지 않는다.
        finalizingTicks += 1
        if finalizingTicks > finalizingTickLimit {
          return .reportAndStop(.terminated(progress))
        }
        return .report(.finalizing(progress))
      case "cancelled":
        return .reportAndStop(.terminated(progress))
      default:
        return .none  // 알 수 없는 상태 열거 — 보수적으로 조용히 둔다
      }
    }
    if let estimate = observation.estimate, !observation.authorizationPresent,
      expectedOK(estimate.inputRevision)
    {
      return .reportAndStop(.estimateReady)
    }
    // 낡은(진행 파일 없는) 작업공간의 완성 기록 — 새로고침 세션이 그대로 보인다.
    // 요청 세션이면 baseline 시도의 기록이므로 완료로 소비하지 않고 기다린다.
    if isNewRecord(observation.record) { return .reportAndStop(.completed) }
    return boundedWaiting()
  }

  private func isBound(
    _ progress: AnalysisProgressMessage, estimate: AnalysisEstimateMessage?
  ) -> Bool {
    // job 신원을 아는 세션은 스냅샷의 주장(비어 있어도)이 같아야 한다.
    if let jobID, progress.jobID != jobID { return false }
    if let expectedInputRevision, progress.inputRevision != expectedInputRevision {
      return false
    }
    // 아는 추정과 어긋나는 진행은 이 job의 것이 아니다 — 종료 기록과 같은 잣대.
    // 추정을 모르는(낡은) 관측은 보수적으로 그냥 지나간다.
    if let estimate, progress.estimateRevision != estimate.estimateRevision { return false }
    if let baselineAttemptID, progress.attemptID == baselineAttemptID { return false }
    return true
  }

  /// 진행과 **신원이 정확히 같은** 묶음 검증 기록 — 시도·입력·추정 셋 다.
  private func coherentRecord(
    _ record: AnalysisRecordState, for progress: AnalysisProgressMessage
  ) -> AnalysisResultMessage? {
    if case .valid(let result) = record,
      result.attemptID == progress.attemptID,
      result.inputRevision == progress.inputRevision,
      result.estimateRevision == progress.estimateRevision,
      expectedOK(result.inputRevision)
    {
      return result
    }
    return nil
  }

  /// 묶음 검증을 통과한 **새** 기록인가 — 요청 세션이면 baseline 시도의 낡은
  /// 기록은, 기대 입력을 아는 세션이면 어긋난 기록은 완료로 읽히지 않는다.
  private func isNewRecord(_ record: AnalysisRecordState) -> Bool {
    if case .valid(let result) = record {
      guard expectedOK(result.inputRevision) else { return false }
      if let baselineAttemptID, result.attemptID == baselineAttemptID { return false }
      return true
    }
    return false
  }

  private func expectedOK(_ revision: String) -> Bool {
    expectedInputRevision.map { $0 == revision } ?? true
  }

  private mutating func boundedWaiting() -> Transition {
    waitingTicks += 1
    if waitingTicks > waitingTickLimit {
      return .reportAndStop(.manualRecovery(Self.absentProgressRecoveryText))
    }
    return .none
  }
}

/// 붙임 세션 — 유한 주기로 내구 상태만 읽어 모델의 판정을 알린다(S2A-3).
///
/// 세대로 묶는다: `start`를 다시 부르면 이전 과제는 취소되고, 폐기된 세대의
/// 관측은 알리지 못한다. 엔진을 부르지 않고, 승인하지 않고, 취소된 job은
/// 스스로 멈춘다. 틱은 주입한다 — 시험은 시간이 아니라 사건으로 진행한다.
public final class WorkflowRefreshSession: @unchecked Sendable {
  public struct Configuration: Sendable {
    public var intervalNanoseconds: UInt64 = 500_000_000
    /// 진행 부재의 상한(틱 수) — 무한 대기 없이 수동 회복 안내로 닫는다.
    public var waitingTickLimit: Int = 240
    /// 종료 진행 → 결과 기록 정착의 상한(틱 수) — 마지막 발행 간극이다.
    public var finalizingTickLimit: Int = 20

    public init(
      intervalNanoseconds: UInt64 = 500_000_000,
      waitingTickLimit: Int = 240,
      finalizingTickLimit: Int = 20
    ) {
      self.intervalNanoseconds = intervalNanoseconds
      self.waitingTickLimit = waitingTickLimit
      self.finalizingTickLimit = finalizingTickLimit
    }
  }

  public typealias Tick = @Sendable (UInt64) async throws -> Void
  public typealias Observe = @Sendable (URL) -> WorkflowDurableObservation
  public typealias Notify = @Sendable (WorkflowRefreshState) -> Void

  private let configuration: Configuration
  private let tick: Tick
  private let observe: Observe
  private let notify: Notify
  private let lock = NSLock()
  private var generation = 0
  private var task: Task<Void, Never>?

  public init(
    configuration: Configuration = Configuration(),
    tick: @escaping Tick = { try await Task.sleep(nanoseconds: $0) },
    observe: @escaping Observe,
    notify: @escaping Notify
  ) {
    self.configuration = configuration
    self.tick = tick
    self.observe = observe
    self.notify = notify
  }

  /// 관측을 시작(또는 교체)한다 — 시작·교체는 이전 과제를 끊는다.
  public func start(
    jobURL: URL,
    jobID: String?,
    flavor: WorkflowRefreshModel.Flavor,
    expectedInputRevision: String? = nil,
    baselineAttemptID: String? = nil
  ) {
    let previous = lock.withLock { () -> Task<Void, Never>? in
      generation += 1
      let old = task
      task = nil
      return old
    }
    previous?.cancel()
    let current = lock.withLock { generation }
    let interval = configuration.intervalNanoseconds
    let limits = (configuration.waitingTickLimit, configuration.finalizingTickLimit)
    let ticker = tick
    let observer = observe
    let notifier = notify
    let prepared = Task.detached { [weak self] in
      var model = WorkflowRefreshModel(
        flavor: flavor,
        jobID: jobID,
        expectedInputRevision: expectedInputRevision,
        baselineAttemptID: baselineAttemptID,
        waitingTickLimit: limits.0,
        finalizingTickLimit: limits.1)
      while true {
        let observation = observer(jobURL)
        guard let self else { return }  // 세션이 사라졌다 — 더 알리지 않는다
        guard
          let transition = self.advance(
            &model, generation: current, observation: observation)
        else { return }  // 폐기된 세대 — 더 알리지 않는다
        switch transition {
        case .none:
          break
        case .report(let state):
          notifier(state)
        case .reportAndStop(let state):
          notifier(state)
          self.retire(current)
          return
        }
        do { try await ticker(interval) } catch { return }  // 취소 — 조용한 설계 종료
      }
    }
    lock.withLock {
      if generation == current {
        task = prepared
      }
    }
  }

  /// 관측을 멈춘다 — 뷰가 사라지거나 job이 바뀔 때. 과제는 곧 끝난다.
  public func stop() {
    let current = lock.withLock { () -> Task<Void, Never>? in
      generation += 1
      let old = task
      task = nil
      return old
    }
    current?.cancel()
  }

  /// 세대가 살아 있을 때만 모델을 앞으로 움직인다 — 폐기 세대의 알림 길이 없다.
  private func advance(
    _ model: inout WorkflowRefreshModel, generation expected: Int,
    observation: WorkflowDurableObservation
  ) -> WorkflowRefreshModel.Transition? {
    lock.withLock { () -> WorkflowRefreshModel.Transition? in
      guard generation == expected else { return nil }
      return model.observe(observation)
    }
  }

  private func retire(_ expected: Int) {
    lock.withLock {
      if generation == expected {
        task = nil
      }
    }
  }
}

/// 늦은 배달의 문지기(S2A-3) — job 신원과 요청 세대가 둘 다 살아 있을 때만
/// 문을 연다. URL 열기 완료·승인 시트 응답 같은 사용자 속도의 뒤늦은 배달이
/// 새 job이나 같은 job의 새 요청 상태를 덮는 길을 닫는다. 확장이 실제로 쓰는
/// 재사용 문이라 시험도 이 문으로 결정적으로 덮는다.
public struct WorkflowDeliveryGate: Equatable, Sendable {
  public let jobID: String
  public let generation: Int

  public init(jobID: String, generation: Int) {
    self.jobID = jobID
    self.generation = generation
  }

  /// 지금 상태가 배달을 기다리던 그 요청인가.
  public func accepts(currentJobID: String?, currentGeneration: Int) -> Bool {
    currentJobID == jobID && currentGeneration == generation
  }
}

/// 후보 선택의 소유자(S2A-3) — 같은 결과 리비전에서만 사용자 선택이 산다.
///
/// 새 리비전은 같은 후보 ID라도 기본 선택으로 되돌아간다(재분석은 새 결과다).
/// 다시 불러오기 도중 후보 파일을 잠시 못 읽으면 호출 자체가 실패로 올라가
/// 현재 줄과 선택은 그대로 보존된다.
public struct CandidateSelectionModel: Equatable, Sendable {
  private(set) var boundResultRevision: String?
  private var approvedIDs: Set<String> = []

  public init() {}

  /// live 줄에서 사용자가 고른 후보를 기록한다 — 같은 리비전 재독기의 재료.
  public mutating func recordUserChoices(_ identifiers: Set<String>) {
    approvedIDs = identifiers
  }

  /// 이번에 적용할 선택 — 같은 리비전이면 사용자 선택을 유지하고(사라진 후보는
  /// 버린다), 새 리비전이면 기본 선택으로 되돌아간다.
  public mutating func effectiveSelection(
    resultRevision: String?, candidates: [CutCandidateMessage]
  ) -> Set<String> {
    let present = Set(candidates.map(\.candidateID))
    guard let resultRevision, !resultRevision.isEmpty,
      resultRevision == boundResultRevision
    else {
      boundResultRevision = resultRevision
      approvedIDs = Set(candidates.filter(\.selectedByDefault).map(\.candidateID))
      return approvedIDs
    }
    approvedIDs.formIntersection(present)
    return approvedIDs
  }
}
