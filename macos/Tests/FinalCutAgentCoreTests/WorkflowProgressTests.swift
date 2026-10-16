import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-3 — 진행 계약의 읽기·새로고침 모델·선택 보존. 확장이 실제로
//  쓰는 모델(WorkflowRefreshModel·WorkflowRefreshSession·
//  CandidateSelectionModel)을 그대로 시험한다. 시간에만 의존하지 않게 틱은
//  사건으로 주입한다.
// ─────────────────────────────────────────────────────────────

/// 진행 메시지 조립 — 리비전 자리만 시험이 정한다(서명은 `signedProgressLine`).
func progressMessage(
  attempt: String = "attempt-1",
  job: String? = "job-42",
  invocation: String? = "inv-1",
  stage: String = "vision",
  status: String = "running",
  completed: Int? = 1,
  total: Int? = 3,
  input: String = String(repeating: "f", count: 64),
  estimate: String = String(repeating: "e", count: 64),
  progressRevision: String = String(repeating: "0", count: 64)
) -> AnalysisProgressMessage {
  AnalysisProgressMessage(
    inputRevision: input,
    estimateRevision: estimate,
    attemptID: attempt,
    jobID: job,
    invocationID: invocation,
    stage: stage,
    status: status,
    completed: completed,
    total: total,
    recordedAt: "2026-09-28T00:00:00Z",
    progressRevision: progressRevision)
}

/// 정준 이해로 서명한 진행 한 줄 — 파이썬 봉투 모양 그대로.
func signedProgressLine(
  attempt: String = "attempt-1",
  job: String? = "job-42",
  invocation: String? = "inv-1",
  stage: String = "vision",
  status: String = "running",
  completed: Int? = 1,
  total: Int? = 3,
  input: String = String(repeating: "f", count: 64),
  estimate: String = String(repeating: "e", count: 64)
) -> String {
  let unsigned = progressMessage(
    attempt: attempt, job: job, invocation: invocation, stage: stage, status: status,
    completed: completed, total: total, input: input, estimate: estimate)
  guard let digest = AnalysisProgressValidator.canonicalDigest(of: unsigned) else {
    return ""
  }
  let signed = progressMessage(
    attempt: attempt, job: job, invocation: invocation, stage: stage, status: status,
    completed: completed, total: total, input: input, estimate: estimate,
    progressRevision: digest)
  return (try? ProtocolCodec.encode(signed, type: "AnalysisProgress", version: "1.3")) ?? ""
}

func validResult(
  attempt: String = "attempt-1", input: String = String(repeating: "f", count: 64),
  status: String = "success"
) -> AnalysisResultMessage {
  AnalysisResultMessage(
    inputRevision: input,
    attemptID: attempt,
    estimateRevision: String(repeating: "e", count: 64),
    status: status,
    windows: .init(total: 3, successful: 3, failed: 0, skipped: 0),
    candidateCount: 2,
    upstream: .init(
      audio: .init(total: 1, failed: 0, skipped: 0),
      transcription: .init(total: 1, failed: 0, skipped: 0),
      vad: .init(total: 1, failed: 0, skipped: 0),
      sources: .init(total: 1, usable: 1)),
    reasonCodes: [],
    resultRevision: String(repeating: "r", count: 64))
}

func estimateMessage(
  input: String = String(repeating: "f", count: 64)
) -> AnalysisEstimateMessage {
  AnalysisEstimateMessage(
    inputRevision: input,
    model: "luna",
    uniqueSourceAudioSeconds: "0/1",
    transcriptionSeconds: "0/1",
    maxWindows: 3,
    maxImages: 6,
    maxImageTokens: 100,
    costUpperBound: "1",
    settingsSHA256: String(repeating: "s", count: 64),
    pricingSnapshotRevision: String(repeating: "p", count: 64),
    mediaGrantRevision: String(repeating: "g", count: 64),
    estimateRevision: String(repeating: "r", count: 64))
}

