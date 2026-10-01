import Foundation

// ─────────────────────────────────────────────────────────────
//  STAB-S4B 패널 시대 — 자막 흐름의 패널 모델
//
//  확장 패널의 자막 동작을 파일 경계로 옮긴 것: 받은 내보내기(job 스테이징)·
//  승인(추정에 묶인 문서)·재시도(같은 승인)·상태 관측. 패널 뷰는 이 모델의
//  결과를 여는 것뿐이고 **유료 실행은 오직 명시적 동작에서만** 열린다:
//
//  - 인박스 스테이징(호스트)은 아무 것도 실행하지 않는다.
//  - `requestEstimate`는 추정 요청만 돌려준다(추정은 무키·무료다).
//  - `authorizeRun`/`retryRun`만 실행 요청을 돌려주고, 재시도는 이미 승인된
//    문서를 그대로 둔다(엔진 저널이 승인 상태를 유지한다, S4A).
// ─────────────────────────────────────────────────────────────

/// 패널 동작의 결과 — 패널이 열 URL(호스트 앱이 실행기다) 또는 없음.
public enum CaptionPanelAction: Equatable, Sendable {
  case none
  case openCaptionEstimate(jobID: String)
  case openCaptionRun(jobID: String)

  /// 열 URL — 동작이 없으면 nil(패널이 조용히 상태만 갱신한다).
  public var url: URL? {
    switch self {
    case .none: return nil
    case .openCaptionEstimate(let jobID): return try? AgentURL.captionEstimateURL(jobID: jobID)
    case .openCaptionRun(let jobID): return try? AgentURL.captionRunURL(jobID: jobID)
    }
  }
}

/// 패널이 보여줄 자막 상태 — 관측은 읽기만 한다.
public enum CaptionPanelPhase: Equatable, Sendable {
  /// 활성 프로젝트에 받은 내보내기가 없다(공유 메뉴로 시작한다).
  case idle
  /// 활성 프로젝트의 내보내기가 인박스에 있다 — 추정 요청을 기다린다.
  case intakeReady(projectUID: String)
  /// 추정(또는 실행)이 호스트에서 진행 중이다.
  case running(jobID: String)
  /// 추정이 고정됐다 — 길이·상한을 보여주고 승인을 기다린다.
  case estimateReady(audioSeconds: String, costUpperBound: String)
  /// 실행 성공 — 발화 없음(성공 상태, SRT는 없다).
  case succeededNoSpeech
  /// 실행 성공 — SRT·경고·수동 가져오기 안내.
  case succeededSrt(fileName: String, warnings: [String])
  /// 실행 실패 — 같은 승인으로 다시 시도할 수 있다.
  case failed(reason: String)
  /// 진행 불가 — 원본 매핑이 없거나 모호하다(재시도로 풀리지 않는다).
  case blocked(reason: String)

  public var userFacingText: String {
    switch self {
    case .idle:
      "자막: 대기 중 — 최종 편집 프로젝트에서 공유 메뉴(AI Captions)로 WAV+FCPXML을 보내세요"
    case .intakeReady(let uid):
      "자막: 최종 내보내기 받음(\(uid.prefix(12))…) — '자막 추정 요청'으로 시작하세요"
    case .running(let jobID):
      "자막 실행 중: \(jobID)"
    case .estimateReady(let seconds, let cost):
      "자막 추정: 오디오 \(seconds)초 · 상한 $\(cost) · 승인 대기"
    case .succeededNoSpeech:
      "자막 완료: 발화 없음(성공)"
    case .succeededSrt(let name, let warnings):
      warnings.isEmpty
        ? "자막 완료: \(name) — File > Import > Captions → Relative to Timeline"
        : "자막 완료: \(name) · 경고 \(warnings.count)건: \(warnings.joined(separator: " · "))"
          + " — File > Import > Captions → Relative to Timeline"
    case .failed(let reason):
      "자막 실패: \(reason) — 같은 승인으로 '자막 재시도'를 누를 수 있습니다"
    case .blocked(let reason):
      reason
    }
  }
}

public enum CaptionPanelError: Error, Equatable, LocalizedError {
  case noIntakeForActiveProject
  case originUnresolved(projectUID: String)
  case noEstimate(jobID: String)
  case noAuthorization(jobID: String)

  public var errorDescription: String? {
    switch self {
    case .noIntakeForActiveProject:
      "활성 프로젝트의 받은 내보내기가 없다 — 공유 메뉴로 먼저 보내주세요"
    case .originUnresolved(let uid):
      "프로젝트 \(uid.prefix(12))…의 원본 러프컷 연결이 없다 — 러프컷 가져오기 확인을 먼저 끝내주세요"
    case .noEstimate(let jobID):
      "자막 추정 기록이 없다: \(jobID) — 추정 요청부터 다시 하세요"
    case .noAuthorization(let jobID):
      "승인 문서가 없다: \(jobID) — 승인 없이는 실행하지 않는다"
    }
  }
}

