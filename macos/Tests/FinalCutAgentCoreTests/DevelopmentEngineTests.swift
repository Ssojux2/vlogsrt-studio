import Foundation
import Testing

@testable import FinalCutAgentCore

@Test func developmentEngineInvocationUsesApplicationScriptWithoutSecrets() throws {
  let scripts = URL(fileURLWithPath: "/Users/test/Library/Application Scripts/com.ssojux2.vlogsrt")
  let job = URL(fileURLWithPath: "/Users/test/Movies/VlogJobs/analysis-42")
  let invocation = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "analysis-42",
    externalJob: job,
    applicationScriptsDirectory: scripts
  )
  #expect(invocation.executableURL == scripts.appendingPathComponent("vlogsrt-engine"))
  // 앱 호출은 항상 봉투 마커로 끝난다 — 키가 없어도 '키 없음' 봉투가 간다(S2A-2).
  // argv에 실리는 것은 마커뿐, 값은 오지 않는다.
  #expect(
    invocation.arguments == [
      "analyze", "analysis-42", "/Users/test/Movies/VlogJobs/analysis-42",
      DevelopmentEngineInvocation.carrierMarkerArgument,
    ])
  #expect(invocation.credentialEnvelope == .explicitNoKey)
  #expect(!invocation.arguments.joined().contains("OPENAI_API_KEY"))

  let keyed = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "analysis-42",
    externalJob: job,
    applicationScriptsDirectory: scripts,
    apiKey: "synthetic-submitted-key"
  )
  #expect(keyed.credentialEnvelope == .key("synthetic-submitted-key"))
  #expect(!keyed.arguments.joined().contains("synthetic-submitted-key"))

  // 낡은 계약 — 봉투 없는 호출도 여전히 조립된다(직접 호출·3/4-인자 계약).
  let legacy = DevelopmentEngineInvocation(
    executableURL: scripts.appendingPathComponent("vlogsrt-engine"),
    arguments: ["analyze", "analysis-42", job.path])
  #expect(legacy.credentialEnvelope == nil)

  #expect(throws: WorkspaceError.self) {
    try DevelopmentEngineInvocation.create(
      operation: .roughcut,
      jobID: "../escape",
      externalJob: URL(fileURLWithPath: "/tmp/escape"),
      applicationScriptsDirectory: scripts
    )
  }
}

@Test func developmentJobStagerCopiesInputsOutAndOnlyAllowlistedResultsBack() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let appGroup = root.appendingPathComponent("group", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  let workspace = AppGroupWorkspace(rootURL: appGroup)
  let groupJob = try workspace.createJob(id: "analysis-1")
  try Data("xml".utf8).write(to: groupJob.appendingPathComponent("input.fcpxml"))
  try Data("selection".utf8).write(
    to: groupJob.appendingPathComponent("project-selection.jsonl")
  )

  let staged = try DevelopmentJobStager.stage(
    jobID: "analysis-1",
    appGroupJob: groupJob,
    externalRoot: external
  )
  #expect(try Data(contentsOf: staged.appendingPathComponent("input.fcpxml")) == Data("xml".utf8))
  try Data("candidate".utf8).write(to: staged.appendingPathComponent("candidates.jsonl"))
  try Data("secret".utf8).write(to: staged.appendingPathComponent(".env"))
  try DevelopmentJobStager.syncResults(from: staged, to: groupJob)
  #expect(FileManager.default.fileExists(atPath: groupJob.appendingPathComponent("candidates.jsonl").path))
  #expect(!FileManager.default.fileExists(atPath: groupJob.appendingPathComponent(".env").path))
}