func observation(
  progress: AnalysisProgressMessage? = nil,
  estimate: AnalysisEstimateMessage? = nil,
  authorization: Bool = false,
  record: AnalysisRecordState = .fresh,
  roughcut: Bool = false,
  cancelled: Bool = false
) -> WorkflowDurableObservation {
  WorkflowDurableObservation(
    progress: progress, estimate: estimate, authorizationPresent: authorization,
    record: record, roughcutPresent: roughcut, cancelled: cancelled)
}

private func decoding(_ line: String) -> AnalysisProgressMessage? {
  var trimmed = line
  if trimmed.hasSuffix("\n") { trimmed.removeLast() }
  return try? ProtocolCodec.decodeAnalysis(
    AnalysisProgressMessage.self, type: "AnalysisProgress", from: trimmed)
}

// ─────────────────────────────────────────────────────────────
//  읽기 — 상한·깨짐·부재의 보수적 처리
// ─────────────────────────────────────────────────────────────

@Test func storeDecodesPythonShapedSnapshotsAndStaysQuietOnDamage() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let url = job.appendingPathComponent(RoundTripJob.progressFileName)

  // 부재 — 낡은 작업공간의 정상 모습.
  #expect(WorkflowProgressStore.read(jobURL: job) == nil)

  // 온전한 스냅샷.
  try signedProgressLine().data(using: .utf8)!.write(to: url)
  let decoded = try #require(WorkflowProgressStore.read(jobURL: job))
  #expect(decoded.attemptID == "attempt-1")
  #expect(decoded.invocationID == "inv-1")
  #expect(decoded.completed == 1 && decoded.total == 3)
  #expect(decoded.userFacingProgressText.contains("1/3"))
  #expect(decoded.isTerminal == false)

  // 깨진 JSON·과대 기록 — 전부 nil(없음)로 읽힌다.
  try Data("not json\n".utf8).write(to: url)
  #expect(WorkflowProgressStore.read(jobURL: job) == nil)
  try Data((signedProgressLine() + signedProgressLine(attempt: "attempt-2")).utf8)
      .write(to: url)
  #expect(WorkflowProgressStore.read(jobURL: job) == nil)
  try Data((String(repeating: "x", count: WorkflowProgressStore.maximumByteCount + 1) + "\n").utf8)
    .write(to: url)
  #expect(WorkflowProgressStore.read(jobURL: job) == nil)
  try FileManager.default.removeItem(at: url)
}

// ─────────────────────────────────────────────────────────────
//  모델 — 상태 전이
// ─────────────────────────────────────────────────────────────

func model(
  baseline: String? = nil,
  expected: String? = nil,
  waiting: Int = 3,
  finalizing: Int = 2
) -> WorkflowRefreshModel {
  WorkflowRefreshModel(
    flavor: .analysis, jobID: "job-42", expectedInputRevision: expected,
    baselineAttemptID: baseline, waitingTickLimit: waiting, finalizingTickLimit: finalizing)
}

@Test func runningProgressReportsAnalyzingAndTerminalWaitsForRecord() {
  var refresh = model()
  var transition = refresh.observe(observation())
  #expect(transition == .none)  // 아무 것도 없다 — 조용히 기다린다

  let running = decoding(signedProgressLine())!
  transition = refresh.observe(observation(progress: running))
  #expect(transition == .report(.analyzing(running)))

  // 종료 진행은 왔다 — 기록이 아직 정착하지 않았다(마지막 발행 간극).
  let done = decoding(signedProgressLine(stage: "publication", status: "succeeded", completed: nil, total: nil))!
  transition = refresh.observe(observation(progress: done))
  #expect(transition == .report(.finalizing(done)))
  transition = refresh.observe(observation(progress: done))
  #expect(transition == .report(.finalizing(done)))
  // 상한을 넘으면 수동 회복 안내로 닫는다 — 엔진은 부르지 않는다.
  transition = refresh.observe(observation(progress: done))
  if case .reportAndStop(.manualRecovery(let text)) = transition {
    #expect(text == WorkflowRefreshModel.finalizingRecoveryText)
  } else {
    Issue.record("정착 상한 뒤에는 수동 회복이어야 한다: \(transition)")
  }
}

