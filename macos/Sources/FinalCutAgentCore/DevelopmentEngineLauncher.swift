import Foundation

public enum DevelopmentEngineOperation: String, Sendable {
  case analyze
  case roughcut
}

/// 앱 실행의 사적 자격 증명 운반 — 값은 이 enum 안에만 산다(S2A-2).
///
/// `.key`는 이 호출이 쓸 키이고, `.explicitNoKey`는 "이 실행에는 키가 없다"는
/// 명시적 선언이다. 둘 다 stdin 봉투로 흘러가 자식 안에서 상속된 낡은 키를
/// 대체하거나 내려놓는다 — argv·파일·부모 환경 전역 뮤테이션은 없다.
public enum EngineCredentialEnvelope: Equatable, Sendable {
  public static let maximumByteCount = 8192

  case key(String)
  case explicitNoKey

  /// stdin 봉투의 바이트 — 마법줄·버전, 십진 **바이트** 길이, 본문, 끝표식
  /// 줄바꿈. 끝표식이 있어 봉투는 스스로 끝난다: 읽는 쪽이 쓰기 끝의 닫힘
  /// 시점을 기다리지 않는다. 페이로드에 NUL이 있거나 상한을 넘으면 만들지
  /// 않는다(실패 닫힘) — 파이촌 판독기의 상한·검사와 같은 값이다.
  public var wire: Data {
    get throws {
      let payload: Data
      switch self {
      case .key(let value):
        guard !value.contains("\0") else {
          throw DevelopmentEngineError.credentialEnvelopeContainsNUL
        }
        payload = Data(value.utf8)
      case .explicitNoKey: payload = Data()
      }
      guard payload.count <= Self.maximumByteCount else {
        throw DevelopmentEngineError.credentialEnvelopeTooLarge
      }
      let header = "VLOGSRT-ENGINE-CARRIER 1\n\(payload.count)\n".data(using: .utf8) ?? Data()
      return header + payload + Data("\n".utf8)
    }
  }
}

public struct DevelopmentEngineInvocation: Equatable, Sendable {
  public static let scriptName = "vlogsrt-engine"
  /// stdin 봉투를 켜는 argv 마커 — 비밀이 아니다(값은 절대 argv에 오지 않는다).
  public static let carrierMarkerArgument = "--stdin-carrier"
  public let executableURL: URL
  public let arguments: [String]
  /// nil이면 낡은 계약(argv만, stdin을 열지 않는다) — 앱 경로는 항상 봉투를 낀다.
  public let credentialEnvelope: EngineCredentialEnvelope?

  init(
    executableURL: URL,
    arguments: [String],
    credentialEnvelope: EngineCredentialEnvelope? = nil
  ) {
    self.executableURL = executableURL
    self.arguments = arguments
    self.credentialEnvelope = credentialEnvelope
  }

  /// 앱 실행의 호출 조립 — 키가 있으면 봉투로, 없어도 '키 없음' 봉투로 낀다.
  /// 없는 키를 봉투 없이 보내면 자식이 상속된 낡은 키로 유료 호출을 열 수 있다.
  public static func create(
    operation: DevelopmentEngineOperation,
    jobID: String,
    externalJob: URL,
    applicationScriptsDirectory: URL,
    apiKey: String? = nil
  ) throws -> Self {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw WorkspaceError.invalidJobID(jobID)
    }
    let envelope: EngineCredentialEnvelope = (apiKey ?? "").isEmpty
      ? .explicitNoKey : .key(apiKey ?? "")
    return Self(
      executableURL: applicationScriptsDirectory.appendingPathComponent(scriptName),
      arguments: [
        operation.rawValue, jobID, externalJob.standardizedFileURL.path,
        Self.carrierMarkerArgument,
      ],
      credentialEnvelope: envelope
    )
  }
}

