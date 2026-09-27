import CryptoKit
import Foundation
import Testing

@testable import FinalCutAgentCore

/// 합성 키의 sha256 다이제스트 — 소비 기대값은 이 모양으로만 자식에 간다.
/// 원문 합성 키는 시험 재료 어디에도 쓰지 않는다(지속 표면 검사가 드라이버·
/// 심까지 훑는다).
nonisolated func _digestOf(_ value: String) -> String {
  SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
}

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

/// 자식 stdout에서 "held" 줄이 나올 때까지 읽는다 — 잠금 보유자가 실제로
/// flock을 쥐었다는 관측 신호다. EOF(잡는 자체가 죽음)는 거짓.
nonisolated func _lockHolderAnnounced(_ handle: FileHandle) -> Bool {
  var buffer = Data()
  while true {
    let chunk = handle.availableData
    if chunk.isEmpty { return false }
    buffer.append(chunk)
    if let text = String(data: buffer, encoding: .utf8),
      text.split(separator: "\n").contains("held") { return true }
  }
}

// ─── 실제 다리의 공용 재료(S2A-2 회복) — 합성 저장소·승인된 자식 보호·같은-해석기 드라이버 ──

/// 실제 다리 시험의 합성 무대 — 공개 파이썬 소스와 바이트 동일 엔진 스크립트만
/// 실제 저장소에서 온다. 경로에 공백이 있고, 임시 뿌리를 미리 resolve해
/// 파이촌의 resolve() 철자와 어긋나지 않게 한다(보호 뿌리 비교는 어휘적이다).
struct BridgeSandbox {
  let root: URL
  let repo: URL
  let realPython: URL
  let home: URL
  let tmp: URL
  let guardBoot: URL
  let standin: URL
  let engineScript: URL
  let driver: URL
  let proofDir: URL
  let resultDir: URL
}

/// #filePath에서 실제 저장소 뿌리까지 — 네 번 지운다(Tests·FinalCutAgentCoreTests·
/// macos·파일 이름). 세 번이면 macos에 남는다.
nonisolated func _bridgeRepoRoot() -> URL {
  URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()  // EngineRequestGateTests.swift
    .deletingLastPathComponent()  // FinalCutAgentCoreTests
    .deletingLastPathComponent()  // Tests
    .deletingLastPathComponent()  // macos → 저장소 루트
}

/// sh 안전 인용 — 값 하나를 한 인자로 묶는다. JSON의 따옴표·경로의 공백이
/// 흩어지지 않게 한다(이중 인용은 JSON의 `"`에 부서진다).
nonisolated func _shQuoted(_ value: String) -> String {
  "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

/// 과정 하나에 정해진 시간 상한 — 무한 waitUntilExit가 시험을 잡아매지 못하게
/// 한다. 종료 관찰은 **run 앞에 단 terminationHandler**가 한다(이 보조자가 시작을
/// 소유한다): 호출자를 세마포로 막아 둔 채 전역 큐에서 waitUntilExit를 돌리던
/// 옛 모양이 타임아웃에 걸린 것은 종료 전달과의 경합으로 보는 **추론**이다(유일하게
/// 증명된 원인은 아니다 — 증상만 실측됐다). 넘으면 SIGTERM → 그래도 살아 있으면
/// SIGKILL로 못 박는다. `terminationStatus`는 **isRunning이 거짓일 때만** 읽는다 —
/// 달리는 과정에서 읽으면 상태 보고 대신 예외가 터진다. 회계가 안 닫히면 상태를
/// 모른 채 불확실로 보고한다(추측하지 않는다).
nonisolated func _runBounded(_ process: Process, seconds: Double, label: String) throws {
  let finished = DispatchSemaphore(value: 0)
  process.terminationHandler = { _ in finished.signal() }  // run보다 먼저 단다
  try process.run()
  let expired = finished.wait(timeout: .now() + seconds) == .timedOut
  if expired {
    if process.isRunning { process.terminate() }
    if finished.wait(timeout: .now() + 2) == .timedOut, process.isRunning {
      kill(process.processIdentifier, SIGKILL)  // 딱딱한 상한 — 무시 못 하는 신호
      _ = finished.wait(timeout: .now() + 5)
    }
  }
  let completed = !process.isRunning
  // 회계가 닫혔을 때만 상태를 읽는다 — 안 닫혔면 상태 자체를 모른다.
  let status: Int32? = completed ? process.terminationStatus : nil
  process.terminationHandler = nil
  if expired || !completed {
    let detail = completed
      ? " — 종료시켰다(마지막 상태 \(status.map(String.init) ?? "?"))"
      : " — SIGKILL 후에도 완결 회계가 안 닫힌다(상태 미확정)"
    throw NSError(
      domain: "bridge-bounded", code: 1,
      userInfo: [NSLocalizedDescriptionKey: "\(label)이 \(Int(seconds))초 안에 끝나지 않았다" + detail])
  }
}

/// 비동기 본문 하나에 시간 상한 — 실행기 호출마다 붙는다.
///
/// **이것은 딱딱한 상한이 아니다**라는 것이 이 문서의 요점이다: NSUserUnixTask
/// 실행은 XPC 완료를 기다리는 취소 불가 연속(continuation) 안에서 기다린다 —
/// 시한이 지나도 구조화된 작업 그룹은 그 본문이 돌아올 때까지 기다린다. 시한
/// 초과는 시험을 실패로 **표시**할 뿐 매달린 실행을 강제로 끊지 못한다. 소유
/// 자식의 딱딱한 종료 상한은 `_runBounded`(SIGTERM→SIGKILL)이고, 시험 과정
/// 전체의 딱딱한 상한은 인수에서 쓰는 **외부 감시자**(수용 검증이 그렇게
/// 돌았다)다. XPC 실행기 자식에는 부모 쪽 프로세스 손잡이가 없다 — 그 이상의
/// 강제 종료는 플랫폼 경계로 남는다.
nonisolated func _withDeadline<T: Sendable>(
  seconds: Double, label: String, _ body: @escaping @Sendable () async throws -> T
) async throws -> T {
  try await withThrowingTaskGroup(of: T.self) { group in
    group.addTask { try await body() }
    group.addTask {
      try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
      throw NSError(
        domain: "bridge-deadline", code: 1,
        userInfo: [NSLocalizedDescriptionKey: "\(label)이 \(Int(seconds))초를 넘었다"])
    }
    guard let result = try await group.next() else {
      throw NSError(domain: "bridge-deadline", code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "\(label) 결과가 없다"])
    }
    group.cancelAll()
    return result
  }
}

/// 시험 소유 보호 드라이버 — payload와 **같은 해석기**에서 보호를 확인하고
/// 복사된 실제 엔진을 runpy로 돈다(사전 부트스트랩 프로세스가 아니다). 확인이
/// payload보다 먼저고, 어떤 확인이 실패해도 payload는 뜨지 않는다(exit 125).
/// 소비 경계(선택)는 실제 운반 적용 → 실제 `env.require_api_key` → 실제
/// `get_transcriber` 공장 → 가짜 `openai.OpenAI`까지만 가고, 기대값은 sha256
/// 다이제스트로만 받아 **결과는 참/거짓뿐** 남긴다 — 원문 합성 키는 시험 재료
/// 어디에도 쓰지 않는다(지속 표면 검사가 드라이버까지 훑는다).
let _bridgeDriverSource = #"""
import hashlib
import importlib.util
import json
import os
import runpy
import sys

try:
    import sitecustomize  # 명시적 적재 — 자동 sitecustomize만 믿지 않는다
except BaseException:
    print("bridge-driver: sitecustomize", file=sys.stderr)
    sys.exit(125)  # 보호가 없다 — payload 없이 끝난다


def _fail(stage):
    # 고정 단계 이름만 말한다 — 환경·경로·값은 실지 않는다(진단은 단계로 충분하다).
    print("bridge-driver: " + stage, file=sys.stderr)
    sys.exit(125)