@Test func terminalProgressCompletesOnlyWithAnIdentityMatchingRecord() {
  var refresh = model()
  let done = decoding(signedProgressLine(stage: "publication", status: "succeeded", completed: nil, total: nil))!
  #expect(
    refresh.observe(observation(progress: done, record: .valid(validResult())))
      == .reportAndStop(.completed))

  // 신원이 하나라도 어긋난 기록은 완료의 근거가 아니다 — 유한 정착으로 간다.
  var mismatched = model()
  #expect(
    mismatched.observe(observation(progress: done, record: .valid(validResult(attempt: "attempt-other"))))
      == .report(.finalizing(done)))
  var drifted = model(expected: String(repeating: "f", count: 64))
  #expect(
    drifted.observe(
      observation(progress: done, record: .valid(validResult(input: String(repeating: "9", count: 64)))))
      == .report(.finalizing(done)))

  // 실패 종료도 정합한 기록을 만나야 요약 경로로 닫는다 — 그 전엔 유한 정착,
  // 상한 뒤에는 종료 상태를 있는 그대로 보인다(낡은 결과 둔갑 금지).
  var failing = model(finalizing: 1)
  let failed = decoding(signedProgressLine(status: "failed", completed: 1, total: 3))!
  #expect(
    failing.observe(observation(progress: failed)) == .report(.finalizing(failed)))
  #expect(
    failing.observe(observation(progress: failed)) == .reportAndStop(.terminated(failed)))
  var coherent = model()
  #expect(
    coherent.observe(observation(progress: failed, record: .valid(validResult())))
      == .reportAndStop(.completed))

  let cancelled = decoding(signedProgressLine(status: "cancelled"))!
  #expect(
    refresh.observe(observation(progress: cancelled, cancelled: true))
      == .reportAndStop(.cancelledJob))
}

@Test func estimateOnlyCompletionIsCostApprovalReady() {
  var refresh = model()
  #expect(
    refresh.observe(observation(estimate: estimateMessage(), authorization: false))
      == .reportAndStop(.estimateReady))
  // 해석된 추정이어야 근거다 — 파일만 있고 메시지가 없으면 승인 준비가 아니다.
  #expect(
    refresh.observe(observation(authorization: false)) == .none)
  // 기대 입력을 아는 세션은 어긋난 추정을 승인 준비로 읽지 않는다.
  var bound = model(expected: String(repeating: "9", count: 64))
  #expect(
    bound.observe(observation(estimate: estimateMessage(), authorization: false)) == .none)
  // 승인이 이미 있으면 승인 준비가 아니다 — 유료 실행 대기다.
  var waiting = model(waiting: 1)
  #expect(
    waiting.observe(observation(estimate: estimateMessage(), authorization: true)) == .none)
  #expect(
    waiting.observe(observation(estimate: estimateMessage(), authorization: true))
      == .reportAndStop(.manualRecovery(WorkflowRefreshModel.absentProgressRecoveryText)))
}

@Test func staleIdentityIsIgnoredConservatively() {
  // 다른 job·job 미표기·다른 입력 리비전의 스냅샷은 없음 취급 — 잘못된 완료를
  // 만들지 않는다(기대 job을 아는 세션은 빈 주장도 묶음 실패다).
  var foreign = model()
  let stranger = decoding(signedProgressLine(job: "job-other"))!
  #expect(foreign.observe(observation(progress: stranger)) == .none)
  let unclaimed = decoding(signedProgressLine(job: nil))!
  #expect(foreign.observe(observation(progress: unclaimed)) == .none)
  var drifted = model(expected: String(repeating: "9", count: 64))
  let oldInput = decoding(signedProgressLine(input: String(repeating: "9", count: 64)))!
  // 기대 입력과 같은 진행은 따라간다 — 단 어긋난 입력의 기록은 완료가 아니다.
  #expect(
    drifted.observe(observation(progress: oldInput, record: .valid(validResult())))
      == .report(.analyzing(oldInput)))
  var bound = model(expected: String(repeating: "f", count: 64))
  let current = decoding(signedProgressLine())!
  #expect(bound.observe(observation(progress: current)) == .report(.analyzing(current)))
}