public enum DevelopmentJobStager {
  private static let inputs = [
    "input.fcpxml",
    "project-selection.jsonl",
    "pricing-snapshot.jsonl",
    "analysis-authorization.jsonl",
    "edit-plan.jsonl",
    "approved-candidates.txt",
    "cancellation.jsonl",
    "media-grant.json",
    "caption-authorization.jsonl",
  ]
  private static let results = [
    "analysis-estimate.jsonl",
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "candidates.jsonl",
    "vision-decisions.jsonl",
    "analysis-result.jsonl",
    "edit-plan.jsonl",
    "roughcut.fcpxml",
    "roughcut.fcpxml.review.json",
    "caption-request.jsonl",
    "caption-estimate.jsonl",
    "caption-result.jsonl",
    "cancellation.jsonl",
  ]
  /// 실패한 실행이 앱으로 나갈 수 있는 진단 증표 — 성공 산물(후보·러프컷)은 없다.
  /// 실패를 보여주려고 성공 실행을 다시 요구하지 않는다(S1B).
  private static let diagnostics = [
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "vision-decisions.jsonl",
    "analysis-result.jsonl",
  ]

  public static func stage(
    jobID: String,
    appGroupJob: URL,
    externalRoot: URL,
    fileManager: FileManager = .default
  ) throws -> URL {
    guard AppGroupWorkspace.isSafeJobID(jobID) else {
      throw WorkspaceError.invalidJobID(jobID)
    }
    try fileManager.createDirectory(at: externalRoot, withIntermediateDirectories: true)
    let confinedRoot = externalRoot.resolvingSymlinksInPath().standardizedFileURL
    let jobs = confinedRoot.appendingPathComponent("Jobs", isDirectory: true)
    try requireDirectoryOrMissing(jobs, fileManager: fileManager)
    let destination = jobs.appendingPathComponent(jobID, isDirectory: true)
    try fileManager.createDirectory(at: destination, withIntermediateDirectories: true)
    try requireDirectory(destination, fileManager: fileManager)
    guard destination.resolvingSymlinksInPath().path.hasPrefix(confinedRoot.path + "/") else {
      throw DevelopmentEngineError.unsafePath(destination.path)
    }
    for name in inputs {
      let source = appGroupJob.appendingPathComponent(name)
      guard fileManager.fileExists(atPath: source.path) else { continue }
      try requireRegularFile(source)
      try copyStable(source, destination.appendingPathComponent(name), fileManager: fileManager)
    }
    guard fileManager.fileExists(atPath: destination.appendingPathComponent("input.fcpxml").path)
    else { throw DevelopmentEngineError.missingInput }
    return destination
  }

  public static func syncResults(
    from externalJob: URL,
    to appGroupJob: URL,
    diagnosticsOnly: Bool = false,
    fileManager: FileManager = .default
  ) throws {
    // 취소된 작업의 결과는 동기화하지 않는다 — 취소 뒤 새 호출·sync·import 차단.
    guard !CancellationStore.isCancelled(jobURL: externalJob, fileManager: fileManager) else {
      throw DevelopmentEngineError.cancelled
    }
    for name in diagnosticsOnly ? diagnostics : results {
      let rootSource = externalJob.appendingPathComponent(name)
      let workspaceSource = externalJob.appendingPathComponent("workspace").appendingPathComponent(name)
      let source = fileManager.fileExists(atPath: rootSource.path) ? rootSource : workspaceSource
      guard fileManager.fileExists(atPath: source.path) else { continue }
      try requireRegularFile(source)
      let destination = appGroupJob.appendingPathComponent(name)
      if fileManager.fileExists(atPath: destination.path) {
        try fileManager.removeItem(at: destination)
      }
      try fileManager.copyItem(at: source, to: destination)
    }
  }

  private static func copyStable(
    _ source: URL,
    _ destination: URL,
    fileManager: FileManager
  ) throws {
    if fileManager.fileExists(atPath: destination.path) {
      guard try Data(contentsOf: source) == Data(contentsOf: destination) else {
        throw DevelopmentEngineError.stagedInputChanged(destination.lastPathComponent)
      }
      return
    }
    try fileManager.copyItem(at: source, to: destination)
  }

  private static func requireDirectoryOrMissing(
    _ url: URL, fileManager: FileManager
  ) throws {
    guard fileManager.fileExists(atPath: url.path) else { return }
    try requireDirectory(url, fileManager: fileManager)
  }

  private static func requireDirectory(
    _ url: URL, fileManager: FileManager
  ) throws {
    let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard values.isDirectory == true, values.isSymbolicLink != true else {
      throw DevelopmentEngineError.unsafePath(url.path)
    }
  }

  private static func requireRegularFile(_ url: URL) throws {
    let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true else {
      throw DevelopmentEngineError.nonRegularFile(url.lastPathComponent)
    }
  }
}