@Test func developmentJobStagerRejectsSymlinkedRootsAndDirectoryResults() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let groupJob = root.appendingPathComponent("group-job", isDirectory: true)
  let external = root.appendingPathComponent("external", isDirectory: true)
  let escaped = root.appendingPathComponent("escaped", isDirectory: true)
  try FileManager.default.createDirectory(at: groupJob, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(at: escaped, withIntermediateDirectories: true)
  try Data("xml".utf8).write(to: groupJob.appendingPathComponent("input.fcpxml"))
  try FileManager.default.createSymbolicLink(
    at: external.appendingPathComponent("Jobs"),
    withDestinationURL: escaped
  )
  #expect(throws: DevelopmentEngineError.self) {
    try DevelopmentJobStager.stage(
      jobID: "analysis-1",
      appGroupJob: groupJob,
      externalRoot: external
    )
  }

  let cleanExternal = root.appendingPathComponent("clean", isDirectory: true)
  let staged = try DevelopmentJobStager.stage(
    jobID: "analysis-2",
    appGroupJob: groupJob,
    externalRoot: cleanExternal
  )
  let fakeResult = staged.appendingPathComponent("candidates.jsonl", isDirectory: true)
  try FileManager.default.createDirectory(at: fakeResult, withIntermediateDirectories: true)
  try Data("secret".utf8).write(to: fakeResult.appendingPathComponent(".env"))
  #expect(throws: DevelopmentEngineError.self) {
    try DevelopmentJobStager.syncResults(from: staged, to: groupJob)
  }
}

// ─────────────────────────────────────────────────────────────
//  사적 stdin 봉투의 수송 계약 — prefill 생애·주입 쓰기·크기 정의(S2A-2 회복)
// ─────────────────────────────────────────────────────────────

/// 상한의 정의는 하나다 — Swift 상수와 엔진 스크립트의 상수가 같은 값인지를
/// 실제 파일에서 읽어 못 박는다(문서 합의가 아니라 증명).
@Test func carrierSizeLimitHasOneDefinitionAcrossLanguages() throws {
  let repo = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()  // DevelopmentEngineTests.swift
    .deletingLastPathComponent()  // FinalCutAgentCoreTests
    .deletingLastPathComponent()  // Tests
    .deletingLastPathComponent()  // macos → 저장소 루트
  let script = try String(
    contentsOf: repo.appendingPathComponent("macos/scripts/vlogsrt-engine.py"),
    encoding: .utf8)
  let pattern = try NSRegularExpression(pattern: #"_CARRIER_MAX_BYTES = (\d+)"#)
  let matches = pattern.matches(
    in: script, range: NSRange(script.startIndex..., in: script))
  #expect(matches.count == 1)
  let range = try #require(matches.first?.range(at: 1))
  let pythonLimit = try #require(
    Int(script[Range(range, in: script) ?? script.startIndex..<script.startIndex]))
  #expect(pythonLimit == EngineCredentialEnvelope.maximumByteCount)
}

/// 부분 쓰기·EINTR로 찢겨 나오는 커널을 흉내 낸 주입 쓰기로 **실물 반복문**을
/// 돌린다 — 프레임 전체가 순서대로 파이프에 들어가고, 읽기 끝에서 바이트 그대로
/// 돌아온다.
@Test func carrierPrefillSurvivesPartialWritesAndEINTR() throws {
  let pipe = Pipe()
  let ends = CarrierPipeEnds(pipe: pipe)
  try ends.makeWriteEndNonBlocking()
  let frame = try EngineCredentialEnvelope.key("synthetic-partial-write-key").wire

  // 한 번에 1바이트씩만 쓰는 커널 — 첫 호출은 EINTR으로 갈라진다.
  let state = PrefillScriptState()
  try ends.queueFrame(frame, rawWrite: state.oneByteAtATimeWithInitialEINTR)

  ends.closeWriteOnce()
  let read = pipe.fileHandleForReading.readDataToEndOfFile()
  #expect(read == frame)
  #expect(state.interruptsHit == 1)
  ends.closeReadOnce()
}

/// EAGAIN·0바이트·그 밖의 오류는 기다림 없이 그 자리에서 실패한다 — 용량·속도
/// 추정이 없다는 것이 계약이다.
@Test func carrierPrefillFailsImmediatelyOnEAGAINZeroAndErrors() throws {
  let frame = try EngineCredentialEnvelope.key("synthetic-failing-key").wire

  for behavior in [
    PrefillScriptState.eagainOnce, PrefillScriptState.zeroWriteForever,
    PrefillScriptState.genericErrorOnce,
  ] {
    let pipe = Pipe()
    let ends = CarrierPipeEnds(pipe: pipe)
    try ends.makeWriteEndNonBlocking()
    do {
      try ends.queueFrame(frame, rawWrite: behavior)
      Issue.record("실패해야 하는 주입 쓰기가 성공했다")
    } catch {
      // 기대 — EAGAIN은 POSIXError로, 0바이트는 통로 오류로, 주입 오류는 그대로.
    }
    ends.closeRemainingOnce()
  }
}