@Test func explicitRetryDoesNotConsumeThePriorAttemptTerminalSnapshot() {
  // 요청 세션 — baseline은 요청 순간의 마지막 시도다. 새 엔진이 시작을 알리기
  // 전에 낡은 종료 스냅샷·낡은 기록이 완료로 소비되지 않는다(S2A-3 간극).
  var retry = model(baseline: "attempt-old")
  let stale = decoding(signedProgressLine(attempt: "attempt-old", stage: "publication", status: "succeeded", completed: nil, total: nil))!
  #expect(
    retry.observe(
      observation(progress: stale, record: .valid(validResult(attempt: "attempt-old"))))
      == .none)
  // 진행 파일이 아예 없는 낡은 job — baseline 시도의 완성 기록도 소비하지 않는다.
  #expect(
    retry.observe(observation(record: .valid(validResult(attempt: "attempt-old")))) == .none)
  // 새 시도가 나타나면 그것을 따라간다.
  let fresh = decoding(signedProgressLine(attempt: "attempt-new"))!
  #expect(retry.observe(observation(progress: fresh)) == .report(.analyzing(fresh)))
  let freshDone = decoding(signedProgressLine(attempt: "attempt-new", stage: "publication", status: "succeeded", completed: nil, total: nil))!
  #expect(
    retry.observe(
      observation(progress: freshDone, record: .valid(validResult(attempt: "attempt-new"))))
      == .reportAndStop(.completed))
}

@Test func roughcutFlavorWaitsForTheArtifact() {
  var roughcut = WorkflowRefreshModel(
    flavor: .roughcut, jobID: "job-42", waitingTickLimit: 2, finalizingTickLimit: 2)
  #expect(roughcut.observe(observation()) == .none)
  #expect(
    roughcut.observe(observation(roughcut: true)) == .reportAndStop(.roughcutCompleted))
  var bounded = WorkflowRefreshModel(
    flavor: .roughcut, jobID: "job-42", waitingTickLimit: 1, finalizingTickLimit: 1)
  #expect(bounded.observe(observation()) == .none)
  #expect(
    bounded.observe(observation())
      == .reportAndStop(.manualRecovery(WorkflowRefreshModel.absentProgressRecoveryText)))
}

// ─────────────────────────────────────────────────────────────
//  선택 보존 — 같은 리비전 유지·새 리비전 초기화
// ─────────────────────────────────────────────────────────────

private func candidate(_ id: String, selectedByDefault: Bool) -> CutCandidateMessage {
  CutCandidateMessage(
    candidateID: id,
    inputRevision: String(repeating: "f", count: 64),
    clipInstanceID: "clip-1",
    sourceRange: .init(start: "0/1", end: "1/1"),
    timelineRange: .init(start: "0/1", end: "1/1"),
    reason: "silence",
    evidence: [],
    confidence: "0.95",
    windowID: "w1",
    decisionRevision: String(repeating: "d", count: 64),
    selectedByDefault: selectedByDefault)
}