def _record(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


refused = False
try:
    open(os.environ["VLOGSRT_TEST_GUARD_STANDIN"], "rb")
except sitecustomize.GuardedEnvAccess:
    refused = True
import socket

socket_refused = False
try:
    socket.socket()
except RuntimeError:
    socket_refused = True
if not refused:
    _fail("standin-refusal")
if not socket_refused:
    _fail("socket-refusal")

if os.getcwd() != os.environ["VLOGSRT_TEST_EXPECTED_CWD"]:
    _fail("cwd")
import vlogsrt

if os.path.realpath(vlogsrt.__file__) != os.path.realpath(
    os.environ["VLOGSRT_TEST_EXPECTED_INIT"]
):
    _fail("provenance")

engine_args = sys.argv[1:]
if engine_args and engine_args[0] == "--bridge-self-check":
    _record(
        os.path.join(os.environ["VLOGSRT_TEST_PROOF_DIR"], "self-check.jsonl"),
        {
            "refused": refused,
            "socket_refused": socket_refused,
            "cwd": os.getcwd(),
            "vlogsrt_file": os.path.realpath(vlogsrt.__file__),
        },
    )
    sys.exit(0)

job_id = engine_args[2] if len(engine_args) > 2 else ""
_record(
    os.path.join(os.environ["VLOGSRT_TEST_PROOF_DIR"], job_id + ".jsonl"),
    {
        "refused": refused,
        "socket_refused": socket_refused,
        "cwd": os.getcwd(),
        "vlogsrt_file": os.path.realpath(vlogsrt.__file__),
        "job_id": job_id,
    },
)


def _digest(value):
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


# V2 소비 경계 — 실제 STT 판독·공장과 실제 비전 자격증명·공장을 지나간다.
# 역할마다 독립 판정: STT가 비면 판독이 거부되고, 비전이 비면 자격증명 문이
# 거부된다 — 한쪽 부재가 다른 쪽 증명을 묻히지 않는다(S2A-2 단위2). 실제 운반
# 적용 뒤에만 오고, 두 공장 모두 **생산 공장**을 그대로 부른다(openai.OpenAI
# 생성자만 가짜로 갈아끼운다 — 네트워크 없음). 기대값은 sha256 다이제스트로만
# 받아 결과는 참/거짓뿐 남긴다.
def _consume_role_credentials(env, transcribe, entry):
    import openai
    from vlogsrt.project import commands as project_commands
    from vlogsrt.project.config import ProjectConfig

    record = {
        "stt_ok": False,
        "stt_refused": False,
        "vision_refused": False,
        "vision_key_ok": False,
        "vision_url_ok": False,
    }
    stt_seen = {}
    vision_seen = {}

    class _FakeSTTOpenAI:
        def __init__(self, **kwargs):
            stt_seen.update(kwargs)

    class _FakeVisionOpenAI:
        def __init__(self, **kwargs):
            vision_seen.update(kwargs)

    openai.OpenAI = _FakeSTTOpenAI
    try:
        env.require_api_key(force=True)
    except RuntimeError:
        record["stt_refused"] = True
    if not record["stt_refused"]:
        transcribe.get_transcriber(online=True)._client()
        record["stt_ok"] = _digest(stt_seen.get("api_key", "")) == entry.get("stt")
    try:
        project_commands._vision_credentials(ProjectConfig())
    except RuntimeError:
        record["vision_refused"] = True
    if not record["vision_refused"]:
        # 생산 공장 경계: 실제 _vision_client → 실제 create_vision_client →
        # (가짜) openai.OpenAI 생성자 — 공장 자체를 흉내 내지 않는다.
        openai.OpenAI = _FakeVisionOpenAI
        project_commands._vision_client(ProjectConfig())
        record["vision_key_ok"] = (
            _digest(vision_seen.get("api_key", "")) == entry.get("vision_key")
        )
        record["vision_url_ok"] = (
            _digest(vision_seen.get("base_url", "")) == entry.get("vision_base_url")
        )
    return record


digests_raw = os.environ.get("VLOGSRT_TEST_CONSUMER_DIGESTS", "")
if digests_raw:
    spec = importlib.util.spec_from_file_location("vlogsrt_engine_bridge", engine_args[0])
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    digests = json.loads(digests_raw)

    def _run_operation_synthetic(operation, job_dir, workspace):
        # 실제 `_run_owned`이 이미 운반을 적용한 뒤에만 온다 — 여기서 운반을
        # 흉내 내면 생산 순서를 증명하지 못한다. 실제 자격 증명 판독과 실제 SDK
        # 공장만 부르고, 공장이 받은 값을 다이제스트로 판정한다.
        from vlogsrt import env, transcribe

        entry = digests.get(job_id)
        if isinstance(entry, dict):
            record = _consume_role_credentials(env, transcribe, entry)
            _record(
                os.path.join(os.environ["VLOGSRT_TEST_RESULT_DIR"], job_id + ".jsonl"),
                record,
            )
            return 0

        reader_refused = False
        try:
            env.require_api_key(force=True)
        except RuntimeError:
            reader_refused = True
        consumer_ok = False
        if not reader_refused:
            seen = {}

            class _FakeOpenAI:
                def __init__(self, **kwargs):
                    seen.update(kwargs)

            import openai

            openai.OpenAI = _FakeOpenAI
            transcribe.get_transcriber(online=True)._client()
            captured = seen.get("api_key")
            consumer_ok = (
                captured is not None
                and hashlib.sha256(captured.encode()).hexdigest() == digests.get(job_id)
            )
        _record(
            os.path.join(os.environ["VLOGSRT_TEST_RESULT_DIR"], job_id + ".jsonl"),
            {"consumer_ok": consumer_ok, "reader_refused": reader_refused},
        )
        return 0

    engine._run_operation = _run_operation_synthetic
    sys.argv = engine_args
    # 패치가 붙은 **같은 모듈 객체**의 진짜 main을 부른다 — runpy는 새 이름공간을
    # 만들어 패치가 묻는다. main→_dispatch→소유권→봉투→_run_owned(실재)→(여기)다.
    raise SystemExit(engine.main(engine_args[1:]))

sys.argv = engine_args
runpy.run_path(engine_args[0], run_name="__main__")
"""#

/// 경로의 물리 철자 — realpath(3)로 연다. URL.resolvingSymlinksInPath는
/// /var→/private/var 굳은링크를 넘지 않는데 자식의 os.getcwd()/realpath는
/// 물리 경로로 답한다: 기대값과 자식이 **같은 철자**를 쓰게 하는 유일한 길이다.
nonisolated func _physicalRoot(_ url: URL) -> URL {
  let physical = url.path.withCString { cPath -> String? in
    guard let resolved = realpath(cPath, nil) else { return nil }
    defer { free(resolved) }
    return String(cString: resolved)
  }
  return URL(fileURLWithPath: physical ?? url.path)
}

/// 공개 재료만 담은 합성 저장소를 만든다 — 심볼링크 원본은 거절한다(복사가
/// 곧 증명이다). dotenv는 시험이 직접 쓴 합성물이다(보호 뿌리 안에 산다).
nonisolated func _makeBridgeSandbox(label: String, dotenv: String) throws -> BridgeSandbox {
  let repo = _bridgeRepoRoot()
  let realPython = repo.appendingPathComponent(".venv/bin/python")
  try FileManager.default.attributesOfItem(atPath: realPython.path)  // venv 없으면 즉시 실패
  let engineScript = repo.appendingPathComponent("macos/scripts/vlogsrt-engine.py")
  let packageRoot = repo.appendingPathComponent("vlogsrt", isDirectory: true)

  // 임시 뿌리를 만든 뒤 물리 철자로 못 박는다 — 자식의 getcwd/realpath와
  // 어긋나는 철자(/var vs /private/var)가 cwd·수입 출처 증명을 부수지 않게.
  let rawRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent("\(label) \(UUID().uuidString)", isDirectory: true)
  try FileManager.default.createDirectory(at: rawRoot, withIntermediateDirectories: true)
  let root = _physicalRoot(rawRoot)

  let syntheticRepo = root.appendingPathComponent("synthetic repo", isDirectory: true)
  let syntheticScripts = syntheticRepo.appendingPathComponent("macos/scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: syntheticScripts, withIntermediateDirectories: true)
  try Data("[project]\nname='synthetic-bridge'\n".utf8)
    .write(to: syntheticRepo.appendingPathComponent("pyproject.toml"))
  // 합성 dotenv — 시험이 직접 쓴 오프라인 설정(실제 파일을 읽지 않는다).
  try Data(dotenv.utf8).write(to: syntheticRepo.appendingPathComponent(".env"))
  // 엔진 스크립트는 바이트 그대로 — 복사본이 흘러가는 일이 없게 못 박는다.
  let copiedEngine = syntheticScripts.appendingPathComponent("vlogsrt-engine.py")
  try FileManager.default.copyItem(at: engineScript, to: copiedEngine)
  guard try Data(contentsOf: engineScript) == Data(contentsOf: copiedEngine) else {
    throw NSError(domain: "bridge-sandbox", code: 1,
                  userInfo: [NSLocalizedDescriptionKey: "엔진 스크립트가 바이트 동일하지 않다"])
  }
  // 공개 파이썬 소스만 명시적으로 복사한다 — PACKAGE_ROOT가 임시 안에 갇힌다.
  let syntheticPackage = syntheticRepo.appendingPathComponent("vlogsrt", isDirectory: true)
  let items = FileManager.default.enumerator(
    at: packageRoot, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey]
  )?.allObjects.compactMap { $0 as? URL } ?? []
  var copiedSources = 0
  for url in items {
    let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    guard url.pathExtension == "py", values.isRegularFile == true else { continue }
    guard values.isSymbolicLink != true else {
      throw NSError(domain: "bridge-sandbox", code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "공개 원본이 심볼링크다 — 복사를 거절한다: \(url.path)"])
    }
    let relative = String(url.path.dropFirst(packageRoot.path.count + 1))
    let target = syntheticPackage.appendingPathComponent(relative)
    try FileManager.default.createDirectory(
      at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
    try FileManager.default.copyItem(at: url, to: target)
    copiedSources += 1
  }
  guard copiedSources > 20 else {
    throw NSError(domain: "bridge-sandbox", code: 3,
                  userInfo: [NSLocalizedDescriptionKey: "패키지 통째가 와야 cli가 돈다 — 빠뜨림은 설정 실패다"])
  }
  let packageInit = syntheticPackage.appendingPathComponent("__init__.py")
  guard FileManager.default.fileExists(atPath: packageInit.path) else {
    throw NSError(domain: "bridge-sandbox", code: 4,
                  userInfo: [NSLocalizedDescriptionKey: "vlogsrt/__init__.py이 없다 — 수입 출처 증명이 불가능하다"])
  }
  // 합성 HOME·TMPDIR — 자식 환경은 아래 심의 허용목록만 쓴다.
  let home = root.appendingPathComponent("synthetic home", isDirectory: true)
  let tmp = root.appendingPathComponent("synthetic tmp", isDirectory: true)
  try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
  // stand-in — 허용 뿌리 밖의 보호 철자. 내용은 합성이고 어디에도 기록하지 않는다.
  let standinDir = root.appendingPathComponent("outside roots", isDirectory: true)
  try FileManager.default.createDirectory(at: standinDir, withIntermediateDirectories: true)
  let standin = standinDir.appendingPathComponent(".env")
  try Data("SYNTHETIC_STANDIN=1\n".utf8).write(to: standin)
  // 드라이버 — 같은-해석기 보호 확인과 (선택) 소비 경계를 산다.
  let driver = root.appendingPathComponent("bridge driver.py")
  try Data(_bridgeDriverSource.utf8).write(to: driver)
  // 호출별 증명·결과 디렉터리 — 드라이버가 job 단위 파일로 남긴다(심은 불변이다).
  let proofDir = root.appendingPathComponent("invocation proofs", isDirectory: true)
  let resultDir = root.appendingPathComponent("consumer results", isDirectory: true)
  try FileManager.default.createDirectory(at: proofDir, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: resultDir, withIntermediateDirectories: true)
  return BridgeSandbox(
    root: root, repo: syntheticRepo, realPython: realPython, home: home, tmp: tmp,
    guardBoot: root.appendingPathComponent("guard boot", isDirectory: true),
    standin: standin, engineScript: copiedEngine, driver: driver,
    proofDir: proofDir, resultDir: resultDir)
}

/// 승인된 자식 보호(tests/_dotenv_guard.py)를 **그 정의 그대로** 내보낸다 —
/// Swift 쪽에서 보호 문면을 다시 짜지 않는다(정의는 하나뿐이다). 이 과정도
/// 문자 그대로의 허용목록 환경에서 돈다(주변 상속이 없다).
nonisolated func _emitChildGuard(
  sandbox: BridgeSandbox, breakBootstrap: Bool = false
) throws -> [String: String] {
  let guardScript = _bridgeRepoRoot().appendingPathComponent("tests/_dotenv_guard.py")
  let guardProof = sandbox.root.appendingPathComponent("guard-proof.jsonl")
  let emit = Process()
  emit.executableURL = sandbox.realPython
  emit.arguments = [
    guardScript.path, "emit-guard",
    "--root", sandbox.guardBoot.path,
    "--allow", sandbox.repo.path,
    "--standin", sandbox.standin.path,
    "--proof", guardProof.path,
  ] + (breakBootstrap ? ["--break-bootstrap"] : [])
  emit.currentDirectoryURL = sandbox.root
  emit.environment = [
    "PATH": "/usr/bin:/bin", "HOME": sandbox.home.path, "TMPDIR": sandbox.tmp.path,
  ]
  let out = Pipe()
  emit.standardOutput = out
  emit.standardError = Pipe()
  try _runBounded(emit, seconds: 30, label: "자식 보호 내보내기")  // 시작도 보조자가 소유한다
  let text = String(
    data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
  guard emit.terminationStatus == 0,
    let data = text.data(using: .utf8),
    let env = try JSONSerialization.jsonObject(with: data) as? [String: String],
    !env.isEmpty
  else {
    throw NSError(domain: "bridge-guard", code: 1,
                  userInfo: [NSLocalizedDescriptionKey: "자식 보호 내보내기 실패(exit \(emit.terminationStatus))"])
  }
  return env
}

/// 시험 소유 해석기 심 — env -i로 **문자 그대로의** 허용목록 환경만 남기고
/// 드라이버를 띄운다(부모 상속·사전 복사 없음). 심 자체는 시험마다 **한 번**
/// 쓰고 안 바뀐다(불변) — 호출별 상태는 드라이버가 job 단위 증명·결과 파일로
/// 남긴다. 소비 기대값은 sha256 다이제스트 사전만 간다(원문 합성 키는 없다).
/// `omitGuard`는 '보호 없음' 음성 시험용 — PYTHONPATH에서 보호 뿌리를 뺀다.
/// 남은 경계: zsh -f여도 시스템 /etc/zshenv은 항상 읽힌다 — 밝혀두되 들여다보지 않는다.
nonisolated func _writeGuardedShim(
  sandbox: BridgeSandbox, credentials: [String: String] = [:],
  consumerDigests: [String: String] = [:],
  roleConsumerDigests: [String: [String: String]] = [:],
  breakBootstrap: Bool = false,
  omitGuard: Bool = false
) throws -> [String: String] {
  let guardEnv = try _emitChildGuard(sandbox: sandbox, breakBootstrap: breakBootstrap)
  var literals: [String] = [
    "PATH=/usr/bin:/bin",
    "HOME=\(sandbox.home.path)",
    "TMPDIR=\(sandbox.tmp.path)",
    "PYTHONPATH=\(omitGuard ? sandbox.repo.path : "\(sandbox.guardBoot.path):\(sandbox.repo.path)")",
    "VLOGSRT_OFFLINE=1",
    "VLOGSRT_TEST_GUARD_STANDIN=\(sandbox.standin.path)",
    "VLOGSRT_TEST_EXPECTED_CWD=\(sandbox.repo.path)",
    "VLOGSRT_TEST_EXPECTED_INIT=\(sandbox.repo.path)/vlogsrt/__init__.py",
    "VLOGSRT_TEST_PROOF_DIR=\(sandbox.proofDir.path)",
    "VLOGSRT_TEST_RESULT_DIR=\(sandbox.resultDir.path)",
  ]
  if !omitGuard {
    literals.append("VLOGSRT_TEST_GUARD_ROOTS=\(guardEnv["VLOGSRT_TEST_GUARD_ROOTS"] ?? "")")
    literals.append("VLOGSRT_TEST_GUARD_PROOF=\(sandbox.root.appendingPathComponent("guard-proof.jsonl").path)")
  }
  if let injectedBreak = guardEnv["VLOGSRT_TEST_GUARD_BREAK"] {
    literals.append("VLOGSRT_TEST_GUARD_BREAK=\(injectedBreak)")
  }
  // 소비 기대값 — V1은 job→다이제스트 문자열, V2는 job→역할별 다이제스트 사전.
  // **사전 그대로** 부호화한다(문자열로 두 번 싸지 않는다): 드라이버의 json.loads가
  // V2 항목을 사전으로 읽어야 역할 분기가 켜진다.
  var mergedExpectations: [String: Any] = [:]
  for (key, value) in consumerDigests { mergedExpectations[key] = value }
  for (key, value) in roleConsumerDigests { mergedExpectations[key] = value }
  if !mergedExpectations.isEmpty,
    let data = try? JSONSerialization.data(withJSONObject: mergedExpectations),
    let digestJSON = String(data: data, encoding: .utf8) {
    literals.append("VLOGSRT_TEST_CONSUMER_DIGESTS=\(digestJSON)")
  }
  literals += credentials.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
  // env -i 세그먼트 — sh 안전 인용으로 값 하나가 인자 하나로 간다(JSON 따옴표·
  // 공백 경로 포함). 절대 경로 /usr/bin/env만 쓴다.
  let envSegments = literals.map { _shQuoted($0) }.joined(separator: " \\\n  ")
  let script = """
    #!/bin/sh
    # 시험 소유 해석기 심(S2A-2 회복) — env -i: 문자 그대로의 환경만. 상속 없음.
    # 드라이버가 같은 해석기에서 보호를 확인하고 payload(실제 엔진)를 돈다.
    exec /usr/bin/env -i \\
      \(envSegments) \\
      \(_shQuoted(sandbox.realPython.path)) \(_shQuoted(sandbox.driver.path)) "$@"
    """
  let shimDirectory = sandbox.repo.appendingPathComponent(".venv/bin", isDirectory: true)
  try FileManager.default.createDirectory(at: shimDirectory, withIntermediateDirectories: true)
  let shim = shimDirectory.appendingPathComponent("python")
  try Data(script.utf8).write(to: shim)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: shim.path)
  return guardEnv
}

/// 진짜 설치 스크립트를 합성 저장소로 돌려 래퍼를 만든다 — 공백이 든 경로에.
/// 설치는 심이 이미 있을 때 돈다(래퍼는 합성 .venv/bin/python을 실행 가능으로
/// 검사한다). 이 과정도 문자 그대로의 환경에서만 돈다.
nonisolated func _installDevelopmentWrapper(sandbox: BridgeSandbox) throws -> URL {
  let installer = _bridgeRepoRoot()
    .appendingPathComponent("macos/scripts/install-development-engine.sh")
  let scriptsDirectory = sandbox.root.appendingPathComponent("engine scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: scriptsDirectory, withIntermediateDirectories: true)
  let install = Process()
  install.executableURL = installer
  install.arguments = [sandbox.repo.path, scriptsDirectory.path]
  install.currentDirectoryURL = sandbox.root
  install.environment = [
    "PATH": "/usr/bin:/bin", "HOME": sandbox.home.path, "TMPDIR": sandbox.tmp.path,
  ]
  let installDiagnosis = Pipe()
  install.standardOutput = installDiagnosis
  install.standardError = installDiagnosis
  try _runBounded(install, seconds: 30, label: "래퍼 설치")
  let installLog = String(
    data: installDiagnosis.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
  guard install.terminationStatus == 0 else {
    throw NSError(domain: "bridge-install", code: Int(install.terminationStatus),
                  userInfo: [NSLocalizedDescriptionKey: "설치 실패(\(install.terminationStatus)): \(installLog)"])
  }
  let wrapper = scriptsDirectory.appendingPathComponent("vlogsrt-engine")
  guard FileManager.default.isExecutableFile(atPath: wrapper.path) else {
    throw NSError(domain: "bridge-install", code: 1,
                  userInfo: [NSLocalizedDescriptionKey: "래퍼가 실행 가능하지 않다"])
  }
  return wrapper
}

/// 보호 자체가 125로 물러난 단계 — 증명 파일의 마지막 'bootstrap failed' 기록에서
/// 고정 단계 이름만 읽는다(환경·값은 실지 않는다). 없으면 빈 문면.
nonisolated func guardAbortStage(_ sandbox: BridgeSandbox) -> String {
  let proof = sandbox.root.appendingPathComponent("guard-proof.jsonl")
  guard let raw = try? String(contentsOf: proof, encoding: .utf8) else { return "" }
  for line in raw.split(separator: "\n").reversed() {
    guard let data = String(line).data(using: .utf8),
      let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      record["bootstrap"] as? String == "failed",
      let stage = record["stage"] as? String
    else { continue }
    return "(보호 단계: \(stage))"
  }
  return "(보호 기록에 실패 단계가 없다)"
}

/// payload 없는 사전확인(canary) — payload를 띄우기 전에 드라이버의 자기검증
/// 경로(보호 거부·cwd·수입 출처)가 0으로 끝나는지를 본다. 문자 그대로의 환경.
/// `expectedFailure`가 있으면 그 **원본 종료 코드**로 죽어야 한다(무관한 실패 X).
nonisolated func _runShimCanary(
  sandbox: BridgeSandbox, expectFailure expectedExit: Int? = nil
) throws {
  let canary = Process()
  canary.executableURL = sandbox.repo.appendingPathComponent(".venv/bin/python")
  canary.arguments = ["--bridge-self-check"]
  canary.currentDirectoryURL = sandbox.repo
  canary.environment = [
    "PATH": "/usr/bin:/bin", "HOME": sandbox.home.path, "TMPDIR": sandbox.tmp.path,
  ]
  let diagnose = Pipe()
  canary.standardError = diagnose
  try _runBounded(canary, seconds: 30, label: "사전확인")
  let log = String(
    data: diagnose.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
  if let expectedExit {
    guard Int(canary.terminationStatus) == expectedExit else {
      throw NSError(domain: "bridge-canary", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "사전확인이 기대한 \(expectedExit)가 아니라 \(canary.terminationStatus)로 끝났다: \(log) \(guardAbortStage(sandbox))"])
    }
    return
  }
  guard canary.terminationStatus == 0 else {
    throw NSError(domain: "bridge-canary", code: Int(canary.terminationStatus),
                  userInfo: [NSLocalizedDescriptionKey: "사전확인 실패(exit \(canary.terminationStatus)): \(log) \(guardAbortStage(sandbox))"])
  }
  // 자기검증 증명이 실제로 남았는지 — cwd·수입 출처까지 같은 해석기가 기록했다.
  let selfCheck = sandbox.proofDir.appendingPathComponent("self-check.jsonl")
  let raw = try String(contentsOf: selfCheck, encoding: .utf8)
  guard let line = raw.split(separator: "\n").last,
    let data = String(line).data(using: .utf8),
    let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
    record["refused"] as? Bool == true,
    record["socket_refused"] as? Bool == true,
    record["cwd"] as? String == sandbox.repo.path,
    record["vlogsrt_file"] as? String
      == sandbox.repo.appendingPathComponent("vlogsrt/__init__.py").path
  else {
    throw NSError(domain: "bridge-canary", code: 2,
                  userInfo: [NSLocalizedDescriptionKey: "자기검증 증명이 온전하지 않다"])
  }
}

/// 이 호출의 증명(job 단위 파일)을 못 박는다 — 보호 거부·네트워크 거부가 참이고
/// cwd는 기대한 자리(기본: 합성 저장소, 번들은 create가 정한 engineRoot), 수입
/// 출처는 복사본 **__init__.py 그 자체**다. 증명이 없거나 깨지면 시험이 실패한다.
nonisolated func _assertJobProof(
  sandbox: BridgeSandbox, jobID: String, minimumRecords: Int, expectedCWD: URL? = nil
) throws {
  let proof = sandbox.proofDir.appendingPathComponent("\(jobID).jsonl")
  let raw = try String(contentsOf: proof, encoding: .utf8)
  let records: [[String: Any]] = raw.split(separator: "\n").compactMap { line in
    guard let data = String(line).data(using: .utf8) else { return nil }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
  }
  guard records.count >= minimumRecords else {
    throw NSError(domain: "bridge-proof", code: 1,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 증명 기록이 \(records.count)건 — 최소 \(minimumRecords)건이어야 한다(보호가 실제로 붙었나)"])
  }
  guard let last = records.last else { return }
  guard last["refused"] as? Bool == true, last["socket_refused"] as? Bool == true else {
    throw NSError(domain: "bridge-proof", code: 2,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 증명의 보호 거부가 참이 아니다"])
  }
  guard last["cwd"] as? String == (expectedCWD ?? sandbox.repo).path else {
    throw NSError(domain: "bridge-proof", code: 3,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 증명의 cwd가 기대한 자리가 아니다"])
  }
  guard last["vlogsrt_file"] as? String
    == sandbox.repo.appendingPathComponent("vlogsrt/__init__.py").path else {
    throw NSError(domain: "bridge-proof", code: 4,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 증명의 수입 출처가 복사본 __init__.py가 아니다"])
  }
  guard last["job_id"] as? String == jobID else {
    throw NSError(domain: "bridge-proof", code: 5,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 증명의 job 표식이 어긋난다"])
  }
}

/// 소비 경계의 결과(job 단위 파일) — 참/거짓만 읽는다. 원문 키는 어디에도 없다.
nonisolated func _assertConsumerResult(
  sandbox: BridgeSandbox, jobID: String, consumerOK: Bool, readerRefused: Bool
) throws {
  let result = sandbox.resultDir.appendingPathComponent("\(jobID).jsonl")
  let raw = try String(contentsOf: result, encoding: .utf8)
  guard let line = raw.split(separator: "\n").last,
    let data = String(line).data(using: .utf8),
    let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
  else {
    throw NSError(domain: "bridge-consumer", code: 1,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 소비 결과 기록이 없다"])
  }
  guard record["consumer_ok"] as? Bool == consumerOK,
    record["reader_refused"] as? Bool == readerRefused else {
    throw NSError(domain: "bridge-consumer", code: 2,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 소비 결과가 기대와 다르다: \(record)"])
  }
}

/// V2 소비 경계의 결과(job 단위 파일) — 역할마다 참/거짓만 읽는다(S2A-2 단위2).
nonisolated func _assertRoleConsumerResult(
  sandbox: BridgeSandbox, jobID: String,
  sttOK: Bool, sttRefused: Bool,
  visionRefused: Bool, visionKeyOK: Bool, visionURLOK: Bool
) throws {
  let result = sandbox.resultDir.appendingPathComponent("\(jobID).jsonl")
  let raw = try String(contentsOf: result, encoding: .utf8)
  guard let line = raw.split(separator: "\n").last,
    let data = String(line).data(using: .utf8),
    let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
  else {
    throw NSError(domain: "bridge-consumer", code: 1,
                  userInfo: [NSLocalizedDescriptionKey: "\(jobID) 역할 소비 결과 기록이 없다"])
  }
  let expected: [String: Bool] = [
    "stt_ok": sttOK, "stt_refused": sttRefused, "vision_refused": visionRefused,
    "vision_key_ok": visionKeyOK, "vision_url_ok": visionURLOK,
  ]
  for (key, value) in expected {
    guard record[key] as? Bool == value else {
      throw NSError(domain: "bridge-consumer", code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "\(jobID) 역할 소비 결과가 기대와 다르다(\(key)): \(record)"])
    }
  }
}

/// 실제 다리: **설치 스크립트가 만든 진짜 개발 래퍼** → 실제 파이썬 → 실제
/// `vlogsrt-engine.py`. NSUserUnixTask → 래퍼의 넷째 인자 전달 → 소유된 상태
/// 기록 → 실행기 판독까지 통째로 잇는다(손으로 쓴 래퍼이 아니다).
///
/// 보호 분리(S2A-2 회복): 이 다리도 승인된 자식 보호 아래 둔다 — 합성 저장소,
/// env -i 허용목록 심(불변), 드라이버의 같은-해석기 확인, payload 없는 사전확인,
/// job 단위 증명 파일. 나중에 전체 시험이 이 구멍을 다시 열 수 없다.
/// 성공(0)·안쪽 실패(1)·실제 잡힌 잠금 앞의 중복(3)을 본다.
@Test func launcherReadsTheRealPythonEntrypointStatusRecord() async throws {
  let sandbox = try _makeBridgeSandbox(
    label: "S2A-1 bridge", dotenv: "VLOGSRT_OFFLINE=1\n")
  defer { try? FileManager.default.removeItem(at: sandbox.root) }
  // 심은 시험마다 한 번 쓰고 불변이다 — 호출별 상태는 드라이버의 job 단위 파일.
  _ = try _writeGuardedShim(sandbox: sandbox)
  try _runShimCanary(sandbox: sandbox)
  let wrapper = try _installDevelopmentWrapper(sandbox: sandbox)

  // 성공 job — 최소 오프라인 프로젝트(receive·inspect까지 0으로 끝난다).
  let goodJob = sandbox.root.appendingPathComponent("job good", isDirectory: true)
  try FileManager.default.createDirectory(at: goodJob, withIntermediateDirectories: true)
  let media = sandbox.root.appendingPathComponent("clip.mov")
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
    executableURL: wrapper, arguments: ["analyze", "job good", goodJob.path])
  #expect(try await launcher.run(succeeding) == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job good", minimumRecords: 1)
  #expect(
    FileManager.default.fileExists(
      atPath: goodJob.appendingPathComponent("analysis-estimate.jsonl").path))

  // 실패 job — input이 없어 receive가 실패한다. 엔진은 안쪽 1로 끝나고, NSUserUnixTask는
  // 그 1을 오류로만 전한다 — 소유된 기록이 1을 되찾는다.
  let badJob = sandbox.root.appendingPathComponent("job bad", isDirectory: true)
  try FileManager.default.createDirectory(at: badJob, withIntermediateDirectories: true)
  let failing = DevelopmentEngineInvocation(
    executableURL: wrapper, arguments: ["analyze", "job bad", badJob.path])
  #expect(try await launcher.run(failing) == 1)
  try _assertJobProof(sandbox: sandbox, jobID: "job bad", minimumRecords: 1)

  // 실제 잡힌 잠금 앞의 중복 — 합성 보유자가 goodJob의 flock을 쥔 사이에 같은 job을
  // 돌리면 안쪽 3이 기록으로 나가고 실행기가 3을 돌려준다(보유자는 표준 라이브러리만
  // 쓴다 — dotenv 로더에 닿지 않는다).
  let holderScript = sandbox.root.appendingPathComponent("hold-lock.py")
  try Data(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(120)
    """.utf8
  ).write(to: holderScript)
  let holder = Process()
  holder.executableURL = sandbox.realPython
  holder.arguments = [holderScript.path, goodJob.appendingPathComponent(".vlogsrt-engine.lock").path]
  let holderOut = Pipe()
  holder.standardOutput = holderOut
  holder.standardError = Pipe()
  try holder.run()
  defer {
    holder.terminate()
    holder.waitUntilExit()
  }
  #expect(_lockHolderAnnounced(holderOut.fileHandleForReading))
  let duplicate = DevelopmentEngineInvocation(
    executableURL: wrapper, arguments: ["analyze", "job good", goodJob.path])
  #expect(try await launcher.run(duplicate) == 3)
  // 같은 job의 증명은 두 번째 호출에서도 새로 남았다 — 꼬리 기록이 이 호출의 것이다.
  try _assertJobProof(sandbox: sandbox, jobID: "job good", minimumRecords: 2)
}

