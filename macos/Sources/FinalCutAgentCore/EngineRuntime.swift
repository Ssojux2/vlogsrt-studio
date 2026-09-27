import Foundation

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
    if let engineRoot, EngineLaunchMode.resolve(bundleURL: engineRoot, fileManager: fileManager)
      == .bundled
    {
      let invocation = try? BundledEngineInvocation.create(
        operation: operation,
        jobID: jobID,
        jobDirectory: appGroupJob,
        engineRoot: engineRoot,
        apiKey: apiKey
      )
      if let invocation {
        return .bundled(invocation)
      }
    }
    guard configuration == .debug else {
      return .unavailable(
        "Release 빌드는 번들 엔진으로만 실행한다 — Resources/Engine이 없다(개발 폴백 없음)"
      )
    }
    if let scripts = applicationScriptsDirectory, let externalJob = developmentExternalJob {
      let invocation = try? DevelopmentEngineInvocation.create(
        operation: operation,
        jobID: jobID,
        externalJob: externalJob,
        applicationScriptsDirectory: scripts
      )
      if let invocation {
        return .development(invocation)
      }
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
  public let registry: ActiveSelectionRegistry
  public let now: () -> String

  public init(
    operation: DevelopmentEngineOperation,
    jobID: String,
    appGroupJob: URL,
    externalJob: URL?,
    plan: EngineLaunchPlan,
    selectionRevision: String,
    registry: ActiveSelectionRegistry,
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

  public func run(
    _ context: EngineSessionContext,
    syncResults: @escaping (URL, URL, Bool) throws -> Void = { from, to, diagnosticsOnly in
      try DevelopmentJobStager.syncResults(from: from, to: to, diagnosticsOnly: diagnosticsOnly)
    },
    importRoughcut: (@Sendable (URL) async throws -> Void)? = nil,
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
    do {
      try context.registry.requireCurrent(context.selectionRevision)
    } catch {
      try recordDriftCancellation(context, fileManager: fileManager)
      return .staleSelection
    }
    let status = try await executor.run(context.plan)
    if CancellationStore.isCancelled(jobURL: context.appGroupJob, fileManager: fileManager) {
      return .stoppedBeforeSync
    }
    do {
      try context.registry.requireCurrent(context.selectionRevision)
    } catch {
      try recordDriftCancellation(context, fileManager: fileManager)
      return .staleSelection
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
    do {
      try context.registry.requireCurrent(context.selectionRevision)
    } catch {
      try recordDriftCancellation(context, fileManager: fileManager)
      return .staleSelection
    }
    try await importRoughcut(
      context.appGroupJob.appendingPathComponent("roughcut.fcpxml")
    )
    return .completed
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