@Test func sameRevisionReloadKeepsUserChoicesAndNewRevisionResets() {
  var selection = CandidateSelectionModel()
  let first = [candidate(String(repeating: "a", count: 64), selectedByDefault: true),
               candidate(String(repeating: "b", count: 64), selectedByDefault: false)]
  var applied = selection.effectiveSelection(resultRevision: "rev-1", candidates: first)
  #expect(applied == Set([first[0].candidateID]))  // 기본 선택

  // 사용자가 기본 선택을 끄고 두 번째를 켰다 — 같은 리비전 재독기는 그대로 둔다.
  selection.recordUserChoices([first[1].candidateID])
  applied = selection.effectiveSelection(resultRevision: "rev-1", candidates: first)
  #expect(applied == Set([first[1].candidateID]))

  // 반복 새로고침에서도 선택은 흔들리지 않는다.
  selection.recordUserChoices([first[1].candidateID])
  applied = selection.effectiveSelection(resultRevision: "rev-1", candidates: first)
  #expect(applied == Set([first[1].candidateID]))

  // 새 리비전 — 같은 후보 ID라도 기본 선택으로 되돌아간다.
  applied = selection.effectiveSelection(resultRevision: "rev-2", candidates: first)
  #expect(applied == Set([first[0].candidateID]))

  // 사라진 후보는 버린다 — 같은 리비전에서도 남은 것만 유지된다.
  selection.recordUserChoices(Set(first.map(\.candidateID)))
  applied = selection.effectiveSelection(resultRevision: "rev-2", candidates: [first[0]])
  #expect(applied == Set([first[0].candidateID]))
}

// ─────────────────────────────────────────────────────────────
//  세션 — 세대 묶음·제어된 틱
// ─────────────────────────────────────────────────────────────

/// 틱을 사건으로 바꾸는 문 — 시험이 한 번 열 때마다 관측이 한 번 진행된다.
final class TickGate: @unchecked Sendable {
  private let lock = NSLock()
  private var continuations: [CheckedContinuation<Void, Never>] = []
  private var opened = 0

  var openings: Int { lock.withLock { opened } }

  func wait() async {
    await withCheckedContinuation { (pending: CheckedContinuation<Void, Never>) in
      lock.withLock { continuations.append(pending) }
    }
  }

  func open() {
    let pending = lock.withLock { () -> [CheckedContinuation<Void, Never>] in
      opened += 1
      defer { continuations.removeAll() }
      return continuations
    }
    pending.forEach { $0.resume() }
  }
}

/// 기록 상자 — 알림을 담아 시험이 읽는다.
final class StateBox: @unchecked Sendable {
  private let lock = NSLock()
  private var rows: [WorkflowRefreshState] = []
  func append(_ state: WorkflowRefreshState) { lock.withLock { rows.append(state) } }
  var states: [WorkflowRefreshState] { lock.withLock { rows } }
}

@Test func sessionFollowsFileUpdatesWithControlledTicksAndStopsAtTerminal() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let progressURL = job.appendingPathComponent(RoundTripJob.progressFileName)

  let gate = TickGate()
  let states = StateBox()
  let session = WorkflowRefreshSession(
    configuration: .init(intervalNanoseconds: 0),
    tick: { _ in await gate.wait() },
    observe: { url in WorkflowDurableObservation.live(jobURL: url) },
    notify: { states.append($0) })
  session.start(jobURL: job, jobID: "job-42", flavor: .analysis)

  // 실행이 막혀 있는 사이 파일이 바뀐다 — 세션은 틱마다 그것을 본다.
  try Data(signedProgressLine(completed: 1).utf8).write(to: progressURL)
  gate.open()
  try await waitBriefly { !states.states.isEmpty }
  #expect(states.states.count == 1)
  guard case .analyzing(let first) = states.states[0] else {
    Issue.record("첫 알림은 analyzing이어야 한다: \(states.states)")
    return
  }
  #expect(first.completed == 1)

  try Data(
    signedProgressLine(stage: "publication", status: "succeeded", completed: nil, total: nil).utf8
  ).write(to: progressURL)
  gate.open()
  try await waitBriefly { states.states.count > 1 }
  // 기록이 아직 없다 — 종료 진행은 finalizing으로 기다린다(과잉 완료 금지).
  if states.states.count > 1 {
    #expect(
      states.states[1].isFinalizingLike,
      "기록 없는 종료는 finalizing이어야 한다: \(states.states[1])")
  }

  session.stop()
}

