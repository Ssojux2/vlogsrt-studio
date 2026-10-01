import Foundation

/// 개발 실행의 둘째 운송 구간(S2A-3) — 실행기가 도는 동안 외부 job의 진행
/// 스냅샷을 App Group job으로 원자적으로 옮긴다.
///
/// 복사는 **이 호출의 증거에 묶인다**: 상한 안에서 한 번 읽은 바이트를 해석해
/// invocation_id가 이 실행이 자식 argv에 심은 호출 신원과 같을 때, **그 바이트
/// 그대로**를 발행한다. 검증과 발행이 같은 바이트를 보므로 그 사이에 소스가
/// 바뀌어도 다른 호출의 스냅샷이 흘러들 길이 없다. 그 신원은 엔진이 실행 잠금을
/// 얻은 뒤에만 기록에 실으므로, 중복(3)으로 지는 호출은 이긴 호출의 진행을 흉내
/// 내지 못한다. 성공 산물(후보·러프컷)은 여전히 실행 뒤의 동기화 게이트로만
/// 나간다 — 이 릴레이가 옮기는 것은 진행 한 장뿐이다.
public struct EngineProgressRelay: Sendable {
  public let intervalNanoseconds: UInt64
  private let tick: @Sendable (UInt64) async throws -> Void
  private let read: @Sendable (URL) -> Data?
  private let decode: @Sendable (Data) -> AnalysisProgressMessage?
  private let publish: @Sendable (Data, URL) throws -> Void

  /// 선택 클로저는 몸통에서 풀린다 — 공개 초기화의 기본 인자가 내부 도우미를
  /// 참조할 수 없기 때문이다(가림 없는 기본값만 공개 계약에 남는다).
  public init(
    intervalNanoseconds: UInt64 = 500_000_000,
    tick: @escaping @Sendable (UInt64) async throws -> Void = {
      try await Task.sleep(nanoseconds: $0)
    },
    read: (@Sendable (URL) -> Data?)? = nil,
    decode: (@Sendable (Data) -> AnalysisProgressMessage?)? = nil,
    publish: (@Sendable (Data, URL) throws -> Void)? = nil
  ) {
    self.intervalNanoseconds = intervalNanoseconds
    self.tick = tick
    self.read = read ?? { url in EngineProgressRelay.readBoundedBytes(at: url) }
    self.decode = decode ?? { bytes in EngineProgressRelay.decodeSnapshot(bytes) }
    self.publish = publish ?? { bytes, destination in
      try EngineProgressRelay.atomicPublish(bytes, to: destination)
    }
  }

  /// 실행기가 도는 동안 돌리는 과제 — 호출자가 끝에서 취소·합류한다. 정지
  /// 조건(취소·활성 선택 드리프트)은 호출자가 이 실행의 재료로 넣는다.
  func start(
    externalJob: URL,
    appGroupJob: URL,
    invocationID: String,
    isStopped: @escaping @Sendable () -> Bool
  ) -> Task<Void, Never> {
    let interval = intervalNanoseconds
    let sourceURL = externalJob.appendingPathComponent(RoundTripJob.progressFileName)
    let destinationURL = appGroupJob.appendingPathComponent(RoundTripJob.progressFileName)
    return Task.detached { [tick, read, decode, publish] in
      while true {
        do { try await tick(interval) } catch { return }
        if Task.isCancelled { return }
        // 검증과 발행이 같은 바이트를 본다 — 소스가 그 사이에 바뀌어도 이미
        // 검증한 스냅샷만 흘러간다(S2A-3).
        guard let bytes = read(sourceURL), let snapshot = decode(bytes),
          snapshot.invocationID == invocationID
        else { continue }
        if Task.isCancelled || isStopped() { return }  // 발행 직전 게이트
        try? publish(bytes, destinationURL)
      }
    }
  }

  /// 상한 안의 한 덩어리 읽기 — 과대·줄바꿈 없음은 nil(흘러들지 않는다).
  static func readBoundedBytes(at url: URL) -> Data? {
    guard let handle = FileHandle(forReadingAtPath: url.path) else { return nil }
    defer { try? handle.close() }
    guard
      let head = try? handle.read(upToCount: WorkflowProgressStore.maximumByteCount + 1),
      head.count <= WorkflowProgressStore.maximumByteCount,
      head.last == UInt8(ascii: "\n")
    else { return nil }
    return head
  }