/// **실제 다리(자격 증명 편, S2A-2)** — 진짜 설치 래퍼 → 실제 파이썬 → 바이트 동일
/// 엔진 스크립트를 NSUserUnixTask로 건너, stdin 봉투가 마커와 함께 흘러가는지를
/// 본다. 소비는 **자식 안에서** 증명한다(드라이버의 같은-해석기 경계): 실제 운반
/// 적용 → 실제 `env.require_api_key` → 실제 `get_transcriber` 공장 → 가짜
/// `openai.OpenAI`. 기대값은 sha256 다이제스트로만 자식에 가고 결과는 참/거짓뿐 —
/// 낡은 상속 키·낡은 dotenv 키가 봉투에 못 박히는 장면이 여기서 실측된다.
/// 핵심 판별 하나: 마커만 있고 봉투를 붙이지 않으면 stdin은 기본(/dev/null)에
/// 묶인다 — 엔진이 마커를 읽었다면 빈 stdin에 실패 닫힘(2)이고, 래퍼가 마커를
/// 흘려보내지 않았다면 낡은 3-인자 계약으로 0이다.
@Test func credentialEnvelopeCrossesTheRealDevelopmentBridge() async throws {
  // 낡은 키는 두 표면에 심어둔다 — dotenv와 환경(운반자 이름 포함). job 표식은
  // 안전한 조각만 쓰고(create의 계약), 공백은 **경로**에 남긴다.
  let sandbox = try _makeBridgeSandbox(
    label: "S2A-2 credential bridge",
    dotenv: "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-key\nVLOGSRT_ENGINE_API_KEY=stale-dotenv-carrier\n")
  defer { try? FileManager.default.removeItem(at: sandbox.root) }
  let submitted = "synthetic-bridge-submitted-key"
  let otherKey = "synthetic-bridge-other-key"
  let staleCredentials = [
    "OPENAI_API_KEY": "stale-env-key",
    "VLOGSRT_ENGINE_API_KEY": "stale-env-carrier",
  ]
  _ = try _writeGuardedShim(
    sandbox: sandbox, credentials: staleCredentials,
    consumerDigests: [
      "job-keyed": _digestOf(submitted),
      "job-c-key": _digestOf(otherKey),
    ])
  try _runShimCanary(sandbox: sandbox)
  let wrapperParent = try _installDevelopmentWrapper(sandbox: sandbox)
    .deletingLastPathComponent()

  func makeJob(_ name: String) throws -> URL {
    let job = sandbox.root.appendingPathComponent(name, isDirectory: true)
    try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
    return job
  }

  let launcher = DevelopmentEngineLauncher()

  // (1) 키 봉투 — 봉투의 키가 상속·dotenv의 낡은 키를 이기고 SDK 공장까지 간다.
  // 실제 순서가 증명된다: 실제 _run_owned이 운반을 적용한 뒤 소비 경계가 온다.
  let keyedJob = try makeJob("job keyed dir")
  let keyed = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "job-keyed",
    externalJob: keyedJob,
    applicationScriptsDirectory: wrapperParent,
    apiKey: submitted
  )
  #expect(!keyed.arguments.joined().contains(submitted))
  #expect(
    try await _withDeadline(seconds: 60, label: "키 봉투 실행") {
      try await launcher.run(keyed)
    } == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job-keyed", minimumRecords: 1)
  try _assertConsumerResult(
    sandbox: sandbox, jobID: "job-keyed", consumerOK: true, readerRefused: false)
  try _assertNoDurableSecret(under: sandbox.root, secret: submitted)

  // (2) '키 없음' 봉투 — 빈 운반은 낡은 상속·dotenv 키를 부활시키지 않는다:
  // 자격 증명 판독이 거부된다(공장은 아예 세워지지 않는다).
  let emptyJob = try makeJob("job empty dir")
  let empty = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "job-empty",
    externalJob: emptyJob,
    applicationScriptsDirectory: wrapperParent,
    apiKey: nil
  )
  #expect(empty.credentialEnvelope == .explicitNoKey)
  #expect(
    try await _withDeadline(seconds: 60, label: "빈 봉투 실행") {
      try await launcher.run(empty)
    } == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job-empty", minimumRecords: 1)
  try _assertConsumerResult(
    sandbox: sandbox, jobID: "job-empty", consumerOK: false, readerRefused: true)

  // (3) 마커만 있고 봉투 없음 — stdin은 기본(/dev/null)이다. 엔진이 마커를
  // 읽었다면 빈 입력에 실패 닫힘(2)이고, 안 읽었다면 낡은 계약의 0이다.
  // exit 2가 마커·stdin 판독의 실측 증명이다(소비 경계는 오기 전에 죽는다).
  let markerOnlyJob = try makeJob("job marker only dir")
  let markerOnly = DevelopmentEngineInvocation(
    executableURL: wrapperParent.appendingPathComponent("vlogsrt-engine"),
    arguments: [
      "analyze", "job-marker-only", markerOnlyJob.path,
      DevelopmentEngineInvocation.carrierMarkerArgument,
    ])
  #expect(
    try await _withDeadline(seconds: 60, label: "마커만 실행") {
      try await launcher.run(markerOnly)
    } == 2)
  try _assertJobProof(sandbox: sandbox, jobID: "job-marker-only", minimumRecords: 1)
  #expect(
    !FileManager.default.fileExists(
      atPath: sandbox.resultDir.appendingPathComponent("job-marker-only.jsonl").path))

  // (4) 서로 다른 봉투의 동시 실행 — 불변 심 하나, job별 증명·결과 파일은
  // 독립이다. 파이프가 섞이면 한쪽이 남의 봉투나 깨진 봉투를 읽어 2로 죽는다.
  // 여기서는 값 수준까지 본다: 각 자식이 제 봉투의 키만 봤다(다이제스트 판정).
  let concurrentKeyedJob = try makeJob("job c key dir")
  let concurrentEmptyJob = try makeJob("job c empty dir")
  let concurrentKeyed = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "job-c-key",
    externalJob: concurrentKeyedJob,
    applicationScriptsDirectory: wrapperParent,
    apiKey: otherKey
  )
  let concurrentEmpty = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "job-c-empty",
    externalJob: concurrentEmptyJob,
    applicationScriptsDirectory: wrapperParent,
    apiKey: nil
  )
  async let firstRun = _withDeadline(seconds: 60, label: "동시 실행(키)") {
    try await launcher.run(concurrentKeyed)
  }
  async let secondRun = _withDeadline(seconds: 60, label: "동시 실행(빈)") {
    try await launcher.run(concurrentEmpty)
  }
  #expect(try await firstRun == 0)
  #expect(try await secondRun == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job-c-key", minimumRecords: 1)
  try _assertJobProof(sandbox: sandbox, jobID: "job-c-empty", minimumRecords: 1)
  try _assertConsumerResult(
    sandbox: sandbox, jobID: "job-c-key", consumerOK: true, readerRefused: false)
  try _assertConsumerResult(
    sandbox: sandbox, jobID: "job-c-empty", consumerOK: false, readerRefused: true)
  try _assertNoDurableSecret(under: sandbox.root, secret: otherKey)
}

