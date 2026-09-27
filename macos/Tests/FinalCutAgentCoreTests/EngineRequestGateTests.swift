import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-1 — job 실행 소유권: 중복은 alreadyRunning, sync/import 없음
// ─────────────────────────────────────────────────────────────

/// 엔진 종료 코드 3은 중복 소유권이다 — 실패 시도가 아니므로 어떤 결과도
/// 동기화하거나 가져오지 않는다. main.swift가 오늘 부르는 실행 경계
/// (EngineSessionRunner)에서 못 박는다.
@Test func engineExitThreeMeansDuplicateOwnershipNotFailure() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  var synced: [URL] = []
  let imported = URLBox()
  let duplicate = try await EngineSessionRunner(executor: RecordingExecutor(status: 3)).run(
    _runtimeContext(
      plan: try makeBundledPlan(),
      registry: registry,
      revision: revision,
      appGroupJob: job,
      externalJob: external,
      operation: .roughcut
    ),
    syncResults: { _, to, _ in synced.append(to) },
    importRoughcut: { imported.urls.append($0) }
  )
  #expect(synced.isEmpty)
  #expect(imported.urls.isEmpty)
  #expect(duplicate == .alreadyRunning)
}

/// 스테이징 콜백의 도착을 알리고 풀려날 때까지 기다리는 동기화 도우미 —
/// 시간 추측 없이 첫 요청/중복 요청의 순서를 못 박는다.
actor ArrivalGate {
  private var arrived = false
  private var arrivalWaiters: [CheckedContinuation<Void, Never>] = []
  private var blocked: [CheckedContinuation<Void, Never>] = []

  func arrive() {
    arrived = true
    while !arrivalWaiters.isEmpty { arrivalWaiters.removeFirst().resume() }
  }

  func arriveAndWaitForRelease() async {
    arrive()
    await withCheckedContinuation { blocked.append($0) }
  }

  func waitForArrival() async {
    if arrived { return }
    await withCheckedContinuation { arrivalWaiters.append($0) }
  }

  func release() {
    while !blocked.isEmpty { blocked.removeFirst().resume() }
  }
}

private struct GateTestSetupError: Error {}

/// 스테이징 호출 수를 세는 상자 — 동시 요청 관측은 상자로만 주고받는다.
final class CountBox: @unchecked Sendable {
  var value = 0
}

/// 게이트 안에서 돌릴 실제 세션 — 동기화·import 관측은 상자에 쌓는다.
nonisolated func _gatedRoughcutSession(
  executor: any EngineExecuting,
  registry: ActiveSelectionRegistry,
  revision: String,
  job: URL,
  external: URL,
  synced: URLBox,
  imported: URLBox
) async throws -> EngineSessionOutcome {
  try await EngineSessionRunner(executor: executor).run(
    _runtimeContext(
      plan: try makeBundledPlan(),
      registry: registry,
      revision: revision,
      appGroupJob: job,
      externalJob: external,
      operation: .roughcut
    ),
    syncResults: { _, to, _ in synced.urls.append(to) },
    importRoughcut: { imported.urls.append($0) }
  )
}