  static func decodeSnapshot(_ bytes: Data) -> AnalysisProgressMessage? {
    guard var line = String(data: bytes, encoding: .utf8), line.hasSuffix("\n") else {
      return nil
    }
    line.removeLast()
    guard
      let message = try? ProtocolCodec.decodeAnalysis(
        AnalysisProgressMessage.self, type: "AnalysisProgress", from: line)
    else { return nil }
    return AnalysisProgressValidator.validate(message) ? message : nil
  }

  /// 검증된 바이트의 원자 발행 — 읽는 쪽은 반쪽 바이트를 못 본다(임시 쓰기 →
  /// 자리 바꿈치기).
  static func atomicPublish(_ bytes: Data, to destination: URL) throws {
    let fileManager = FileManager.default
    let temporary = destination.deletingLastPathComponent()
      .appendingPathComponent(".\(destination.lastPathComponent).relay.tmp")
    try bytes.write(to: temporary, options: [.atomic])
    if fileManager.fileExists(atPath: destination.path) {
      _ = try fileManager.replaceItemAt(destination, withItemAt: temporary)
    } else {
      try fileManager.moveItem(at: temporary, to: destination)
    }
  }
}

/// 활성 선택의 읽기 전용 탐침(S2A-3) — 릴레이의 정지 조건이 @Sendable 경계
/// 너머에서 재료를 쓰기 위한 좁은 그릇. UserDefaults는 같은 인스턴스 안의
/// 스레드 안전을 애플이 문서로 보증한다 — 그 보증 위에만 얹힌 @unchecked다.
/// 레지스트리를 여는 것이 아니라 이 읽기 하나만 실어 나르고, 비공개 키는
/// `requireCurrent` 안에 그대로 봉인돼 있다.
private struct SelectionDriftProbe: @unchecked Sendable {
  private let registry: ActiveSelectionRegistry
  private let revision: String

  init(registry: ActiveSelectionRegistry, revision: String) {
    self.registry = registry
    self.revision = revision
  }

  /// 활성 선택이 아직 이 리비전인가 — 던지는 `requireCurrent`의 값 판정 짝.
  func isCurrent() -> Bool {
    (try? registry.requireCurrent(revision)) != nil
  }
}

/// 엔진 실행 계획 — Release는 번들만, Debug만 개발 폴백을 허용한다.
public enum EngineBuildConfiguration: String, Sendable, Equatable {
  case debug
  case release
}

public enum EngineLaunchPlan: Equatable, Sendable {
  case bundled(BundledEngineInvocation)
  case development(DevelopmentEngineInvocation)
  case unavailable(String)

  public var isExecutable: Bool {
    switch self {
    case .bundled, .development: true
    case .unavailable: false
    }
  }
}

/// 실행기 선택과 호출 인자 조립 — 이 표가 앱의 유일한 분기다.
public enum EngineRuntime {
  /// V1 단일 키 경로 — 낡은 계약 그대로다(직접 호출·기존 호출자).
  public static func resolvePlan(
    configuration: EngineBuildConfiguration,
    operation: DevelopmentEngineOperation,
    jobID: String,
    appGroupJob: URL,
    engineRoot: URL?,
    applicationScriptsDirectory: URL?,
    developmentExternalJob: URL?,
    apiKey: String?,
    fileManager: FileManager = .default
  ) -> EngineLaunchPlan {
    guard
      let plan = try? resolvePlanThrowing(
        configuration: configuration,
        bundled: { engineRoot in
          try BundledEngineInvocation.create(
            operation: operation, jobID: jobID, jobDirectory: appGroupJob,
            engineRoot: engineRoot, apiKey: apiKey)
        },
        development: { scripts, externalJob in
          try DevelopmentEngineInvocation.create(
            operation: operation, jobID: jobID, externalJob: externalJob,
            applicationScriptsDirectory: scripts, apiKey: apiKey)
        },
        applicationScriptsDirectory: applicationScriptsDirectory,
        developmentExternalJob: developmentExternalJob,
        engineRoot: engineRoot,
        fileManager: fileManager)
    else {
      return .unavailable(
        "실행 가능한 엔진이 없다 — Debug에서는 개발 스크립트와 외부 작업 폴더가 필요하다"
      )
    }
    return plan
  }