/// prefill이 실패하면 실행기는 아예 띄지 않는다 — 마커 파일로 실행 흔적을
/// 관찰한다(실행기가 돌았다면 흔적이 남는다).
@Test func launcherSkipsTheExecutorWhenPrefillFails() async throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  defer { try? FileManager.default.removeItem(at: root) }
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  let marker = root.appendingPathComponent("executor-ran")
  let script = root.appendingPathComponent("engine.sh")
  try Data(
    """
    #!/bin/sh
    touch "\(marker.path)"
    exit 0
    """.utf8
  ).write(to: script)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)

  let job = root.appendingPathComponent("job", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
  let invocation = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "job",
    externalJob: job,
    applicationScriptsDirectory: root,
    apiKey: "synthetic-prefail-key"
  )
  let launcher = DevelopmentEngineLauncher(
    rawWrite: PrefillScriptState.genericErrorOnce)
  do {
    _ = try await launcher.run(invocation)
    Issue.record("prefill 실패는 오류여야 한다")
  } catch {
    // 기대 — 주입한 오류가 그대로 나오고 실행기는 띄지 않았다.
  }
  #expect(!FileManager.default.fileExists(atPath: marker.path))
  // 실행기가 돌았다면 잠금 파일이 생긴다 — 없어야 한다.
  #expect(
    !FileManager.default.fileExists(
      atPath: job.appendingPathComponent(".vlogsrt-engine.lock").path))
}

/// **작은 수송 시험(게이트 분리 승인용)** — Python·설정·환경 없이, 봉투 바이트가
/// 실제 NSUserUnixTask stdin 경로로 건너가는지만 본다. 자식은 시험이 직접 쓴
/// `zsh -f` 실행파일이다: 마커 검증(합성 고정값)과 소유 상태 기록만 하고,
/// 그 밖에 아무것도 읽지·탐색하지·출력하지 않는다. 읽기 fd의 수명·상태 기약·
/// 실행기는 전부 실물이고 흉내 낸 것이 없다. 모든 경로는 문자 그대로 임시,
/// jobID는 안전한 조각이며 시험 자체와 셸 읽기(read -t) 모두 마감이 정해져 있다.
@Test func credentialEnvelopeBytesCrossTheStdinPathWithoutPython() async throws {
  let tinyKey = "synthetic-tiny-transport-key"
  // 봉투 모양을 못 박아 둔다 — 자식이 비교하는 세 줄과 정확히 같은 바이트다.
  let wire = try EngineCredentialEnvelope.key(tinyKey).wire
  #expect(
    String(decoding: wire, as: UTF8.self)
      == "VLOGSRT-ENGINE-CARRIER 1\n\(Data(tinyKey.utf8).count)\n\(tinyKey)\n")

  let root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath()
    .appendingPathComponent("S2A-2 tiny transport \(UUID().uuidString)", isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let scripts = root.appendingPathComponent("scripts", isDirectory: true)
  try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
  let job = root.appendingPathComponent("job dir", isDirectory: true)
  try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)

  // 시험 소유 자식 — argv: operation job_id job_dir [marker] [status_file] 또는
  // 마커 없이 operation job_id job_dir [status_file]. 마커가 없으면 stdin을
  // 건드리지 않는다(낡은 계약) — 이때 상태 경로는 넷째 자리에 온다. read -t는
  // 초 단위 마감이다.
  let executable = scripts.appendingPathComponent("vlogsrt-engine")
  let script = """
    #!/bin/zsh -f
    # 시험 소유 자식(S2A-2 작은 수송 시험) — Python·설정·환경·주변 읽기 없음.
    set -u
    operation=$1
    job_id=$2
    job_dir=$3
    marker=""
    status_file=""
    if [[ "${4:-}" == "--stdin-carrier" ]]; then
      marker=$4
      status_file=${5:-}
    else
      status_file=${4:-}
    fi
    record() {
      if [[ -n "$status_file" ]]; then
        printf 'VLOGSRT-ENGINE-STATUS %s %s\\n' "${status_file:t:r}" "$1" > "$status_file"
      fi
    }
    if [[ "$marker" != "--stdin-carrier" ]]; then
      record 0
      exit 0
    fi
    IFS=''
    read -t 5 -r magic || { record 2; exit 0; }
    read -t 5 -r length || { record 2; exit 0; }
    read -t 5 -r payload || { record 2; exit 0; }
    if [[ "$magic" == "VLOGSRT-ENGINE-CARRIER 1" && "$length" == "\(Data(tinyKey.utf8).count)" && "$payload" == "\(tinyKey)" ]]; then
      record 0
    else
      record 2
    fi
    exit 0
    """
  try Data(script.utf8).write(to: executable)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)

  let launcher = DevelopmentEngineLauncher()

  // (1) 봉투 첨부 — 실제 create가 마커를 붙이고, 프리필이 stdin으로 흘러간다.
  let carried = try DevelopmentEngineInvocation.create(
    operation: .analyze,
    jobID: "tiny-transport",
    externalJob: job,
    applicationScriptsDirectory: scripts,
    apiKey: tinyKey
  )
  #expect(!carried.arguments.joined().contains(tinyKey))
  #expect(
    try await _withDeadline(seconds: 30, label: "작은 수송(봉투)") {
      try await launcher.run(carried)
    } == 0)

  // (2) 마커만 있고 봉투 없음 — stdin은 기본(/dev/null): 읽기가 즉시 비어
  // 실패 닫힘(2)으로 기록된다. 마커·stdin 판독의 Python 없는 실측이다.
  let markerOnly = DevelopmentEngineInvocation(
    executableURL: executable,
    arguments: [
      "analyze", "tiny-transport", job.path, DevelopmentEngineInvocation.carrierMarkerArgument,
    ])
  #expect(
    try await _withDeadline(seconds: 30, label: "작은 수송(마커만)") {
      try await launcher.run(markerOnly)
    } == 2)

  // (3) 낡은 계약 — 마커 없는 호출은 stdin을 건드리지 않고 0으로 끝난다.
  let legacy = DevelopmentEngineInvocation(
    executableURL: executable, arguments: ["analyze", "tiny-transport", job.path])
  #expect(
    try await _withDeadline(seconds: 30, label: "작은 수송(낡은 계약)") {
      try await launcher.run(legacy)
    } == 0)
}