/// 중복 요청은 본문을 시작조차 하지 않는다 — 스테이징·실행기·import는 첫 요청
/// 것이 정확히 한 번, 첫 소유권은 그대로, 결과는 alreadyRunning이다.
@Test @MainActor func duplicateRequestRunsOnceAndPreservesFirstOwnership() async throws {
  let gate = EngineRequestGate()
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  // 동기화 관측용 결과 산물 — 성공 실행은 이 한 파일을 앱 job으로 가져온다.
  try Data("roughcut\n".utf8).write(to: external.appendingPathComponent("roughcut.fcpxml"))
  defer { try? FileManager.default.removeItem(at: root) }

  let arrival = ArrivalGate()
  let executor = RecordingExecutor()
  let staged = CountBox()
  let synced = URLBox()
  let imported = URLBox()
  let board = EngineJobBoard()

  async let firstOutcome: EngineSessionOutcome = gate.perform(jobURL: job) {
    staged.value += 1  // main.swift의 DevelopmentJobStager.stage 자리 — 첫 스테이징이다.
    let (registry, revision) = try _testRegistry()
    await arrival.arriveAndWaitForRelease()
    board.begin(
      key: EngineRequestGate.ownershipKey(for: job),
      context: EngineJobBoard.Context(
        jobID: "job-42", jobURL: job, runningJob: job, externalJob: external))
    return try await _gatedRoughcutSession(
      executor: executor,
      registry: registry,
      revision: revision,
      job: job,
      external: external,
      synced: synced,
      imported: imported
    )
  }
  await arrival.waitForArrival()  // 첫 요청이 스테이징을 마치고 실행 안에서 막혀 있다
  #expect(gate.isHeld(jobURL: job))

  let duplicate = await gate.perform(jobURL: job) {
    Issue.record("중복 요청의 본문은 실행되지 않는다")
    return .completed
  }
  #expect(duplicate == .alreadyRunning)
  #expect(staged.value == 1)
  #expect(executor.plans.count == 0)  // 아직 첫 실행기도 시작 전 — 중복은 더하지 않는다
  #expect(gate.isHeld(jobURL: job))  // 중복이 첫 소유권을 대체하지 않는다
  #expect(board.current == nil)  // 중복은 판에 아무것도 올리지 않는다

  await arrival.release()
  let first = try await firstOutcome
  #expect(first == .completed)
  #expect(board.current?.jobID == "job-42")  // 첫 요청의 실행 문맥만 판에 있다
  #expect(executor.plans.count == 1)  // 실행기는 정확히 한 번 — 첫 요청의 몫
  #expect(synced.urls.count == 1)  // 정확히 한 번의 동기화 — 중복의 몫은 없다
  #expect(imported.urls.map(\.lastPathComponent) == ["roughcut.fcpxml"])
  #expect(!gate.isHeld(jobURL: job))  // 성공으로 끝나면 해제된다
  board.end(key: EngineRequestGate.ownershipKey(for: job))  // 요청 본문의 defer 자리
  #expect(board.current == nil)
}

/// 정준 별칭은 같은 소유권으로 충돌하고, 글자가 같은 job_id라도 디렉터리가
/// 다르면 독립이다 — 전역 잠금이 아니다.
@Test @MainActor func canonicalAliasesCollideWhileDistinctDirectoriesOverlap() async throws {
  let gate = EngineRequestGate()
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let jobs = root.appendingPathComponent("Jobs", isDirectory: true)
  let first = jobs.appendingPathComponent("job-1", isDirectory: true)
  try FileManager.default.createDirectory(at: first, withIntermediateDirectories: true)
  // symlink 별칭 — 같은 디렉터리의 다른 표기다.
  let alias = root.appendingPathComponent("alias")
  try FileManager.default.createSymbolicLink(
    atPath: alias.path, withDestinationPath: first.path)
  // 같은 이름의 다른 디렉터리 — job_id 표기가 같아도 다른 job이다.
  let elsewhere = root.appendingPathComponent("elsewhere", isDirectory: true)
    .appendingPathComponent("job-1", isDirectory: true)
  try FileManager.default.createDirectory(
    at: elsewhere, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  let claimed = gate.tryClaim(jobURL: first)
  #expect(claimed != nil)
  #expect(gate.tryClaim(jobURL: alias) == nil)  // 별칭은 첫 소유권에 부딪힌다
  #expect(gate.isHeld(jobURL: alias))
  #expect(
    EngineRequestGate.ownershipKey(for: alias)
      == EngineRequestGate.ownershipKey(for: first))

  let independent = gate.tryClaim(jobURL: elsewhere)
  #expect(independent != nil)  // 다른 디렉터리는 함께 잡을 수 있다
  gate.release(independent!)
  gate.release(claimed!)
  #expect(!gate.isHeld(jobURL: first))
}