/// **보호 음성 시험** — 보호가 부서졌거나 없으면 자식은 payload **앞에서** 죽는다.
/// 원본 종료 코드 125를 직접 보고(사전확인·실제 엔진 인자 직통 실행 둘 다),
/// 실행기 호에는 오류로 나가며, payload가 돌았다면 남았을 흔적(증명·소비 결과·
/// 소유권 잠금)이 하나도 없어야 한다. 각 음성은 제 무대(심 변형)에서 돈다.
@Test func unguardedChildrenNeverReachThePayload() async throws {
  for variant in ["broken", "missing"] {
    let sandbox = try _makeBridgeSandbox(
      label: "S2A-2 guard \(variant)",
      dotenv: "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-key\n")
    defer { try? FileManager.default.removeItem(at: sandbox.root) }
    _ = try _writeGuardedShim(
      sandbox: sandbox,
      credentials: ["OPENAI_API_KEY": "stale-env-key"],
      consumerDigests: ["job-guarded": _digestOf("synthetic-never-delivered-key")],
      breakBootstrap: variant == "broken",
      omitGuard: variant == "missing")
    // 사전확인부터 실패해야 한다 — 성공하면 보호 없이 payload가 뜰 수 있다.
    try _runShimCanary(sandbox: sandbox, expectFailure: 125)
    let wrapperParent = try _installDevelopmentWrapper(sandbox: sandbox)
      .deletingLastPathComponent()
    let job = sandbox.root.appendingPathComponent("job guarded dir", isDirectory: true)
    try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)

    // 직통 실행 — 심을 실제 엔진 인자로 부르면 원본 종료 코드 125가 그대로 나온다.
    let direct = Process()
    direct.executableURL = sandbox.repo.appendingPathComponent(".venv/bin/python")
    direct.arguments = [sandbox.engineScript.path, "analyze", "job-guarded", job.path]
    direct.currentDirectoryURL = sandbox.repo
    direct.environment = [
      "PATH": "/usr/bin:/bin", "HOME": sandbox.home.path, "TMPDIR": sandbox.tmp.path,
    ]
    direct.standardError = Pipe()
    try _runBounded(direct, seconds: 30, label: "\(variant) 직통 자식")
    #expect(direct.terminationStatus == 125, "\(variant): 원본 종료 코드는 125여야 한다")

    let invocation = try DevelopmentEngineInvocation.create(
      operation: .analyze,
      jobID: "job-guarded",
      externalJob: job,
      applicationScriptsDirectory: wrapperParent,
      apiKey: "synthetic-never-delivered-key"
    )
    do {
      _ = try await _withDeadline(seconds: 60, label: "\(variant) 실행기 호") {
        try await DevelopmentEngineLauncher().run(invocation)
      }
      Issue.record("\(variant): 보호 없는 자식은 오류여야 한다")
    } catch {
      // 기대 — 125로 payload 앞에서 죽는다(상태 기록 없음 오류).
    }
    // payload가 돌지 않았다는 흔적 증명 — 세 표면 모두 비어 있어야 한다.
    #expect(
      !FileManager.default.fileExists(
        atPath: sandbox.proofDir.appendingPathComponent("job-guarded.jsonl").path),
      "\(variant): 증명 기록이 남았다 — 드라이버가 payload 판을 지났다")
    #expect(
      !FileManager.default.fileExists(
        atPath: sandbox.resultDir.appendingPathComponent("job-guarded.jsonl").path),
      "\(variant): 소비 결과가 남았다 — payload가 돌았다")
    #expect(
      !FileManager.default.fileExists(
        atPath: job.appendingPathComponent(".vlogsrt-engine.lock").path),
      "\(variant): 소유권 잠금이 남았다 — 엔진 본체가 돌았다")
  }
}