  /// V2 역할 경로 — 앱 호출의 정상 경로다(S2A-2 단위2). 세 값이 함께 가고
  /// 빈 문자열은 역할마다 독립 부재다. V1 운반자는 아예 실리지 않는다(자식의
  /// 충돌 판정이 두 운반자를 같은 호출에 허용하지 않는다).
  public static func resolvePlan(
    configuration: EngineBuildConfiguration,
    operation: DevelopmentEngineOperation,
    jobID: String,
    appGroupJob: URL,
    engineRoot: URL?,
    applicationScriptsDirectory: URL?,
    developmentExternalJob: URL?,
    credentials: EngineRoleCredentials,
    fileManager: FileManager = .default
  ) -> EngineLaunchPlan {
    guard
      let plan = try? resolvePlanThrowing(
        configuration: configuration,
        bundled: { engineRoot in
          try BundledEngineInvocation.createRoles(
            operation: operation, jobID: jobID, jobDirectory: appGroupJob,
            engineRoot: engineRoot, credentials: credentials)
        },
        development: { scripts, externalJob in
          try DevelopmentEngineInvocation.createRoles(
            operation: operation, jobID: jobID, externalJob: externalJob,
            applicationScriptsDirectory: scripts, credentials: credentials)
        },
        applicationScriptsDirectory: applicationScriptsDirectory,
        developmentExternalJob: developmentExternalJob,
        engineRoot: engineRoot,
        fileManager: fileManager)
    else {
      return .unavailable(
        "실행 가능한 엔진이 없다 — Debug에서는 개발 스크립트와 외부 작업 폴더가 필요하다"
      )
    }
    return plan
  }

  /// 검증 계획 — 호출 조립 실패를 삼키지 않고 그대로 던진다(S2A-2 마지막 단위).
  ///
  /// 그르른 운반자(NUL·과대 봉투)를 이유로 다른 엔진을 조용히 고르는 길이
  /// 없다: 실패는 호출자의 교정 글로 올라간다. 모드 선택·실행 가능 검사는
  /// 비검증 경로와 같은 표를 쓴다.
  public static func resolvePlanValidated(
    configuration: EngineBuildConfiguration,
    operation: DevelopmentEngineOperation,
    jobID: String,
    appGroupJob: URL,
    engineRoot: URL?,
    applicationScriptsDirectory: URL?,
    developmentExternalJob: URL?,
    credentials: EngineRoleCredentials,
    fileManager: FileManager = .default
  ) throws -> EngineLaunchPlan {
    try resolvePlanThrowing(
      configuration: configuration,
      bundled: { engineRoot in
        try BundledEngineInvocation.createRoles(
          operation: operation, jobID: jobID, jobDirectory: appGroupJob,
          engineRoot: engineRoot, credentials: credentials)
      },
      development: { scripts, externalJob in
        try DevelopmentEngineInvocation.createRoles(
          operation: operation, jobID: jobID, externalJob: externalJob,
          applicationScriptsDirectory: scripts, credentials: credentials)
      },
      applicationScriptsDirectory: applicationScriptsDirectory,
      developmentExternalJob: developmentExternalJob,
      engineRoot: engineRoot,
      fileManager: fileManager)
  }