/// 주입 쓰기의 상태 — 부분 쓰기와 EINTR을 커널처럼 흉내 낸다(안전한 시험 경계).
final class PrefillScriptState: @unchecked Sendable {
  let lock = NSLock()
  private var interrupts = 0

  var interruptsHit: Int {
    lock.lock()
    defer { lock.unlock() }
    return interrupts
  }

  /// 첫 호출은 EINTR, 이후 실제로 1바이트씩 쓴다 — 반환값만 1로 꾸미면 파이프가
  /// 빈 채로 남는다(반복문은 돌았지만 바이트는 안 실렸다는 뜻이 된다).
  var oneByteAtATimeWithInitialEINTR: CarrierRawWrite {
    { [self] fd, bytes, _ in
      self.lock.lock()
      let first = self.interrupts == 0
      if first { self.interrupts += 1 }
      self.lock.unlock()
      if first { throw POSIXError(.EINTR) }
      let written = Darwin.write(fd, bytes, 1)  // 참된 부분 쓰기 — 1바이트만
      if written >= 0 { return written }
      throw POSIXError(POSIXError.Code(rawValue: errno) ?? POSIXError.Code.EIO)
    }
  }

  /// 즉시 EAGAIN — 용량 부족 흉내.
  static var eagainOnce: CarrierRawWrite {
    { _, _, _ in throw POSIXError(.EAGAIN) }
  }

  /// 0바이트 '성공' — 진행이 없는 쓰기.
  static var zeroWriteForever: CarrierRawWrite {
    { _, _, _ in 0 }
  }

  /// 그 밖의 오류 — 원문 그대로 나가는지 본다.
  static var genericErrorOnce: CarrierRawWrite {
    { _, _, _ in throw PrefillScriptError.injected }
  }
}

struct PrefillScriptError: Error, Equatable {
  static let injected = PrefillScriptError()
}
