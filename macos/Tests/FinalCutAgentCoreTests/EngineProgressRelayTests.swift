import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-3 — 개발 실행의 둘째 운송 구간. 실행기가 도는 동안 외부 job의
//  진행 스냅샷이 App Group job에 보여야 하고, 그 복사는 **이 호출의 신원에
//  묶인다**: 실행기가 받은 argv의 --invocation-id 토큰이 스냅샷에 실려야만
//  옮긴다(잠금 소유자만 실을 수 있다). 잠금은 동기 withLock로만 — 비동기
//  문맥에서 NSLock.lock/unlock을 직접 부르는 것은 Swift 6에서 쓸 수 없다.
// ─────────────────────────────────────────────────────────────

/// 계획 argv에서 이 실행의 호출 신원을 뽑는다 — 자식이 실제로 받는 값.
func invocationID(of plan: EngineLaunchPlan) -> String? {
  switch plan {
  case .bundled(let invocation):
    let arguments = invocation.arguments
    guard
      let index = arguments.firstIndex(of: DevelopmentEngineInvocation.invocationIDArgument),
      index + 1 < arguments.count
    else { return nil }
    return arguments[index + 1]
  case .development(let invocation):
    let arguments = invocation.arguments
    guard
      let index = arguments.firstIndex(of: DevelopmentEngineInvocation.invocationIDArgument),
      index + 1 < arguments.count
    else { return nil }
    return arguments[index + 1]
  case .unavailable:
    return nil
  }
}

/// 막히는 가짜 실행기 — 열리자마자 받은 계획(호출 신원 포함)으로 진행 파일을
/// 쓰고 신호가 올 때까지 기다린다(엔진 자식이 실행 중 파일을 남기는 모양).
/// release가 먼저 오면 나중의 run은 곧바로 지나간다(등록 경쟁 없음).
final class ProgressWritingBlockedExecutor: EngineExecuting, @unchecked Sendable {
  let status: Int32
  private let onRun: @Sendable (EngineLaunchPlan) -> Void
  private let lock = NSLock()
  private var continuation: CheckedContinuation<Void, Never>?
  private var released = false
  private var runCount = 0

  init(status: Int32, onRun: @escaping @Sendable (EngineLaunchPlan) -> Void) {
    self.status = status
    self.onRun = onRun
  }

  var runs: Int { lock.withLock { runCount } }

  func run(_ plan: EngineLaunchPlan) async throws -> Int32 {
    lock.withLock { runCount += 1 }
    onRun(plan)
    await withCheckedContinuation { (pending: CheckedContinuation<Void, Never>) in
      let alreadyReleased = lock.withLock { () -> Bool in
        if released { return true }
        continuation = pending
        return false
      }
      if alreadyReleased { pending.resume() }
    }
    return status
  }

  func release() {
    let pending = lock.withLock { () -> CheckedContinuation<Void, Never>? in
      released = true
      defer { continuation = nil }
      return continuation
    }
    pending?.resume()
  }
}

/// 정준 이해로 서명한 진행 한 줄 — 파이썬 봉투 모양 그대로(릴레이 검증 통과분).
func progressEnvelopeLine(
  invocation: String?,
  job: String? = "job-42",
  attempt: String = "attempt-1",
  stage: String = "vision",
  status: String = "running",
  completed: Int? = 1,
  total: Int? = 3
) -> String {
  let unsigned = progressMessage(
    attempt: attempt, job: job, invocation: invocation, stage: stage, status: status,
    completed: completed, total: total)
  guard let digest = AnalysisProgressValidator.canonicalDigest(of: unsigned) else {
    return ""
  }
  let signed = progressMessage(
    attempt: attempt, job: job, invocation: invocation, stage: stage, status: status,
    completed: completed, total: total, progressRevision: digest)
  return (try? ProtocolCodec.encode(signed, type: "AnalysisProgress", version: "1.3")) ?? ""
}