/// write(2) 하나의 손잡이 — 실물은 Darwin.write, 시험은 부분 쓰기·EINTR·
/// EAGAIN·기타 오류를 안전하게 흉내 낸다(S2A-2). 반환은 쓴 바이트 수고,
/// 오류는 그 자리에서 던진다.
public typealias CarrierRawWrite = @Sendable (
  _ fd: Int32, _ bytes: UnsafeRawPointer, _ count: Int
) throws -> Int

public final class DevelopmentEngineLauncher: @unchecked Sendable {
  private let rawWrite: CarrierRawWrite

  /// 시험은 rawWrite로 커널의 쓰기 행동을 주입한다 — 실물 계약 그대로 돈다.
  public init(rawWrite: @escaping CarrierRawWrite = {
    fd, bytes, count in
    let written = Darwin.write(fd, bytes, count)
    if written >= 0 { return written }
    throw POSIXError(POSIXError.Code(rawValue: errno) ?? POSIXError.Code.EIO)
  }) {
    self.rawWrite = rawWrite
  }

  public func run(_ invocation: DevelopmentEngineInvocation) async throws -> Int32 {
    // NSUserUnixTask는 비0 종료·신호를 오류로만 알린다. 이 실행의 안쪽 종료
    // 코드는 엔진 진입점이 남기는 소유된 기록으로만 읽는다(S2A-1). 계약: 진입점은
    // 상태 전달이 끝난 뒤에만 0으로 끝난다 — 그러므로 기록은 **오류가 없을 때만**
    // 받아들인다. 오류 문면을 해석하는 휴리스틱은 아예 없다.
    let capture = try EngineStatusCapture.make()
    defer { capture.discard() }  // 시작 실패·취소·오류 어떤 끝에서도 치운다.
    let task = try NSUserUnixTask(url: invocation.executableURL)
    // 봉투의 생애 — 되감기 없는 한 방향이다(S2A-2 재정리).
    //   ① 검증·부호화가 먼저(과대·NUL은 파이프조차 만들지 않는다)
    //   ② 사적 Pipe — 읽기 끝을 자식 stdin으로, 쓰기 끝은 이쪽이 쥔다
    //   ③ 쓰기 끝을 논블로킹으로
    //   ④ 프레임 전부를 큐에 넣는다(부분·EINTR 계속, EAGAIN·오류는 그 자리에서 실패)
    //   ⑤ 쓰기 끝을 execute 앞에서 정확히 한 번 닫는다
    //   ⑥ 전부 쓰였을 때만 실행기를 띄운다
    //   ⑦ 읽기 끝은 completion(성공·실패 어느 쪽이든)에서 정확히 한 번 닫는다
    // prefill 동안 부모가 읽기 끝을 계속 쥐고 있으므로 파이프에는 언제나 독자가
    // 있다 — 쓰다가 EPIPE/SIGPIPE를 만날 길이 없고, 전역 신호·환경 손대기도 없다.
    // 읽기 끝이 execute를 넘겨까지 사는 이유는 XPC가 standardInput을 execute
    // 안에서 전송 인자로 부호화하기 때문이다.
    var carrier: CarrierPipeEnds? = nil
    if let envelope = invocation.credentialEnvelope {
      let wire = try envelope.wire  // ①
      let pipe = Pipe()
      let ends = CarrierPipeEnds(pipe: pipe)
      do {
        task.standardInput = pipe.fileHandleForReading  // ②
        try ends.makeWriteEndNonBlocking()  // ③
        try ends.queueFrame(wire, rawWrite: rawWrite)  // ④
        ends.closeWriteOnce()  // ⑤
      } catch {
        // prefill 실패 — 남은 끝을 닫고 실행기는 띄우지 않는다(실패 닫힘).
        ends.closeRemainingOnce()
        throw error
      }
      carrier = ends
    }
    // completion(@Sendable)은 불변 참조만 잡는다 — var를 잡으면 자료 경쟁 경고다.
    let readEndCloser: CarrierPipeEnds? = carrier
    return try await withCheckedThrowingContinuation { continuation in
      task.execute(withArguments: invocation.arguments + [capture.fileURL.path]) { error in
        readEndCloser?.closeReadOnce()  // ⑦ — 읽기 끝은 여기서만 닫힌다
        continuation.resume(with: Result {
          // 오류(비0 종료·신호·시작 실패·낡은 래퍼)는 기록이 있어도 재분류하지
          // 않는다 — 없는 기록과 함께 오류로 나간다.
          if let error { throw error }
          return try capture.readExitStatus()
        })
      }
    }
  }
}