@MainActor
public final class CaptionPanelModel {
  public let workspace: AppGroupWorkspace

  public init(workspace: AppGroupWorkspace) {
    self.workspace = workspace
  }

  /// 받은 내보내기를 job으로 스테이징하고 **추정 요청만** 돌려준다.
  ///
  /// 원본 매핑(UID → 러프컷 job/리비전)이 없거나 모호하면 job을 만들지 않고
  /// 닫는다 — 인박스 항목은 그대로 남아 매핑이 생긴 뒤 다시 시도할 수 있다.
  public func requestEstimate(activeProjectUID: String) throws -> CaptionPanelAction {
    guard let entry = try CaptionInbox.entry(
      forProjectUID: activeProjectUID, workspace: workspace)
    else { throw CaptionPanelError.noIntakeForActiveProject }
    let mapping = OriginMapStore(workspace: workspace)
    guard let origin = try mapping.lookup(projectUID: entry.projectUID) else {
      throw CaptionPanelError.originUnresolved(projectUID: entry.projectUID)
    }
    guard let export = CaptionInbox.stagedExport(for: entry, workspace: workspace) else {
      throw CaptionPanelError.noIntakeForActiveProject
    }
    let jobID = "caption-\(UUID().uuidString)"
    let jobURL = try workspace.createJob(id: jobID)
    _ = try ShareInbox.stageCaptionOrigin(export, origin: origin, in: jobURL)
    try CaptionInbox.consume(projectUID: entry.projectUID, workspace: workspace)
    return .openCaptionEstimate(jobID: jobID)
  }

  /// 추정을 승인하고 실행을 연다 — 승인 문서는 방금 읽은 추정에 정확히 묶인다.
  public func authorizeRun(job: RoundTripJob) throws -> CaptionPanelAction {
    let estimate = try job.readCaptionEstimate()
    try job.writeCaptionAuthorization(
      CaptionAuthorizationMessage(
        requestRevision: estimate.requestRevision,
        estimateRevision: estimate.estimateRevision,
        pricingSnapshotRevision: estimate.pricingSnapshotRevision,
        authorizedAt: ISO8601DateFormatter().string(from: Date())
      ))
    return .openCaptionRun(jobID: job.id)
  }

  /// 같은 유효 승인으로 다시 실행한다 — 승인 문서를 다시 쓰지 않는다.
  public func retryRun(job: RoundTripJob, fileManager: FileManager = .default) throws
    -> CaptionPanelAction
  {
    guard
      fileManager.fileExists(atPath: job.captionAuthorizationURL.path)
    else { throw CaptionPanelError.noAuthorization(jobID: job.id) }
    return .openCaptionRun(jobID: job.id)
  }

  /// 상태 관측(읽기만) — 인박스 폴링과 job 기록을 한 문에서 본다.
  ///
  /// jobID가 있으면 그 job의 기록이 우선이다(진행 중·완료·실패). 기록이 아직
  /// 없고 인박스에 활성 UID 항목이 있으면 받은 내보내기 대기다.
  public func observe(activeProjectUID: String?, job: RoundTripJob?) -> CaptionPanelPhase {
    if let job {
      // 실행 결과가 있으면 그것이 정본 — 성공한 무발화와 실패를 갈라 본다.
      if let result = job.readCaptionResultIfPresent() {
        if result.srtPath == nil { return .succeededNoSpeech }
        let name = ((result.srtPath ?? "") as NSString).lastPathComponent
        return .succeededSrt(fileName: name.isEmpty ? "SRT" : name, warnings: result.warnings)
      }
      if let estimate = try? job.readCaptionEstimate() {
        if FileManager.default.fileExists(atPath: job.captionAuthorizationURL.path) {
          // 승인 문서가 있는데 결과가 없다 — 실행 중이거나 실패 직전이다.
          if let attempt = job.readLastCaptionAttemptIfPresent(), attempt.status == "failed",
            let reason = attempt.error
          {
            return .failed(reason: reason)
          }
          return .running(jobID: job.id)
        }
        return .estimateReady(
          audioSeconds: estimate.audioSeconds, costUpperBound: estimate.costUpperBound)
      }
      return .running(jobID: job.id)
    }
    guard let uid = activeProjectUID, !uid.isEmpty,
      let entry = try? CaptionInbox.entry(forProjectUID: uid, workspace: workspace)
    else { return .idle }
    return .intakeReady(projectUID: entry.projectUID)
  }
}