/// 게이트는 어떤 끝에서든 해제된다 — 던지는 설정 오류, 취소, 실행 실패,
/// 성공 전부. 그 뒤의 같은 job 재청구는 즉시 된다.
@Test @MainActor func gateReleasesOnEveryOutcomeAndAllowsReacquisition() async throws {
  let gate = EngineRequestGate()
  let job = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)

  do {
    _ = try await gate.perform(jobURL: job) { throw GateTestSetupError() }
    Issue.record("설정 오류는 밖으로 나가야 한다")
  } catch {
    // 기대 — 해제는 defer가 이미 했다.
  }
  #expect(!gate.isHeld(jobURL: job))

  for outcome in [EngineSessionOutcome.cancelledBeforeRun, .stoppedBeforeSync] {
    let stopped = await gate.perform(jobURL: job) { outcome }
    #expect(stopped == outcome)
    #expect(!gate.isHeld(jobURL: job))
  }
  let failed = await gate.perform(jobURL: job) { .engineFailed(1) }
  #expect(failed == .engineFailed(1))
  let completed = await gate.perform(jobURL: job) { .completed }
  #expect(completed == .completed)
  #expect(!gate.isHeld(jobURL: job))

  // 실패·취소 뒤의 재획득 — 같은 job이 다시 실행된다.
  let reacquired = await gate.perform(jobURL: job) { .completed }
  #expect(reacquired == .completed)
}

/// 중복(3)이 드리프트·동시 취소와 겹나도 이 실행은 시도가 아니다 — 중복 판정은
/// 실행기가 돌려준 순간 어떤 읽기·쓰기보다 앞서고, 기록·동기화는 없다.
@Test func duplicateExitWithDriftOrConcurrentCancellationRecordsNoCancellation() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let (registry, revision) = try _testRegistry()

  var synced: [URL] = []
  let drifted = try await EngineSessionRunner(
    executor: RecordingExecutor(status: 3, onRun: { registry.invalidate() })
  ).run(
    _runtimeContext(
      plan: try makeBundledPlan(),
      registry: registry,
      revision: revision,
      appGroupJob: job,
      externalJob: external
    ),
    syncResults: { _, to, _ in synced.append(to) }
  )
  #expect(drifted == .alreadyRunning)
  #expect(synced.isEmpty)

  // 정상 소유자가 실행 중 취소됐어도 중복은 중복이다 — stoppedBeforeSync가 아니다.
  // 위 케이스가 레지스트리를 무효화했으니 여기는 새 리비전으로 시작한다.
  let (freshRegistry, freshRevision) = try _testRegistry()
  let cancelledMidRun = try await EngineSessionRunner(
    executor: RecordingExecutor(status: 3, onRun: {
      try? Data("cancelled\n".utf8)
        .write(to: job.appendingPathComponent(CancellationStore.fileName))
    })
  ).run(
    _runtimeContext(
      plan: try makeBundledPlan(),
      registry: freshRegistry,
      revision: freshRevision,
      appGroupJob: job,
      externalJob: external
    ),
    syncResults: { _, to, _ in synced.append(to) }
  )
  #expect(cancelledMidRun == .alreadyRunning)
  #expect(synced.isEmpty)
  // 이 실행은 시도가 아니었다 — 취소 파일은 시험이 쓴 바이트 그대로고(드리프트
  // 기록이 붙지 않았다), 외부 루트에는 아무 기록도 없다.
  let cancellation = try String(
    contentsOf: job.appendingPathComponent(CancellationStore.fileName), encoding: .utf8)
  #expect(cancellation == "cancelled\n")
  #expect(
    !FileManager.default.fileExists(
      atPath: external.appendingPathComponent(CancellationStore.fileName).path))
}

/// 항상 던지는 실행기 — 실행기 오류가 게이트 해제로 이어지는 길을 본다.
final class ThrowingExecutor: EngineExecuting, @unchecked Sendable {
  struct LaunchFailure: Error {}

  func run(_ plan: EngineLaunchPlan) async throws -> Int32 {
    throw LaunchFailure()
  }
}

/// 실행기가 오류를 던지면 요청 전체가 그대로 나가고(앱이 알림으로 보여준다)
/// 소유권은 놓인다 — 같은 job의 재실행은 즉시 된다.
@Test @MainActor func thrownExecutorErrorReleasesTheGateForReacquisition() async throws {
  let gate = EngineRequestGate()
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }

  do {
    _ = try await gate.perform(jobURL: job) {
      let (registry, revision) = try _testRegistry()
      return try await _gatedRoughcutSession(
        executor: ThrowingExecutor(),
        registry: registry,
        revision: revision,
        job: job,
        external: external,
        synced: URLBox(),
        imported: URLBox()
      )
    }
    Issue.record("실행기 오류는 밖으로 나가야 한다")
  } catch {
    // 기대 — 해제는 perform의 defer가 이미 했다.
  }
  #expect(!gate.isHeld(jobURL: job))
  let reacquired = await gate.perform(jobURL: job) { .completed }
  #expect(reacquired == .completed)
}