/// 봉투 파이프의 두 끝 — 닫기는 끝마다 정확히 한 번뿐이다(NSFileHandle은 두 번
/// 닫힘을 잡을 수 없는 ObjC 예외로 죽는다). 읽기 끝은 실행 결말까지 산다.
final class CarrierPipeEnds: @unchecked Sendable {
  /// 연속 EINTR의 상한 — 진전 없이 이 횟수만큼 갈라지면 병리 상태로 보고 실패로
  /// 닫는다(주입된 끝없는 인터럽트가 반복문을 붙잡는 일이 없게). 한 바이트라도
  /// 쓰면 계수는 되돌아간다 — 실제 커널은 신호 사이에 진전을 만든다.
  static let maximumInterruptBurst = 100

  private let pipe: Pipe
  private let lock = NSLock()
  private var writeClosed = false
  private var readClosed = false

  init(pipe: Pipe) {
    self.pipe = pipe
  }

  var writeDescriptor: Int32 { pipe.fileHandleForWriting.fileDescriptor }

  /// 쓰기 끝을 논블로킹으로 — 파이프 용량을 가정하지 않고 EAGAIN을 기다림 없이
  /// 실패로 바로잡기 위한 최소 준비다.
  func makeWriteEndNonBlocking() throws {
    let flags = fcntl(self.writeDescriptor, F_GETFL)
    guard flags >= 0,
      fcntl(self.writeDescriptor, F_SETFL, flags | O_NONBLOCK) >= 0
    else { throw DevelopmentEngineError.credentialCarrierUnavailable }
  }

  /// 프레임 전체를 큐에 넣는다 — 부분 쓰기는 남은 만큼 계속하고, EINTR은 다시
  /// 하되 **유한한 상한** 안에서만 하며, EAGAIN·그 밖의 오류는 기다리지 않고 그
  /// 자리에서 실패한다(용량·속도 추정이 없다). 0바이트 '성공'은 진행이 아니므로
  /// 실패로 닫는다.
  func queueFrame(_ data: Data, rawWrite: CarrierRawWrite) throws {
    var offset = 0
    var interruptBurst = 0
    try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
      guard let base = raw.baseAddress else { return }  // 빈 프레임은 쓸 것도 없다
      while offset < data.count {
        let written: Int
        do {
          written = try rawWrite(
            self.writeDescriptor, base + offset, data.count - offset)
        } catch let interrupted as POSIXError where interrupted.code == .EINTR {
          interruptBurst += 1
          if interruptBurst > Self.maximumInterruptBurst {
            throw DevelopmentEngineError.credentialCarrierWriteFailed
          }
          continue  // 신호에 갈라진 쓰기 — 기다림 없이 다시 한다(상한 안에서)
        }
        guard written > 0 else {
          throw DevelopmentEngineError.credentialCarrierWriteFailed
        }
        offset += written
        interruptBurst = 0  // 진전이 있었다 — 인터럽트 연쇄는 아니라는 뜻이다
      }
    }
  }

  /// 쓰기 끝을 닫는다 — 자식이 EOF를 본다. execute 앞에서 정확히 한 번.
  func closeWriteOnce() {
    lock.lock()
    let shouldClose = !writeClosed
    writeClosed = true
    lock.unlock()
    guard shouldClose else { return }
    try? pipe.fileHandleForWriting.close()
  }

  /// 읽기 끝을 닫는다 — completion에서 정확히 한 번.
  func closeReadOnce() {
    lock.lock()
    let shouldClose = !readClosed
    readClosed = true
    lock.unlock()
    guard shouldClose else { return }
    try? pipe.fileHandleForReading.close()
  }

  /// prefill 실패 길 — 남은 끝 둘 다 닫는다(쓰기 끝은 아직 열려 있을 때만).
  func closeRemainingOnce() {
    lock.lock()
    let closeWrite = !writeClosed
    let closeRead = !readClosed
    writeClosed = true
    readClosed = true
    lock.unlock()
    if closeWrite { try? pipe.fileHandleForWriting.close() }
    if closeRead { try? pipe.fileHandleForReading.close() }
  }
}