/// 짧은 유한 대기 — 조건이 참이 되는 순간 돌아가고, 상한을 넘으면 마지막으로
/// 한 번 더 조건을 본 뒤 돌아간다(타이밍만으로 성공을 말하지 않게).
/// 호출자의 격리를 유지한다(SE-0461) — 배우 위 시험이 넘기는 판정 클로저가
/// 격리를 넘지 않는다.
nonisolated(nonsending) func waitBriefly(
  _ limit: TimeInterval = 2.0, until condition: @escaping () -> Bool
) async -> Bool {
  let started = Date()
  while Date().timeIntervalSince(started) < limit {
    if condition() { return true }
    try? await Task.sleep(nanoseconds: 20_000_000)
  }
  return condition()
}

/// 이 파일의 개발 실행 대면 — job 신원(job-42)은 문맥·스냅샷과 같다.
private func makeDevelopmentPlan(external: URL, root: URL) throws -> EngineLaunchPlan {
  let scripts = root.appendingPathComponent("Scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  return .development(
    try DevelopmentEngineInvocation.create(
      operation: .analyze,
      jobID: "job-42",
      externalJob: external,
      applicationScriptsDirectory: scripts
    ))
}

private func prepareRelayFixture() throws -> (
  root: URL, job: URL, external: URL, registry: ActiveSelectionRegistry, revision: String
) {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("appgroup-Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  let external = root.appendingPathComponent("external-Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  let (registry, revision) = try _testRegistry()
  return (root, job, external, registry, revision)
}

@Test func progressSnapshotReachesTheAppGroupJobWhileTheExecutorIsStillRunning() async throws {
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  // 실행기는 자기가 받은 argv의 호출 신원을 스냅샷에 싣는다 — 잠금 소유자만
  // 아는 값이 실제 launch wire를 흘러가는 모양.
  let progressURL = external.appendingPathComponent(RoundTripJob.progressFileName)
  let executor = ProgressWritingBlockedExecutor(status: 0) { plan in
    let line = progressEnvelopeLine(invocation: invocationID(of: plan))
    try? Data(line.utf8).write(to: progressURL, options: [.atomic])
  }
  let session = EngineSessionRunner(executor: executor)

  let running = Task {
    try await session.run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external))
  }
  // 실행기가 외부 job에 진행 파일을 쓰고 아직 막혀 있는 동안 — App Group job에서
  // 같은 바이트가 보여야 한다(실행 중 관측).
  let relayedURL = job.appendingPathComponent(RoundTripJob.progressFileName)
  let relayed = await waitBriefly { FileManager.default.fileExists(atPath: relayedURL.path) }
  #expect(relayed, "실행 중 App Group job에 진행 파일이 보이지 않는다")
  if relayed {
    let expected = try String(contentsOf: progressURL, encoding: .utf8)
    let copied = try String(contentsOf: relayedURL, encoding: .utf8)
    #expect(copied == expected)
  }
  executor.release()
  let outcome = try await running.value
  #expect(outcome == .completed)
  #expect(executor.runs == 1, "실행기 호출은 정확히 한 번이다")
}

@Test func foreignInvocationSnapshotsAreNeverRelayed() async throws {
  // 남의 호출(또는 낡은 스냅샷)은 신원이 다르다 — 릴레이가 옮기지 않는다.
  // 중복(3)으로 지는 호출이 이긴 호출의 진행을 흉내 내는 길이 없다(S2A-3).
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  let progressURL = external.appendingPathComponent(RoundTripJob.progressFileName)
  let executor = ProgressWritingBlockedExecutor(status: 0) { _ in
    let line = progressEnvelopeLine(invocation: "someone-elses-invocation")
    try? Data(line.utf8).write(to: progressURL, options: [.atomic])
  }
  let running = Task {
    try await EngineSessionRunner(executor: executor).run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external))
  }
  let relayed = await waitBriefly {
    FileManager.default.fileExists(atPath: job.appendingPathComponent(RoundTripJob.progressFileName).path)
  }
  #expect(!relayed, "무관 신원의 스냅샷이 흘러들었다")
  executor.release()
  #expect(try await running.value == .completed)
  // 실행이 끝난 뒤에도 결과 동기화는 이 파일을 다루지 않는다 — 진행은 게이트 밖.
  #expect(
    !FileManager.default.fileExists(
      atPath: job.appendingPathComponent(RoundTripJob.progressFileName).path))
}