/// **번들 소비 경계** — 실제 BundledEngineLauncher(Process, 환경 통째 교체 계약)로
/// 자식을 띄워 환경 운반자(VLOGSRT_ENGINE_API_KEY)가 실제 SDK 공장까지 가는지를
/// 본다. 자격 증명 환경·인자·작업 디렉터리는 **실제 create API가 파생**한 것에서
/// 시작한다(운반자 시맨틱을 손으로 다시 짜지 않는다) — 시험 전용 문맥(보호·드라이버
/// 경로·다이제스트)만 문자 그대로 얹는다. 빈 운반자도 명시적으로 가야 낡은 상속
/// 키가 부활하지 않는다.
@Test func bundledCarrierReachesTheLaunchedConsumer() async throws {
  let sandbox = try _makeBridgeSandbox(
    label: "S2A-2 bundled consumer",
    dotenv: "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-key\n")
  defer { try? FileManager.default.removeItem(at: sandbox.root) }
  let carried = "synthetic-bundled-carried-key"
  let digests = ["job-bundled-keyed": _digestOf(carried)]
  let digestJSON = String(
    data: try JSONSerialization.data(withJSONObject: digests), encoding: .utf8)!
  let guardEnv = try _writeGuardedShim(sandbox: sandbox, consumerDigests: digests)
  try _runShimCanary(sandbox: sandbox)

  /// 실제 create로 파생한 호출에 시험 전용 문맥만 얹는다 — 실행기는 실물 그대로.
  func derivedInvocation(carrier: String, jobID: String, job: URL) throws -> BundledEngineInvocation {
    // engineRoot는 create의 작업 디렉터리·PATH의 뿌리 — 실제로 있어야 실행기가
    // 그 안에서 자식을 띄운다(없으면 Process 시작 자체가 실패한다).
    let engineRoot = sandbox.root.appendingPathComponent("synthetic engine", isDirectory: true)
    try FileManager.default.createDirectory(at: engineRoot, withIntermediateDirectories: true)
    let real = try BundledEngineInvocation.create(
      operation: .analyze,
      jobID: jobID,
      jobDirectory: job,
      engineRoot: engineRoot,
      apiKey: carrier.isEmpty ? nil : carrier
    )
    // 파생 근거를 못 박는다 — 운반자는 create가 준 값 그대로다(빈 포함).
    #expect(real.environment[BundledEngineInvocation.apiKeyEnvironmentName] == carrier)
    var environment = real.environment
    environment["HOME"] = sandbox.home.path
    environment["TMPDIR"] = sandbox.tmp.path
    environment["PYTHONPATH"] = "\(sandbox.guardBoot.path):\(sandbox.repo.path)"
    environment["VLOGSRT_OFFLINE"] = "1"
    environment["VLOGSRT_TEST_GUARD_ROOTS"] = guardEnv["VLOGSRT_TEST_GUARD_ROOTS"] ?? ""
    environment["VLOGSRT_TEST_GUARD_STANDIN"] = sandbox.standin.path
    environment["VLOGSRT_TEST_GUARD_PROOF"] =
      sandbox.root.appendingPathComponent("guard-proof.jsonl").path
    environment["VLOGSRT_TEST_EXPECTED_CWD"] = real.currentDirectory.path
    environment["VLOGSRT_TEST_EXPECTED_INIT"] =
      sandbox.repo.appendingPathComponent("vlogsrt/__init__.py").path
    environment["VLOGSRT_TEST_PROOF_DIR"] = sandbox.proofDir.path
    environment["VLOGSRT_TEST_RESULT_DIR"] = sandbox.resultDir.path
    environment["VLOGSRT_TEST_CONSUMER_DIGESTS"] = digestJSON
    return BundledEngineInvocation(
      executableURL: sandbox.realPython,
      arguments: [
        sandbox.driver.path, sandbox.engineScript.path, "analyze", jobID,
        job.standardizedFileURL.path,
      ],
      currentDirectory: real.currentDirectory,
      environment: environment)
  }

  let keyedJob = sandbox.root.appendingPathComponent("job bundled keyed dir", isDirectory: true)
  try FileManager.default.createDirectory(at: keyedJob, withIntermediateDirectories: true)
  let keyedInvocation = try derivedInvocation(
    carrier: carried, jobID: "job-bundled-keyed", job: keyedJob)
  let keyedStatus = try await _withDeadline(seconds: 60, label: "번들 소비(키)") {
    try await Task.detached { try BundledEngineLauncher().run(keyedInvocation) }.value
  }
  #expect(keyedStatus == 0)
  // 번들 자식의 자리는 create가 정한 engineRoot다 — 저장소가 아니다.
  try _assertJobProof(
    sandbox: sandbox, jobID: "job-bundled-keyed", minimumRecords: 1,
    expectedCWD: keyedInvocation.currentDirectory)
  try _assertConsumerResult(
    sandbox: sandbox, jobID: "job-bundled-keyed", consumerOK: true, readerRefused: false)

  // 빈 운반자 — create(apiKey: nil)의 명시적 빈 값이 낡은 상속·dotenv 키를 누른다.
  let emptyJob = sandbox.root.appendingPathComponent("job bundled empty dir", isDirectory: true)
  try FileManager.default.createDirectory(at: emptyJob, withIntermediateDirectories: true)
  let emptyInvocation = try derivedInvocation(
    carrier: "", jobID: "job-bundled-empty", job: emptyJob)
  let emptyStatus = try await _withDeadline(seconds: 60, label: "번들 소비(빈)") {
    try await Task.detached { try BundledEngineLauncher().run(emptyInvocation) }.value
  }
  #expect(emptyStatus == 0)
  try _assertJobProof(
    sandbox: sandbox, jobID: "job-bundled-empty", minimumRecords: 1,
    expectedCWD: emptyInvocation.currentDirectory)
  try _assertConsumerResult(
    sandbox: sandbox, jobID: "job-bundled-empty", consumerOK: false, readerRefused: true)
  try _assertNoDurableSecret(under: sandbox.root, secret: carried)
}