/// 실제 Task 취소가 게이트를 해제한다 — 취소에 반응하는 대기(Task.sleep)를
/// 지난 뒤에도 재획득된다. 반환값 클로저만으로는 증명되지 않는 길이다.
@Test @MainActor func cancellingTheOwningTaskReleasesTheGate() async throws {
  let gate = EngineRequestGate()
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let arrival = ArrivalGate()

  let owning = Task { @MainActor in
    try await gate.perform(jobURL: job) {
      await arrival.arrive()  // 도착 신호만 — 뒤는 취소 가능한 대기로 막는다
      while !Task.isCancelled {
        try await Task.sleep(nanoseconds: 5_000_000)
      }
      throw CancellationError()
    }
  }
  await arrival.waitForArrival()
  #expect(gate.isHeld(jobURL: job))

  owning.cancel()
  do {
    _ = try await owning.value
    Issue.record("취소는 오류로 나와야 한다")
  } catch {
    // 기대
  }
  #expect(!gate.isHeld(jobURL: job))
  let reacquired = await gate.perform(jobURL: job) { .completed }
  #expect(reacquired == .completed)
}

/// 서로 다른 job은 perform 수준에서 실제로 겹친다 — 하나가 대기하는 동안
/// 다른 하나가 소유권을 쥐고 함께 진행한다(전역 잠금이 아니다).
@Test @MainActor func distinctJobsOverlapThroughPerform() async throws {
  let gate = EngineRequestGate()
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let first = root.appendingPathComponent("job-a", isDirectory: true)
  let second = root.appendingPathComponent("job-b", isDirectory: true)
  try FileManager.default.createDirectory(at: first, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: second, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let firstArrival = ArrivalGate()
  let secondArrival = ArrivalGate()

  async let firstOutcome: EngineSessionOutcome = gate.perform(jobURL: first) {
    await firstArrival.arriveAndWaitForRelease()
    return .completed
  }
  async let secondOutcome: EngineSessionOutcome = gate.perform(jobURL: second) {
    await secondArrival.arriveAndWaitForRelease()
    return .completed
  }
  // 둘 다 소유권을 쥔 채 대기한다 — 겹침의 동기화 증거.
  await firstArrival.waitForArrival()
  await secondArrival.waitForArrival()
  #expect(gate.isHeld(jobURL: first))
  #expect(gate.isHeld(jobURL: second))

  await firstArrival.release()
  await secondArrival.release()
  #expect(try await firstOutcome == .completed)
  #expect(try await secondOutcome == .completed)
  #expect(!gate.isHeld(jobURL: first))
  #expect(!gate.isHeld(jobURL: second))
}

/// job별 취소 문맥 판 — 한 job이 끝나도 다른 job의 취소 대상은 산다.
/// 취소 버튼은 가장 최근에 시작한 실행 중 job을 겨냥하는 것도 못 박는다.
@Test @MainActor func finishingOneJobLeavesTheOtherCancellable() throws {
  let board = EngineJobBoard()
  let first = EngineJobBoard.Context(
    jobID: "job-a", jobURL: URL(fileURLWithPath: "/jobs/a"),
    runningJob: URL(fileURLWithPath: "/jobs/a"), externalJob: nil)
  let second = EngineJobBoard.Context(
    jobID: "job-b", jobURL: URL(fileURLWithPath: "/jobs/b"),
    runningJob: URL(fileURLWithPath: "/external/b"), externalJob: URL(fileURLWithPath: "/external/b"))

  board.begin(key: "a", context: first)
  #expect(board.current == first)
  board.begin(key: "b", context: second)  // 최근 시작이 취소 대상
  #expect(board.current == second)

  // 옛 job(A)이 먼저 끝나도 새 job(B)의 취소 문맥은 산다 — 낡은 main.swift의
  // 단일 슬롯 defer가 지우던 바로 그 자리다.
  board.end(key: "a")
  #expect(board.current == second)

  board.end(key: "b")  // 반대 순서도 마찬가지
  #expect(board.current == nil)

  // 재시작한 job은 다시 최근 시작이 된다.
  board.begin(key: "a", context: first)
  board.begin(key: "b", context: second)
  board.end(key: "b")
  #expect(board.current == first)
  board.end(key: "a")
  #expect(board.current == nil)
}

/// 실제 NSUserUnixTask를 통한 소유된 상태 기록 계약: 진입점은 상태 전달이
/// 끝난 뒤에만 0으로 끝난다 — 그래서 기록은 **오류가 없을 때만** 받아들인다.
/// 비0 종료·신호로 끝난 스크립트가 온전한 기록을 남겼어도 재분류하지 않고,
/// 없거나 깨진 기록·시작 실패도 상태 추측 없이 오류로 나간다.
@Test func developmentLauncherPropagatesScriptExitStatuses() async throws {
  let dir = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }

  /// 상태 파일($1)에 기록을 남기는 스크립트. 진입점 계약대로 기록 뒤 exit 0으로
  /// 끝난다(finish가 있으면 그 결말로 끝낸다). noise는 기록 앞에 흘려보내는 출력
  /// 바이트 수 — 파이프 용량을 넘겨도 막히지 않음을 본다.
  func invocation(
    record code: Int32, noise: Int = 0, rawRecord: String? = nil, oversize: Bool = false,
    finish: String? = nil, recordless: Bool = false, mode: Int = 0o755
  ) throws -> DevelopmentEngineInvocation {
    var lines = ["#!/bin/sh"]
    if noise > 0 {
      lines.append("dd if=/dev/zero bs=1024 count=\(noise / 1024) 2>/dev/null")
    }
    if oversize {
      lines.append("head -c 300 /dev/zero > \"$1\"")
    } else if !recordless {
      if let rawRecord {
        lines.append("printf '\(rawRecord)' > \"$1\"")
      } else {
        lines.append("token=$(basename \"$1\" .status)")
        lines.append("printf 'VLOGSRT-ENGINE-STATUS %s \(code)\\n' \"$token\" > \"$1\"")
      }
    }
    lines.append(finish ?? "exit 0")
    let url = dir.appendingPathComponent("engine-\(UUID().uuidString).sh")
    try Data(lines.joined(separator: "\n").appending("\n").utf8).write(to: url)
    try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
    return DevelopmentEngineInvocation(executableURL: url, arguments: [])
  }

  func assertThrowsStatusError(_ invocation: DevelopmentEngineInvocation) async {
    do {
      _ = try await DevelopmentEngineLauncher().run(invocation)
      Issue.record("상태를 추측할 수 없는 호출은 오류여야 한다")
    } catch {
      // 기대
    }
  }

  let launcher = DevelopmentEngineLauncher()
  // 전달 성공(스크립트 exit 0)이면 안쪽 코드가 그대로 상태가 된다.
  #expect(try await launcher.run(invocation(record: 0)) == 0)
  #expect(try await launcher.run(invocation(record: 3)) == 3)
  #expect(try await launcher.run(invocation(record: 1)) == 1)
  // 파이프 용량(64KB)을 훌쩍 넘는 출력 — 상태 기록은 별도 채널이라 영향이 없다.
  #expect(try await launcher.run(invocation(record: 0, noise: 200_000)) == 0)

  // 온전한 기록을 남기고 비0으로 끝나면(낡은 래퍼 모양) 재분류하지 않는다.
  try await assertThrowsStatusError(invocation(record: 3, finish: "exit 3"))
  // 온전한 기록을 남기고 신호로 죽어도 오류다 — 기록이 오류를 덮지 못한다.
  try await assertThrowsStatusError(invocation(record: 3, finish: "kill -9 $$"))

  // 기록이 없으면 상태를 추측하지 않는다 — 오류다.
  try await assertThrowsStatusError(invocation(record: 0, recordless: true))
  // 기록이 이 호출의 것이 아니면(잘못된 token·쓰레기·과대) 오류다.
  try await assertThrowsStatusError(
    invocation(record: 3, rawRecord: "VLOGSRT-ENGINE-STATUS not-my-token 3\\n"))
  try await assertThrowsStatusError(invocation(record: 0, rawRecord: "garbage"))
  try await assertThrowsStatusError(invocation(record: 0, oversize: true))

  // 시작 실패(실행 권한 없음)도 상태가 아니라 오류로 나간다.
  try await assertThrowsStatusError(invocation(record: 0, mode: 0o644))
}