@Test @MainActor func duplicateExitRelaysNothingAndKeepsNoResultSync() async throws {
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  // 중복 호출의 자식은 잠금을 얻지 못해 3으로 물러난다 — 아무 것도 쓰지 않는다.
  // 외부 job에 있는 것은 (다른 곳에서 도는) 이긴 호출의 스냅샷뿐이다.
  let foreign = progressEnvelopeLine(invocation: "winners-invocation")
  try Data(foreign.utf8).write(
    to: external.appendingPathComponent(RoundTripJob.progressFileName), options: [.atomic])
  let executor = ProgressWritingBlockedExecutor(status: 3, onRun: { _ in })
  var synced: [Bool] = []
  let running = Task {
    try await EngineSessionRunner(executor: executor).run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external),
      syncResults: { _, _, diagnosticsOnly in synced.append(diagnosticsOnly) })
  }
  // 중복 호출도 실행기가 돌았다가 3으로 물러난다 — 그 사이 릴레이는 조용하다.
  let leakedWhileDuplicate = await waitBriefly {
    FileManager.default.fileExists(
      atPath: job.appendingPathComponent(RoundTripJob.progressFileName).path)
  }
  #expect(!leakedWhileDuplicate, "지는 호출의 릴레이가 이긴 호출의 진행을 흘려보내지 않는다")
  executor.release()
  #expect(try await running.value == .alreadyRunning)
  #expect(synced.isEmpty, "중복은 어떤 결과 동기화도 하지 않는다")
  #expect(
    !FileManager.default.fileExists(
      atPath: job.appendingPathComponent(RoundTripJob.progressFileName).path))
}

@Test func relayStopsWhenTheJobIsCancelledMidRun() async throws {
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  // 실행 중 취소 기록이 생긴다 — 릴레이는 다음 틱에서 스스로 멈춘다.
  let executor = ProgressWritingBlockedExecutor(status: 0) { plan in
    try? Data("cancelled\n".utf8)
      .write(to: job.appendingPathComponent(CancellationStore.fileName))
    let line = progressEnvelopeLine(invocation: invocationID(of: plan))
    try? Data(line.utf8).write(
      to: external.appendingPathComponent(RoundTripJob.progressFileName), options: [.atomic])
  }
  let running = Task {
    try await EngineSessionRunner(executor: executor).run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external))
  }
  // 실행기가 막혀 있는 동안 틱이 지나가도 — 취소된 job의 릴레이는 아무 것도
  // 흘리지 않고 스스로 멈춘다(유한 상한 안에 판정된다).
  let leakedWhileCancelled = await waitBriefly {
    FileManager.default.fileExists(
      atPath: job.appendingPathComponent(RoundTripJob.progressFileName).path)
  }
  #expect(!leakedWhileCancelled, "취소된 실행의 릴레이가 관측 파일을 흘렸다")
  executor.release()
  #expect(try await running.value == .stoppedBeforeSync)
}