/// 지속 표면의 비밀 부재 증명 — 뿌리 아래 모든 파일(합성 재료 포함)을 **바이트로**
/// 훑어 제출값이 한 번도 기록되지 않았음을 못 박는다. 텍스트 판독은 __pycache__
/// 같은 이진 파일에서 깨진다 — 바이트 탐색은 텍스트·이진을 가리지 않아 더 세다.
/// 심·래퍼·sitecustomize에 있는 것은 시험이 심어둔 **낡은** 합성 값뿐이라는 것이
/// 이 검사의 요점이므로 예외 없이 전부 훑는다.
nonisolated func _assertNoDurableSecret(under root: URL, secret: String) throws {
  let needle = Data(secret.utf8)
  let items = FileManager.default.enumerator(
    at: root, includingPropertiesForKeys: [.isRegularFileKey]
  )?.allObjects.compactMap { $0 as? URL } ?? []
  for url in items {
    let values = try url.resourceValues(forKeys: [.isRegularFileKey])
    guard values.isRegularFile == true else { continue }
    let bytes = try Data(contentsOf: url)
    if bytes.range(of: needle) != nil {
      throw NSError(
        domain: "bridge-secret", code: 1,
        userInfo: [NSLocalizedDescriptionKey: "지속 표면에서 제출값이 발견됐다: \(url.path)"])
    }
  }
}