/// 실제 다리: NSUserUnixTask → 래퍼 모양의 sh → 저장소 venv 파이썬 → 실제
/// `vlogsrt-engine.py`가 소유된 상태 기록을 남기고 실행기가 그 값을 읽는다.
/// 실패(1) 케이스가 핵심이다 — 비0 종료를 오류로만 알리는 NSUserUnixTask의 한계를
/// 기록이 넘어선다. 셸 가짜가 아닌 진짜 진입점이 기록을 쓴다.
@Test func launcherReadsTheRealPythonEntrypointStatusRecord() async throws {
  let repo = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()  // EngineRequestGateTests.swift
    .deletingLastPathComponent()  // FinalCutAgentCoreTests
    .deletingLastPathComponent()  // Tests
    .deletingLastPathComponent()  // macos → 저장소 루트
  let python = repo.appendingPathComponent(".venv/bin/python")
  let engineScript = repo.appendingPathComponent("macos/scripts/vlogsrt-engine.py")
  try FileManager.default.attributesOfItem(atPath: python.path)  // venv 없으면 즉시 실패

  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let wrapper = root.appendingPathComponent("bridge.sh")
  try Data(
    (
      "#!/bin/sh\nexport VLOGSRT_OFFLINE=1\n"
        + "exec \"\(python.path)\" \"\(engineScript.path)\" \"$@\"\n"
    ).utf8
  ).write(to: wrapper)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)

  // 성공 job — 최소 오프라인 프로젝트(receive·inspect까지 0으로 끝난다).
  let goodJob = root.appendingPathComponent("job-good", isDirectory: true)
  try FileManager.default.createDirectory(at: goodJob, withIntermediateDirectories: true)
  let media = root.appendingPathComponent("clip.mov")
  try Data("synthetic-bridge-media".utf8).write(to: media)
  let fcpxml = """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE fcpxml>
    <fcpxml version="1.14">
      <resources>
        <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
        <asset id="r2" name="clip.mov" start="0s" duration="600s" hasAudio="1" format="r1">
          <media-rep kind="original-media" src="\(media.absoluteString)"/>
        </asset>
      </resources>
      <library>
        <event name="이벤트">
          <project name="브리지" uid="PROJECT-BRIDGE">
            <sequence format="r1" tcStart="0s" duration="600s">
              <spine>
                <asset-clip ref="r2" name="클립0" offset="0s" start="0s" duration="12s"/>
              </spine>
            </sequence>
          </project>
        </event>
      </library>
    </fcpxml>
    """
  try Data(fcpxml.utf8).write(to: goodJob.appendingPathComponent("input.fcpxml"))

  let launcher = DevelopmentEngineLauncher()
  let succeeding = DevelopmentEngineInvocation(
    executableURL: wrapper, arguments: ["analyze", "job-good", goodJob.path])
  #expect(try await launcher.run(succeeding) == 0)
  #expect(
    FileManager.default.fileExists(
      atPath: goodJob.appendingPathComponent("analysis-estimate.jsonl").path))

  // 실패 job — input이 없어 receive가 실패한다. 엔진은 1로 끝나고, NSUserUnixTask는
  // 그 1을 오류로만 전한다 — 소유된 기록이 1을 되찾는다.
  let badJob = root.appendingPathComponent("job-bad", isDirectory: true)
  try FileManager.default.createDirectory(at: badJob, withIntermediateDirectories: true)
  let failing = DevelopmentEngineInvocation(
    executableURL: wrapper, arguments: ["analyze", "job-bad", badJob.path])
  #expect(try await launcher.run(failing) == 1)
}