@Test func relayJoinsBeforeTheRunnerReturns() async throws {
  // 실행이 끝나면 릴레이 과제는 합류해 끝난다 — 그 뒤에는 새 스냅샷이 나와도
  // 더 옮겨지지 않는다(정리가 타이머를 남기지 않는다는 증거).
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  let progressURL = external.appendingPathComponent(RoundTripJob.progressFileName)
  let relayedURL = job.appendingPathComponent(RoundTripJob.progressFileName)
  let executor = ProgressWritingBlockedExecutor(status: 0) { plan in
    let line = progressEnvelopeLine(invocation: invocationID(of: plan))
    try? Data(line.utf8).write(to: progressURL, options: [.atomic])
  }
  let running = Task {
    try await EngineSessionRunner(executor: executor).run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external))
  }
  let copiedWhileBlocked = await waitBriefly {
    FileManager.default.fileExists(atPath: relayedURL.path)
  }
  #expect(copiedWhileBlocked)
  let invocation = invocationID(
    of: try makeDevelopmentPlan(external: external, root: root))
  executor.release()
  _ = try await running.value
  let afterFinish = try Data(contentsOf: relayedURL)
  // 끝난 뒤 외부에서 같은 신원의 스냅샷이 바뀌어도 App Group은 그대로다.
  try Data(progressEnvelopeLine(invocation: invocation, status: "failed").utf8)
    .write(to: progressURL, options: [.atomic])
  let changed = await waitBriefly {
    (try? Data(contentsOf: relayedURL)) != afterFinish
  }
  #expect(!changed, "실행 뒤에 릴레이가 살아 있으면 안 된다")
}

@Test func bundledRunsNeedNoRelayBecauseTheEnginePublishesIntoTheJobRoot() async throws {
  // 번들길(외부 job 없음)의 실제 대조군 — 릴레이가 없어도 완료는 그대로다.
  // 파이썬이 job 루트에 직접 발행하므로 앱 쪽 운송은 없다(S2A-3 첫째 구간).
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("appgroup-Jobs", isDirectory: true)
    .appendingPathComponent("job-42", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  let outcome = try await EngineSessionRunner(executor: RecordingExecutor()).run(
    _runtimeContext(
      plan: try makeBundledPlan(), registry: registry, revision: revision, appGroupJob: job))
  #expect(outcome == .completed)
  #expect(
    !FileManager.default.fileExists(
      atPath: job.appendingPathComponent(RoundTripJob.progressFileName).path),
    "번들길에서 앱이 진행 파일을 만들지 않는다 — 파이썬이 job 루트에 직접 쓴다")
}

/// 발행된 바이트를 기록하는 상자 — 검증·발행 바이트 일치의 증거.
final class PublishedBox: @unchecked Sendable {
  private let lock = NSLock()
  private var rows: [Data] = []

  func append(_ data: Data) { lock.withLock { rows.append(data) } }

  var published: [Data] { lock.withLock { rows } }
}

@Test func relayValidatesAndPublishesTheSameBytesEvenWhenTheSourceSwaps() async throws {
  // 검증과 발행이 같은 바이트를 본다(S2A-3) — 발행 순간에 소스가 다른 신원의
  // 스냅샷으로 바뀌어도 이미 검증한 바이트만 흘러간다.
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  let sourceURL = external.appendingPathComponent(RoundTripJob.progressFileName)
  let destinationURL = job.appendingPathComponent(RoundTripJob.progressFileName)
  let fixed = Data(
    progressEnvelopeLine(invocation: "deterministic-invocation").utf8)
  let box = PublishedBox()

  let relay = EngineProgressRelay(
    intervalNanoseconds: 10_000_000,
    read: { _ in fixed },
    decode: { bytes in EngineProgressRelay.decodeSnapshot(bytes) },
    publish: { bytes, destination in
      // 발행 직전 경쟁의 흉내 — 소스가 남의 신원으로 바뀌었다.
      try? Data(progressEnvelopeLine(invocation: "swapped-invocation").utf8)
        .write(to: sourceURL, options: [.atomic])
      try EngineProgressRelay.atomicPublish(bytes, to: destination)
      box.append(bytes)
    })
  let executor = ProgressWritingBlockedExecutor(status: 0, onRun: { _ in })
  let running = Task {
    try await EngineSessionRunner(executor: executor).run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external),
      invocationID: "deterministic-invocation",
      progressRelay: relay)
  }
  let published = await waitBriefly { !box.published.isEmpty }
  #expect(published)
  executor.release()
  #expect(try await running.value == .completed)
  #expect(box.published.first == fixed, "검증한 바이트와 발행한 바이트가 다르다")
  #expect(try Data(contentsOf: destinationURL) == fixed)
  #expect(
    try String(contentsOf: sourceURL, encoding: .utf8)
      .contains("swapped-invocation"),
    "경쟁 흉내가 실제로 소스를 바꿨는지 확인")
}