  /// 분기의 본문 — 두 공개 경로가 같은 표를 쓴다(운반자 조립만 갈라진다).
  /// 선택된 실행길의 비선택값(옵셔널)은 이 문에서 풀어 클로저 인자로 넘긴다 —
  /// 클로저가 바깥 옵셔널을 포획하면 호출부의 보장이 클로저 안까지 좁혀지지
  /// 않는다(컴파일 오류). 대상은 언제나 풀린 값이다.
  ///
  /// 호출 조립(`bundled`·`development`)이 던지면 그대로 올라간다 — 검증 경로는
  /// 교정 글로, 비검증 경로는 unavailable 하나로 접는다. 그르른 운반자를 이유로
  /// 다른 엔진을 조용히 고르는 길은 어느 쪽에도 없다(S2A-2).
  private static func resolvePlanThrowing(
    configuration: EngineBuildConfiguration,
    bundled: (URL) throws -> BundledEngineInvocation,
    development: (URL, URL) throws -> DevelopmentEngineInvocation,
    applicationScriptsDirectory: URL?,
    developmentExternalJob: URL?,
    engineRoot: URL?,
    fileManager: FileManager
  ) throws -> EngineLaunchPlan {
    if let engineRoot, EngineLaunchMode.resolve(bundleURL: engineRoot, fileManager: fileManager)
      == .bundled
    {
      return .bundled(try bundled(engineRoot))
    }
    guard configuration == .debug else {
      return .unavailable(
        "Release 빌드는 번들 엔진으로만 실행한다 — Resources/Engine이 없다(개발 폴백 없음)"
      )
    }
    if let scripts = applicationScriptsDirectory, let externalJob = developmentExternalJob {
      // 개발 폴백은 **실행 가능한** 래퍼가 있을 때만이다(S2A-2). 래퍼의 존재를
      // 보지 않으면 계획이 development로 나가 실행 단계에서야 죽는다 — 준비의
      // 말이 거짓이 된다. 교정 글이 설치를 가리킨다.
      let wrapper = scripts.appendingPathComponent(DevelopmentEngineInvocation.scriptName)
      guard fileManager.isExecutableFile(atPath: wrapper.path) else {
        return .unavailable(
          "개발 엔진 래퍼(\(DevelopmentEngineInvocation.scriptName))가 실행 가능하지 않다"
            + " — macos/scripts/install-development-engine.sh로 설치하세요: \(wrapper.path)"
        )
      }
      return .development(try development(scripts, externalJob))
    }
    return .unavailable(
      "실행 가능한 엔진이 없다 — Debug에서는 개발 스크립트와 외부 작업 폴더가 필요하다"
    )
  }
}

/// 엔진 세션의 실행 경계 — 테스트는 이 프로토콜을 가짜로 바꾼다.
public protocol EngineExecuting: Sendable {
  func run(_ plan: EngineLaunchPlan) async throws -> Int32
}

public struct EngineSessionContext {
  public let operation: DevelopmentEngineOperation
  public let jobID: String
  public let appGroupJob: URL
  public let externalJob: URL?
  public let plan: EngineLaunchPlan
  public let selectionRevision: String
  /// 없으면 선택 드리프트 게이트가 없다 — FCP 선택에 묶이지 않는 job(자막, S4B).
  public let registry: ActiveSelectionRegistry?
  public let now: () -> String

  public init(
    operation: DevelopmentEngineOperation,
    jobID: String,
    appGroupJob: URL,
    externalJob: URL?,
    plan: EngineLaunchPlan,
    selectionRevision: String,
    registry: ActiveSelectionRegistry?,
    now: @escaping () -> String = { ISO8601DateFormatter().string(from: Date()) }
  ) {
    self.operation = operation
    self.jobID = jobID
    self.appGroupJob = appGroupJob
    self.externalJob = externalJob
    self.plan = plan
    self.selectionRevision = selectionRevision
    self.registry = registry
    self.now = now
  }

  /// 엔진이 실제로 돌고 있는 job 폴더 — 개발 계획이면 외부, 아니면 App Group(S1A-R1).
  ///
  /// 취소 기록의 리비전 근원은 이 경로다: 번들 실행의 흔적은 App Group workspace에,
  /// 개발 실행의 흔적은 외부 job workspace에 남는다.
  public var runningJobURL: URL {
    if case .development = plan, let externalJob {
      return externalJob
    }
    return appGroupJob
  }
}

public enum EngineSessionOutcome: Equatable, Sendable {
  /// 엔진이 0으로 끝났고 결과를 동기화했다(roughcut이면 import도 허용된 상태).
  case completed
  /// 엔진 종료코드가 0이 아니다 — 진단 증거만 동기화하고 실패로 닫는다(성공 산물·import 없음).
  case engineFailed(Int32)
  /// 엔진이 exit 3으로 물러났다 — 같은 job이 이미 다른 곳에서 도는 중이다(S2A-1).
  /// 성공도 실패 시도도 아니므로 어떤 결과도 동기화·가져오기·import를 하지 않는다.
  case alreadyRunning
  /// 실행 전에 취소 기록이 있었다 — 엔진을 부르지 않았다.
  case cancelledBeforeRun
  /// 실행은 끝났지만 sync 전 취소/드리프트 — 결과를 가져오지 않았다.
  case stoppedBeforeSync
  /// import 직전 취소/드리프트 — 결과는 동기화됐지만 Final Cut에 보내지 않았다.
  case stoppedBeforeImport
  /// 활성 프로젝트가 바뀌었다 — 취소 기록(active_project_changed)을 남겼다.
  case staleSelection
  /// 실행기가 없다(Release에서 번들 엔진 부재 등).
  case unavailable(String)