@Test func replacingTheSessionCancelsTheOldTaskAndLateStaleCallbacksDrop() async throws {
  let gate = TickGate()
  let states = StateBox()
  let armed = ArmedObservationBox(
    armedObservation: observation(record: .valid(validResult())))
  let session = WorkflowRefreshSession(
    configuration: .init(intervalNanoseconds: 0),
    tick: { _ in await gate.wait() },
    observe: { _ in armed.next() },
    notify: { states.append($0) })

  // 첫 관측 과제 — 아직 무장 전이라 조용히 기다린다(틱에서 막힌다).
  session.start(jobURL: URL(fileURLWithPath: "/tmp/old-job"), jobID: "job-42", flavor: .analysis)
  try await Task.sleep(nanoseconds: 50_000_000)

  // 교체 — 시작이 이전 과제를 끊는다. 두 과제 모두 같은 문에서 깨어나도
  // 폐기 세대는 세대 검사에서 알림 없이 끝난다.
  session.start(jobURL: URL(fileURLWithPath: "/tmp/new-job"), jobID: "job-43", flavor: .analysis)
  armed.arm()
  gate.open()
  try await waitBriefly { !states.states.isEmpty }
  try await Task.sleep(nanoseconds: 100_000_000)
  #expect(states.states.count == 1, "폐기된 세대의 알림이 새어 나왔다: \(states.states)")
  if case .completed? = states.states.first {
    #expect(Bool(true))
  } else {
    Issue.record("살아 있는 세대의 알림이 완료여야 한다: \(states.states)")
  }
  session.stop()
}

/// 무장 전에는 빈 관측, 무장 뒤에는 지정한 관측을 내주는 상자.
final class ArmedObservationBox: @unchecked Sendable {
  private let lock = NSLock()
  private var armed = false
  private let armedObservation: WorkflowDurableObservation

  init(armedObservation: WorkflowDurableObservation) {
    self.armedObservation = armedObservation
  }

  func arm() { lock.withLock { armed = true } }

  func next() -> WorkflowDurableObservation {
    lock.withLock { armed ? armedObservation : unarmedObservation }
  }

  private var unarmedObservation: WorkflowDurableObservation {
    WorkflowDurableObservation(
      progress: nil, estimate: nil, authorizationPresent: false,
      record: .fresh, roughcutPresent: false, cancelled: false)
  }
}

private extension WorkflowRefreshState {
  var isFinalizingLike: Bool {
    if case .finalizing = self { return true }
    return false
  }
}

// ─────────────────────────────────────────────────────────────
//  웜 검증 — 파이썬 정준 바이트와 같은 지문, 같은 거절 행렬
// ─────────────────────────────────────────────────────────────

/// 파이썬이 실제로 내는 봉투 한 줄(정준 json.dumps·sort_keys·콤팩트로 서명) —
/// 부호 계약의 교차 증거다. Swift 정준 이해가 같은 지문을 내지 못하면 이 상수와
/// 어긋난다.
private let pythonProducedRunningLine: String = {
  let estimate = String(repeating: "e", count: 64)
  let input = String(repeating: "f", count: 64)
  return "{\"payload\":{\"attempt_id\":\"attempt-1\",\"completed\":1,"
    + "\"estimate_revision\":\"\(estimate)\","
    + "\"input_revision\":\"\(input)\","
    + "\"invocation_id\":\"inv-1\",\"job_id\":\"job-42\","
    + "\"progress_revision\":\"d2df6c6120b117d0aa29fe06930ecba5eddafb45b0c4f37a36d77ba2006cb5f7\","
    + "\"recorded_at\":\"2026-09-28T00:00:00Z\",\"stage\":\"vision\","
    + "\"status\":\"running\",\"total\":3},"
    + "\"protocol_version\":\"1.3\",\"type\":\"AnalysisProgress\"}\n"
}()