@Test func stoppedSessionNeverNotifiesAfterwards() async throws {
  // 정리(teardown) 뒤의 늦은 알림 — 세대 검사에서 조용히 끝난다.
  let gate = TickGate()
  let states = StateBox()
  let armed = ArmedObservationBox(
    armedObservation: observation(record: .valid(validResult())))
  let session = WorkflowRefreshSession(
    configuration: .init(intervalNanoseconds: 0),
    tick: { _ in await gate.wait() },
    observe: { _ in armed.next() },
    notify: { states.append($0) })
  session.start(jobURL: URL(fileURLWithPath: "/tmp/teardown-job"), jobID: "job-42", flavor: .analysis)
  try await Task.sleep(nanoseconds: 50_000_000)
  session.stop()
  armed.arm()
  gate.open()  // 멈춘 세션의 틱이 깨어나도 — 알림은 없다.
  try await Task.sleep(nanoseconds: 150_000_000)
  #expect(states.states.isEmpty, "정리 뒤 알림이 새어 나왔다: \(states.states)")
}

/// 실행기가 받은 호출 신원을 담는 상자 — 시험이 이 실행의 신원을 알게 한다.
final class InvocationBox: @unchecked Sendable {
  private let lock = NSLock()
  private var value: String?

  func set(_ id: String) { lock.withLock { value = id } }

  var id: String? { lock.withLock { value } }
}

@Test @MainActor func relayStopsOnSelectionDriftWhileTheExecutorStaysBlocked() async throws {
  let (root, job, external, registry, revision) = try prepareRelayFixture()
  defer { try? FileManager.default.removeItem(at: root) }

  let progressURL = external.appendingPathComponent(RoundTripJob.progressFileName)
  let relayedURL = job.appendingPathComponent(RoundTripJob.progressFileName)
  let invocation = InvocationBox()
  let executor = ProgressWritingBlockedExecutor(status: 0) { plan in
    guard let id = invocationID(of: plan) else { return }
    invocation.set(id)
    try? Data(progressEnvelopeLine(invocation: id).utf8)
      .write(to: progressURL, options: [.atomic])
  }
  let running = Task {
    try await EngineSessionRunner(executor: executor).run(
      _runtimeContext(
        plan: try makeDevelopmentPlan(external: external, root: root),
        registry: registry, revision: revision,
        appGroupJob: job, externalJob: external))
  }
  // 첫 스냅샷은 흘러간다 — 실행기는 아직 막혀 있다.
  let firstArrived = await waitBriefly { FileManager.default.fileExists(atPath: relayedURL.path) }
  #expect(firstArrived)
  let first = try Data(contentsOf: relayedURL)
  // 실행 중 활성 프로젝트가 바뀐다 — 릴레이는 발행 직전 게이트에서 멈춘다.
  registry.invalidate()
  try Data(
    progressEnvelopeLine(invocation: invocation.id ?? "missing", attempt: "attempt-2", completed: 2)
      .utf8
  ).write(to: progressURL, options: [.atomic])
  let changed = await waitBriefly { (try? Data(contentsOf: relayedURL)) != first }
  #expect(!changed, "드리프트 뒤에 릴레이가 살아 있으면 안 된다")
  // 원본 종료를 보존한다 — 실행기는 놓아주고 staleSelection으로 닫는다.
  executor.release()
  #expect(try await running.value == .staleSelection)
}