  /// 엔진 진입점이 중복 소유권으로 예약한 종료 코드다(S2A-1).
  public static let alreadyRunningExitCode: Int32 = 3
}

/// 한 번의 엔진 실행을 감싸는 게이트 — 취소·드리프트를 매 단계 앞에서 확인한다.
///
/// 순서가 계약이다: (1) 실행 전 취소·드리프트 (2) 엔진 실행 (3) sync 전 재확인
/// (4) roughcut이면 import 전 재확인. 드리프트를 만나는 순간 CancellationRequest
/// (active_project_changed)를 App Group과 외부 작업 폴더 양쪽에 남긴다.
/// 엔진이 실패(종료코드 비0)로 끝나도 sync 게이트를 지난 뒤 **진단 증거만**
/// 가져온다 — 취소·드리프트는 여전히 아무것도 가져오지 않는다(S1A·S1B).
public struct EngineSessionRunner {
  public let executor: any EngineExecuting

  public init(executor: any EngineExecuting) {
    self.executor = executor
  }

  /// `nonisolated(nonsending)`(SE-0461) — 실행 전체가 **호출자의 격리 위에서**
  /// 돈다. 문맥(레지스트리 등 비-Sendable 재료)은 격리를 넘지 않고, 무거운
  /// 프로세스 대기만 실행기 안의 detached 작업으로 나간다. 취소·드리프트·
  /// 세션 계약과 서로 다른 job의 독립 실행은 그대로다.
  public nonisolated(nonsending) func run(
    _ context: EngineSessionContext,
    syncResults: @escaping (URL, URL, Bool) throws -> Void = { from, to, diagnosticsOnly in
      try DevelopmentJobStager.syncResults(from: from, to: to, diagnosticsOnly: diagnosticsOnly)
    },
    importRoughcut: (@Sendable (URL) async throws -> Void)? = nil,
    invocationID: String? = nil,
    progressRelay: EngineProgressRelay = EngineProgressRelay(),
    fileManager: FileManager = .default
  ) async throws -> EngineSessionOutcome {
    guard context.plan.isExecutable else {
      if case .unavailable(let reason) = context.plan {
        return .unavailable(reason)
      }
      return .unavailable("실행 계획이 없다")
    }
    if CancellationStore.isCancelled(jobURL: context.appGroupJob, fileManager: fileManager) {
      return .cancelledBeforeRun
    }
    if let registry = context.registry {
      do {
        try registry.requireCurrent(context.selectionRevision)
      } catch {
        try recordDriftCancellation(context, fileManager: fileManager)
        return .staleSelection
      }
    }
    // 진행 릴레이(S2A-3): 개발(외부) 실행 동안에만 돈다. 이 실행의 호출 신원을
    // 자식 argv에 심고, 그 신원이 실린 스냅샷만 옮긴다 — 실행기가 끝나거나
    // 취소·드리프트로 흐트러지면 과제를 끊고 합류한 뒤 원래의 종료를 지킨다.
    let relayID = invocationID ?? UUID().uuidString
    let ownedPlan = Self.planAppendingInvocationID(context.plan, invocationID: relayID)
    var relayTask: Task<Void, Never>?
    if let externalJob = context.externalJob {
      let appGroupJob = context.appGroupJob
      let driftProbe = context.registry.map {
        SelectionDriftProbe(registry: $0, revision: context.selectionRevision)
      }
      relayTask = progressRelay.start(
        externalJob: externalJob,
        appGroupJob: appGroupJob,
        invocationID: relayID,
        isStopped: {
          // 취소든 활성 선택 드리프트든 — 이 실행이 무효인 순간 관측 발행도 멈춘다.
          CancellationStore.isCancelled(jobURL: appGroupJob)
            || !(driftProbe?.isCurrent() ?? true)
        })
    }
    let status: Int32
    do {
      status = try await executor.run(ownedPlan)
    } catch {
      relayTask?.cancel()
      if let relayTask { _ = await relayTask.value }
      throw error
    }
    relayTask?.cancel()
    if let relayTask { _ = await relayTask.value }
    // 중복 소유권 종료(3)는 이 실행의 시도가 아니다(S2A-1). 실행기가 돌려준 순간
    // 어떤 읽기·쓰기보다 먼저 판정한다 — 정상 소유자가 그 사이에 취소됐어도 이
    // 요청의 결과는 alreadyRunning이고, 드리프트 기록·동기화는 없다. 릴레이는
    // 이미 끊혔다 — 지는 호출이 이긴 호출의 진행·결과를 흘려보내지 않는다.
    if status == EngineSessionOutcome.alreadyRunningExitCode {
      return .alreadyRunning
    }
    if CancellationStore.isCancelled(jobURL: context.appGroupJob, fileManager: fileManager) {
      return .stoppedBeforeSync
    }
    if let registry = context.registry {
      do {
        try registry.requireCurrent(context.selectionRevision)
      } catch {
        try recordDriftCancellation(context, fileManager: fileManager)
        return .staleSelection
      }
    }
    if let externalJob = context.externalJob {
      // 실패(비0)여도 증거는 나간다 — 단 진단 파일뿐이고 성공 산물은 없다.
      try syncResults(externalJob, context.appGroupJob, status != 0)
    }
    if status != 0 {
      return .engineFailed(status)
    }
    guard context.operation == .roughcut, let importRoughcut else { return .completed }
    if CancellationStore.isCancelled(jobURL: context.appGroupJob, fileManager: fileManager) {
      return .stoppedBeforeImport
    }
    if let registry = context.registry {
      do {
        try registry.requireCurrent(context.selectionRevision)
      } catch {
        try recordDriftCancellation(context, fileManager: fileManager)
        return .staleSelection
      }
    }
    try await importRoughcut(
      context.appGroupJob.appendingPathComponent("roughcut.fcpxml")
    )
    return .completed
  }