/// **실제 다리(역할 자격 증명 편, S2A-2 단위2)** — 진짜 설치 래퍼 → 실제 파이썬
/// → 바이트 동일 엔진 스크립트를 NSUserUnixTask로 건너, V2 stdin 봉투(세 역할
/// JSON)가 실제 운반 적용을 지나 **두 공장 모두**(STT `get_transcriber`·비전
/// `_vision_credentials`→`create_vision_client`)에 제 값을 내리는지를 본다.
/// 기대값은 역할별 sha256 다이제스트 사전만 자식에 가고 결과는 참/거짓뿐이다.
/// 역할마다 독립 부재도 여기서 실측한다 — 비전만 비워도 STT 공장은 살고,
/// STT만 비워도 비전 자격증명은 산다.
@Test func roleCredentialEnvelopeCrossesTheRealDevelopmentBridge() async throws {
  let sandbox = try _makeBridgeSandbox(
    label: "S2A-2 role credential bridge",
    dotenv: "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-stt\n"
      + "GOOGLE_API_KEY=stale-dotenv-vision\nGEMINI_BASE_URL=https://stale.example/v1\n")
  defer { try? FileManager.default.removeItem(at: sandbox.root) }
  let sttKey = "synthetic-role-bridge-stt-key"
  let visionKey = "synthetic-role-bridge-vision-key"
  let endpoint = "https://synthetic-vision.example/v1"

  func roleEntry(stt: String, vision: String, url: String) -> [String: String] {
    [
      "stt": _digestOf(stt),
      "vision_key": _digestOf(vision),
      "vision_base_url": _digestOf(url),
    ]
  }
  _ = try _writeGuardedShim(
    sandbox: sandbox,
    credentials: [
      "OPENAI_API_KEY": "stale-env-stt",
      "GOOGLE_API_KEY": "stale-env-vision",
      "GEMINI_BASE_URL": "https://stale-env.example/v1",
    ],
    roleConsumerDigests: [
      "job-roles-full": roleEntry(stt: sttKey, vision: visionKey, url: endpoint),
      "job-roles-no-vision": roleEntry(stt: sttKey, vision: "", url: ""),
      "job-roles-no-stt": roleEntry(stt: "", vision: visionKey, url: endpoint),
    ])
  try _runShimCanary(sandbox: sandbox)
  let wrapperParent = try _installDevelopmentWrapper(sandbox: sandbox)
    .deletingLastPathComponent()
  let launcher = DevelopmentEngineLauncher()

  func makeJob(_ name: String) throws -> URL {
    let job = sandbox.root.appendingPathComponent(name, isDirectory: true)
    try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
    return job
  }

  // (1) 세 역할 전부 — 상속·dotenv의 낡은 값을 이기고 두 공장이 제 값을 본다.
  let full = try DevelopmentEngineInvocation.createRoles(
    operation: .analyze,
    jobID: "job-roles-full",
    externalJob: try makeJob("job roles full dir"),
    applicationScriptsDirectory: wrapperParent,
    credentials: EngineRoleCredentials(
      sttKey: sttKey, visionKey: visionKey, visionBaseURL: endpoint)
  )
  #expect(try await _withDeadline(seconds: 60, label: "역할 봉투 실행") {
    try await launcher.run(full)
  } == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job-roles-full", minimumRecords: 1)
  try _assertRoleConsumerResult(
    sandbox: sandbox, jobID: "job-roles-full",
    sttOK: true, sttRefused: false, visionRefused: false, visionKeyOK: true, visionURLOK: true)
  try _assertNoDurableSecret(under: sandbox.root, secret: sttKey)
  try _assertNoDurableSecret(under: sandbox.root, secret: visionKey)

  // (2) 비전만 부재 — STT 공장은 살아 있고(독립 부재) 비전 자격증명 문은 닫는다.
  let noVision = try DevelopmentEngineInvocation.createRoles(
    operation: .analyze,
    jobID: "job-roles-no-vision",
    externalJob: try makeJob("job roles no vision dir"),
    applicationScriptsDirectory: wrapperParent,
    credentials: EngineRoleCredentials(sttKey: sttKey, visionKey: "", visionBaseURL: "")
  )
  #expect(try await _withDeadline(seconds: 60, label: "역할 봉투(비전 없음)") {
    try await launcher.run(noVision)
  } == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job-roles-no-vision", minimumRecords: 1)
  try _assertRoleConsumerResult(
    sandbox: sandbox, jobID: "job-roles-no-vision",
    sttOK: true, sttRefused: false, visionRefused: true, visionKeyOK: false, visionURLOK: false)

  // (3) STT만 부재 — 반대 방향: STT 판독은 거부되고 비전 두 값은 여전히 산다.
  let noSTT = try DevelopmentEngineInvocation.createRoles(
    operation: .analyze,
    jobID: "job-roles-no-stt",
    externalJob: try makeJob("job roles no stt dir"),
    applicationScriptsDirectory: wrapperParent,
    credentials: EngineRoleCredentials(sttKey: "", visionKey: visionKey, visionBaseURL: endpoint)
  )
  #expect(try await _withDeadline(seconds: 60, label: "역할 봉투(STT 없음)") {
    try await launcher.run(noSTT)
  } == 0)
  try _assertJobProof(sandbox: sandbox, jobID: "job-roles-no-stt", minimumRecords: 1)
  try _assertRoleConsumerResult(
    sandbox: sandbox, jobID: "job-roles-no-stt",
    sttOK: false, sttRefused: true, visionRefused: false, visionKeyOK: true, visionURLOK: true)
  try _assertNoDurableSecret(under: sandbox.root, secret: visionKey)
}