/// 호출 한 번의 종료 상태 기록 — 실행기가 만든 전용 임시 경로에 엔진이 남긴다.
///
/// 경로는 job 상태 밖의 고유한 임시 디렉터리 안에 있고 호출마다 새로 만든다:
/// 낡은 기록·남의 기록이 이 실행으로 읽힐 자리가 없다(token은 파일 이름 줄기).
/// 읽기는 상한(256바이트+1)까지만 — 큰 기록이 메모리를 채우는 길이 없다.
/// 기록이 없으면(낡은 래퍼·시작 실패·비정상 종료) 오류다 — 상태를 추측하지 않는다.
struct EngineStatusCapture {
  static let prefix = "VLOGSRT-ENGINE-STATUS"
  static let maximumByteCount = 256

  let directory: URL
  let token: String

  var fileURL: URL { directory.appendingPathComponent("\(token).status") }

  static func make(fileManager: FileManager = .default) throws -> Self {
    let token = UUID().uuidString
    let directory = fileManager.temporaryDirectory
      .appendingPathComponent("vlogsrt-status-\(token)", isDirectory: true)
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: false)
    return Self(directory: directory, token: token)
  }

  func readExitStatus() throws -> Int32 {
    guard let handle = FileHandle(forReadingAtPath: fileURL.path) else {
      throw DevelopmentEngineError.missingStatusRecord
    }
    defer { try? handle.close() }
    // 상한보다 한 바이트 더 읽는다 — 그 한 바이트가 차면 과대 기록이다.
    guard
      let head = try handle.read(upToCount: Self.maximumByteCount + 1),
      head.count <= Self.maximumByteCount,
      var line = String(data: head, encoding: .utf8),
      line.hasSuffix("\n")
    else { throw DevelopmentEngineError.malformedStatusRecord }
    line.removeLast()
    let parts = line.split(separator: " ")
    guard parts.count == 3, parts[0] == Self.prefix, parts[1] == Substring(token),
      let code = Int32(parts[2]), (0...255).contains(code)
    else { throw DevelopmentEngineError.malformedStatusRecord }
    return code
  }

  func discard(fileManager: FileManager = .default) {
    try? fileManager.removeItem(at: directory)
  }
}

public enum DevelopmentEngineError: Error, Equatable, LocalizedError {
  case missingInput
  case stagedInputChanged(String)
  case unsafePath(String)
  case nonRegularFile(String)
  case cancelled
  case missingStatusRecord
  case malformedStatusRecord
  case credentialEnvelopeTooLarge
  case credentialEnvelopeContainsNUL
  case credentialCarrierUnavailable
  case credentialCarrierWriteFailed

  public var errorDescription: String? {
    switch self {
    case .missingInput:
      "App Group job에 input.fcpxml이 없습니다"
    case .stagedInputChanged(let name):
      "재개하려는 외부 작업의 입력이 바뀌었습니다: \(name)"
    case .unsafePath(let path):
      "작업 폴더가 symlink이거나 허용 범위 밖입니다: \(path)"
    case .nonRegularFile(let name):
      "동기화 대상이 일반 파일이 아닙니다: \(name)"
    case .cancelled:
      "취소된 작업입니다 — 새 호출·동기화·import를 하지 않습니다"
    case .missingStatusRecord:
      "엔진이 종료 상태 기록을 남기지 않았습니다 — 상태를 추측하지 않습니다"
    case .malformedStatusRecord:
      "엔진의 종료 상태 기록이 이 실행의 것이 아닙니다"
    case .credentialEnvelopeTooLarge:
      "자격 증명 봉투가 상한을 넘습니다 — 실행을 시작하지 않습니다"
    case .credentialEnvelopeContainsNUL:
      "자격 증명에 NUL 바이트가 들어 있습니다 — 실행을 시작하지 않습니다"
    case .credentialCarrierUnavailable:
      "자격 증명 통로를 준비하지 못했습니다 — 실행을 시작하지 않습니다"
    case .credentialCarrierWriteFailed:
      "자격 증명 통로에 쓰지 못했습니다 — 실행을 시작하지 않습니다"
    }
  }
}