@Test func swiftCanonicalizationReproducesThePythonDigest() throws {
  let decoded = try #require(decoding(pythonProducedRunningLine))
  #expect(AnalysisProgressValidator.validate(decoded))
  #expect(
    AnalysisProgressValidator.canonicalDigest(of: decoded)
      == "d2df6c6120b117d0aa29fe06930ecba5eddafb45b0c4f37a36d77ba2006cb5f7")
  // 같은 재료로 시험이 지은 줄도 같은 지문을 낸다.
  let rebuilt = decoding(signedProgressLine())
  #expect(rebuilt == decoded)
}

/// 같은 내용을 바로 다시 서명한 메시지 — 거절 이유가 리비전 불일치가 아니라
/// 의미 검증 그 자체여야 하는 부정 시험의 재료.
func freshlySigned(_ message: AnalysisProgressMessage) -> AnalysisProgressMessage {
  progressMessage(
    attempt: message.attemptID, job: message.jobID, invocation: message.invocationID,
    stage: message.stage, status: message.status, completed: message.completed,
    total: message.total, input: message.inputRevision,
    progressRevision: AnalysisProgressValidator.canonicalDigest(of: message) ?? "")
}

@Test func validatorRejectsTheSameMatrixAsPython() {
  // 알 수 없는 단계·상태 — 서명은 유효하므로 거절 이유는 열거 어휘다.
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(stage: "transcription"))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(status: "paused"))))
  // 깨진 신원 — 짧은 해시, 빈 시도·job·호출.
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(input: "short"))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(attempt: ""))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(job: ""))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(invocation: ""))))
  // 계수 — 음수, 완료가 전체보다 큼, 짝이 아님, vision 밖의 계수.
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(completed: -1))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(completed: 3, total: 2))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(completed: 1, total: nil))))
  #expect(!AnalysisProgressValidator.validate(freshlySigned(progressMessage(completed: nil, total: 3))))
  #expect(
    !AnalysisProgressValidator.validate(
      freshlySigned(progressMessage(stage: "audio", completed: 0, total: 1))))
  // 불가능한 종료 조합 — 발행 앞의 succeeded.
  #expect(
    !AnalysisProgressValidator.validate(
      freshlySigned(progressMessage(stage: "vision", status: "succeeded"))))
  // 내용을 바꾸고 옛 리비전을 그대로 두면(변조) 어긋난다.
  #expect(
    !AnalysisProgressValidator.validate(
      progressMessage(completed: 2, progressRevision: "d2df6c6120b117d0aa29fe06930ecba5eddafb45b0c4f37a36d77ba2006cb5f7")))
}

@Test func storeRejectsNonIntegerCountersOnTheWire() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let url = job.appendingPathComponent(RoundTripJob.progressFileName)
  let base = signedProgressLine()
  // bool·float 계수 — Int로 디코드 자체가 실패해야 한다(파이썬 _optional_integer와
  // 같은 문이다). 정렬된 정준 바이트라 completed는 쉼표를, total은 닫는 괄호를 뒤에 둔다.
  for mutated in [
    base.replacingOccurrences(of: "\"completed\":1,", with: "\"completed\":true,"),
    base.replacingOccurrences(of: "\"completed\":1,", with: "\"completed\":1.5,"),
    base.replacingOccurrences(of: "\"total\":3}", with: "\"total\":false}"),
  ] {
    try Data(mutated.utf8).write(to: url)
    #expect(
      WorkflowProgressStore.read(jobURL: job) == nil,
      "bool·float 계수가 정수로 디코드되면 안 된다")
  }
  // 원문 그대로는 읽힌다 — 대조군.
  try Data(base.utf8).write(to: url)
  #expect(WorkflowProgressStore.read(jobURL: job) != nil)
}

// ─────────────────────────────────────────────────────────────
//  늦은 배달의 문지기 — URL 열기 완료·승인 시트 응답의 세대 검사
// ─────────────────────────────────────────────────────────────