  /// 자식 argv에 이 실행의 호출 신원을 심는다 — 계획의 나머지는 그대로다.
  /// 신원은 잠금 소유자만 기록에 실으므로, 이 값을 받은 자식이 실제로 소유권을
  /// 얻었을 때만 릴레이가 그 진행을 옮긴다(S2A-3).
  static func planAppendingInvocationID(
    _ plan: EngineLaunchPlan, invocationID: String
  ) -> EngineLaunchPlan {
    let flag = DevelopmentEngineInvocation.invocationIDArgument
    switch plan {
    case .bundled(let invocation):
      return .bundled(
        BundledEngineInvocation(
          executableURL: invocation.executableURL,
          arguments: invocation.arguments + [flag, invocationID],
          currentDirectory: invocation.currentDirectory,
          environment: invocation.environment))
    case .development(let invocation):
      return .development(
        DevelopmentEngineInvocation(
          executableURL: invocation.executableURL,
          arguments: invocation.arguments + [flag, invocationID],
          credentialEnvelope: invocation.credentialEnvelope))
    case .unavailable(let reason):
      return .unavailable(reason)
    }
  }

  /// 드리프트 취소 기록 — 리비전은 실행 경로에서 묶는다(S1A-R1).
  ///
  /// 실행 workspace에 리비전이 없으면(receive 전) `RunningJobCancellation`이
  /// 스테이징된 input.fcpxml의 해시로 job에 묶는다 — input.fcpxml마저 없을 때만
  /// all-zero로 남고, 그때도 엔진은 다른 리비전으로 무시하지만 앱 쪽 게이트
  /// (재실행·sync·import)는 파일 존재로 닫힌다.
  func recordDriftCancellation(
    _ context: EngineSessionContext, fileManager: FileManager
  ) throws {
    let cancellation = RunningJobCancellation(
      appGroupJob: context.appGroupJob,
      externalJob: context.externalJob,
      runningJob: context.runningJobURL
    )
    try cancellation.record(
      reason: "active_project_changed",
      requestedAt: context.now(),
      fileManager: fileManager
    )
  }

  /// 엔진이 남긴 revision.jsonl(ProjectRevision)의 계산 리비전 — 없으면 nil.
  public static func stagedInputRevision(
    job: URL, fileManager: FileManager
  ) -> String? {
    let path = job.appendingPathComponent("workspace").appendingPathComponent("revision.jsonl")
    guard let line = try? String(contentsOf: path, encoding: .utf8),
      let firstLine = line.split(separator: "\n").first,
      let data = String(firstLine).data(using: .utf8),
      let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let payload = envelope["payload"] as? [String: Any]
    else { return nil }
    return payload["revision"] as? String
  }
}