/// **번들 역할 소비 경계(S2A-2 단위2)** — 실제 BundledEngineLauncher가 createRoles
/// 가 파생한 환경(V2 사적 운반자 한 통, V1 운반자 없음)으로 자식을 띄워 두 공장
/// 모두가 제 값을 보는지를 본다. 빈 역할도 명시적으로 가서 낡은 상속 키가
/// 부활하지 않는다. 호출 환경은 실제 create가 준 것에서 시험 문맥만 얹는다.
@Test func bundledRoleCarrierReachesTheLaunchedConsumer() async throws {
  let sandbox = try _makeBridgeSandbox(
    label: "S2A-2 bundled role consumer",
    dotenv: "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-stt\n"
      + "GOOGLE_API_KEY=stale-dotenv-vision\nGEMINI_BASE_URL=https://stale.example/v1\n")
  defer { try? FileManager.default.removeItem(at: sandbox.root) }
  let sttKey = "synthetic-bundled-role-stt-key"
  let visionKey = "synthetic-bundled-role-vision-key"
  let endpoint = "https://synthetic-vision.example/v1"

  func roleEntry(stt: String, vision: String, url: String) -> [String: String] {
    [
      "stt": _digestOf(stt),
      "vision_key": _digestOf(vision),
      "vision_base_url": _digestOf(url),
    ]
  }
  let digestMap = [
    "job-bundled-roles": roleEntry(stt: sttKey, vision: visionKey, url: endpoint),
    "job-bundled-roles-empty": roleEntry(stt: "", vision: "", url: ""),
  ]
  // 부호화는 심의 병합 부호화와 같은 모양 — job→사전 그대로(JSON 문자열 이중 포장 없음).
  var digestPayload: [String: Any] = [:]
  for (key, value) in digestMap { digestPayload[key] = value }
  let digestJSON = String(
    data: try JSONSerialization.data(withJSONObject: digestPayload), encoding: .utf8)!
  let guardEnv = try _writeGuardedShim(
    sandbox: sandbox, roleConsumerDigests: digestMap)
  try _runShimCanary(sandbox: sandbox)

  func derivedRoleInvocation(
    credentials: EngineRoleCredentials, jobID: String, job: URL
  ) throws -> BundledEngineInvocation {
    let engineRoot = sandbox.root.appendingPathComponent("synthetic engine", isDirectory: true)
    try FileManager.default.createDirectory(at: engineRoot, withIntermediateDirectories: true)
    let real = try BundledEngineInvocation.createRoles(
      operation: .analyze,
      jobID: jobID,
      jobDirectory: job,
      engineRoot: engineRoot,
      credentials: credentials
    )
    // 파생 근거를 못 박는다 — V2 운반자만 실렸고 V1 운반자는 없다.
    let expectedPayload = try credentials.encodedPayload()
    #expect(
      real.environment[BundledEngineInvocation.roleCarrierEnvironmentName]
        == String(decoding: expectedPayload, as: UTF8.self))
    #expect(real.environment[BundledEngineInvocation.apiKeyEnvironmentName] == nil)
    var environment = real.environment
    environment["HOME"] = sandbox.home.path
    environment["TMPDIR"] = sandbox.tmp.path
    environment["PYTHONPATH"] = "\(sandbox.guardBoot.path):\(sandbox.repo.path)"
    environment["VLOGSRT_OFFLINE"] = "1"
    environment["VLOGSRT_TEST_GUARD_ROOTS"] = guardEnv["VLOGSRT_TEST_GUARD_ROOTS"] ?? ""
    environment["VLOGSRT_TEST_GUARD_STANDIN"] = sandbox.standin.path
    environment["VLOGSRT_TEST_GUARD_PROOF"] =
      sandbox.root.appendingPathComponent("guard-proof.jsonl").path
    environment["VLOGSRT_TEST_EXPECTED_CWD"] = real.currentDirectory.path
    environment["VLOGSRT_TEST_EXPECTED_INIT"] =
      sandbox.repo.appendingPathComponent("vlogsrt/__init__.py").path
    environment["VLOGSRT_TEST_PROOF_DIR"] = sandbox.proofDir.path
    environment["VLOGSRT_TEST_RESULT_DIR"] = sandbox.resultDir.path
    environment["VLOGSRT_TEST_CONSUMER_DIGESTS"] = digestJSON
    return BundledEngineInvocation(
      executableURL: sandbox.realPython,
      arguments: [
        sandbox.driver.path, sandbox.engineScript.path, "analyze", jobID,
        job.standardizedFileURL.path,
      ],
      currentDirectory: real.currentDirectory,
      environment: environment)
  }

  // 세 역할 전부 — 두 공장 모두 제 값을 본다.
  let keyedJob = sandbox.root.appendingPathComponent("job bundled roles dir", isDirectory: true)
  try FileManager.default.createDirectory(at: keyedJob, withIntermediateDirectories: true)
  let keyed = try derivedRoleInvocation(
    credentials: EngineRoleCredentials(
      sttKey: sttKey, visionKey: visionKey, visionBaseURL: endpoint),
    jobID: "job-bundled-roles", job: keyedJob)
  let keyedStatus = try await _withDeadline(seconds: 60, label: "번들 역할 소비(키)") {
    try await Task.detached { try BundledEngineLauncher().run(keyed) }.value
  }
  #expect(keyedStatus == 0)
  try _assertJobProof(
    sandbox: sandbox, jobID: "job-bundled-roles", minimumRecords: 1,
    expectedCWD: keyed.currentDirectory)
  try _assertRoleConsumerResult(
    sandbox: sandbox, jobID: "job-bundled-roles",
    sttOK: true, sttRefused: false, visionRefused: false, visionKeyOK: true, visionURLOK: true)
  try _assertNoDurableSecret(under: sandbox.root, secret: sttKey)
  try _assertNoDurableSecret(under: sandbox.root, secret: visionKey)

  // 전부 비어도 V2 운반자는 간다 — 두 자격증명 문 모두 거부(명시적 부재).
  let emptyJob = sandbox.root
    .appendingPathComponent("job bundled roles empty dir", isDirectory: true)
  try FileManager.default.createDirectory(at: emptyJob, withIntermediateDirectories: true)
  let empty = try derivedRoleInvocation(
    credentials: EngineRoleCredentials(),
    jobID: "job-bundled-roles-empty", job: emptyJob)
  let emptyStatus = try await _withDeadline(seconds: 60, label: "번들 역할 소비(빈)") {
    try await Task.detached { try BundledEngineLauncher().run(empty) }.value
  }
  #expect(emptyStatus == 0)
  try _assertJobProof(
    sandbox: sandbox, jobID: "job-bundled-roles-empty", minimumRecords: 1,
    expectedCWD: empty.currentDirectory)
  try _assertRoleConsumerResult(
    sandbox: sandbox, jobID: "job-bundled-roles-empty",
    sttOK: false, sttRefused: true, visionRefused: true, visionKeyOK: false, visionURLOK: false)
}