@Test func deliveryGateBlocksLateOpenAndStaleApproval() {
  // 늦은 열기 실패가 새 job의 상태를 덮는 길 — 문은 닫혀 있다.
  let oldJob = WorkflowDeliveryGate(jobID: "job-old", generation: 3)
  #expect(!oldJob.accepts(currentJobID: "job-new", currentGeneration: 4))
  #expect(!oldJob.accepts(currentJobID: nil, currentGeneration: 4))
  // 같은 job이라도 새 요청이 세대를 올렸으면 옛 배달은 못 들어온다.
  #expect(!oldJob.accepts(currentJobID: "job-old", currentGeneration: 4))
  // 살아 있는 요청만 문을 연다.
  #expect(oldJob.accepts(currentJobID: "job-old", currentGeneration: 3))
}

@Test func identityMatchingTerminalRecordOutranksStaleRunningProgress() {
  // 마지막 종료 발행이 무해하게 실패하면 스냅샷은 running에 멈춰 있다 — 같은
  // 시도의 완성 기록이 이미 내구적이면 그것이 정본이다(S2A-3).
  var success = model()
  let running = decoding(signedProgressLine())!
  #expect(
    success.observe(observation(progress: running, record: .valid(validResult())))
      == .reportAndStop(.completed))
  // 실패 기록도 같다 — 검증된 요약 경로로 닫는다.
  var failure = model()
  let failingRecord = AnalysisRecordState.valid(validResult(status: "error"))
  #expect(
    failure.observe(observation(progress: running, record: failingRecord))
      == .reportAndStop(.completed))
  // 무관한 낡은 기록(다른 시도)은 우선하지 못한다 — 계속 진행 중으로 보인다.
  var unrelated = model()
  #expect(
    unrelated.observe(
      observation(progress: running, record: .valid(validResult(attempt: "attempt-other"))))
      == .report(.analyzing(running)))
}

@Test func unchangedRunningSnapshotStaysTruthfulAndStillAllowsTerminalResult() {
  // 같은 running 스냅샷이 오래 이어지는 것은 긴 오디오 단계의 정상 모양이다 —
  // 정지하지 않고 '마지막 신호' 문장으로 말하며 관측을 계속하고, 이후 정합한
  // 종료 기록은 언제든 완료로 소비한다(S2A-3). 종료 증거가 없다고 실행의
  // 죽음이나 생존을 지어내지 않는다.
  var patient = model(waiting: 2)
  let running = decoding(signedProgressLine())!
  #expect(patient.observe(observation(progress: running)) == .report(.analyzing(running)))
  #expect(patient.observe(observation(progress: running)) == .report(.analyzing(running)))
  #expect(patient.observe(observation(progress: running)) == .report(.analyzing(running)))
  #expect(patient.observe(observation(progress: running)) == .report(.lastKnown(running)))
  #expect(patient.observe(observation(progress: running)) == .report(.lastKnown(running)))
  // 그 사이 정합한 종료 기록이 정착하면 — 즉시 완료다(관측은 계속하고 있었다).
  #expect(
    patient.observe(observation(progress: running, record: .valid(validResult())))
      == .reportAndStop(.completed))
}

@Test func runningProgressMustMatchTheKnownEstimate() {
  // 아는 추정과 어긋나는 진행은 이 job의 것이 아니다 — 종료 기록과 같은 잣대.
  // 승인된(활성) 분석 자리에서는 그 무관 진행이 아무 상태도 못 만든다.
  var known = model()
  let mismatched = decoding(signedProgressLine(estimate: String(repeating: "9", count: 64)))!
  #expect(
    known.observe(
      observation(progress: mismatched, estimate: estimateMessage(), authorization: true))
      == .none)
  // 무관 진행의 거절이 정당한 승인 준비를 누르지 않는다 — 미승인 대조군.
  #expect(
    known.observe(observation(progress: mismatched, estimate: estimateMessage()))
      == .reportAndStop(.estimateReady))
  // 추정을 모르는(낡은) 관측에서는 보수적으로 그냥 따라간다.
  #expect(
    known.observe(observation(progress: mismatched))
      == .report(.analyzing(mismatched)))
}
