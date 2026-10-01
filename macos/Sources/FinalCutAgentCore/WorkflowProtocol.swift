import Foundation

public enum ProtocolCodec {
  public static let version = "1.0"
  public static let analysisVersion = "1.3"
  /// 1.2·1.1 기록도 읽는다 — 결과 상태가 없는 판정은 legacy/unknown로만 취급한다.
  public static let priorAnalysisVersion = "1.2"
  public static let legacyAnalysisVersion = "1.1"
  public static let captionVersion = "1.1"
  public static let analysisVersions = [analysisVersion, priorAnalysisVersion, legacyAnalysisVersion]

  public static func encode<Payload: Encodable>(
    _ payload: Payload,
    type: String,
    version protocolVersion: String = version
  ) throws -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(
      Envelope(payload: payload, protocolVersion: protocolVersion, type: type)
    )
    guard let text = String(data: data, encoding: .utf8) else {
      throw ProtocolCodecError.invalidUTF8
    }
    return text + "\n"
  }

  /// 한 줄(JSONL)을 typed payload로 되돌린다. envelope의 protocol_version과
  /// type가 기대와 다르면 payload를 만들지 않는다.
  public static func decode<Payload: Decodable>(
    _ payload: Payload.Type,
    type: String,
    version expectedVersion: String = version,
    from line: String
  ) throws -> Payload {
    let envelope = try JSONDecoder().decode(
      DecodedEnvelope<Payload>.self,
      from: Data(line.utf8)
    )
    guard envelope.protocolVersion == expectedVersion else {
      throw ProtocolCodecError.unsupportedVersion(envelope.protocolVersion)
    }
    guard envelope.type == type else {
      throw ProtocolCodecError.typeMismatch(expected: type, actual: envelope.type)
    }
    return envelope.payload
  }

  /// 분석 프로토콜의 낡은 기록(1.1·1.2)도 읽는다 — 페이로드가 같은 종류는
  /// 버전만 바뀌어도 같은 payload로 복원한다(S1B).
  public static func decodeAnalysis<Payload: Decodable>(
    _ payload: Payload.Type,
    type: String,
    from line: String
  ) throws -> Payload {
    let envelope = try JSONDecoder().decode(
      DecodedEnvelope<Payload>.self,
      from: Data(line.utf8)
    )
    guard analysisVersions.contains(envelope.protocolVersion) else {
      throw ProtocolCodecError.unsupportedVersion(envelope.protocolVersion)
    }
    guard envelope.type == type else {
      throw ProtocolCodecError.typeMismatch(expected: type, actual: envelope.type)
    }
    return envelope.payload
  }
}

public struct PricingSnapshotMessage: Codable, Equatable, Sendable {
  public let effectiveDate: String
  public let currency: String
  public let processingRegion: String
  public let serviceTier: String
  public let lunaModel: String
  public let lunaInputPerMillionTokens: String
  public let lunaOutputPerMillionTokens: String
  public let whisperModel: String
  public let whisperPerMinute: String
  public let gptTranscribeModel: String
  public let gptTranscribePerMinute: String
  public let documentationURLs: [String]
  public let snapshotRevision: String

  enum CodingKeys: String, CodingKey {
    case effectiveDate = "effective_date"
    case currency
    case processingRegion = "processing_region"
    case serviceTier = "service_tier"
    case lunaModel = "luna_model"
    case lunaInputPerMillionTokens = "luna_input_per_million_tokens"
    case lunaOutputPerMillionTokens = "luna_output_per_million_tokens"
    case whisperModel = "whisper_model"
    case whisperPerMinute = "whisper_per_minute"
    case gptTranscribeModel = "gpt_transcribe_model"
    case gptTranscribePerMinute = "gpt_transcribe_per_minute"
    case documentationURLs = "documentation_urls"
    case snapshotRevision = "snapshot_revision"
  }
}

public struct AnalysisEstimateMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let model: String
  public let uniqueSourceAudioSeconds: String
  public let transcriptionSeconds: String
  public let maxWindows: Int
  public let maxImages: Int
  public let maxImageTokens: Int
  public let costUpperBound: String?
  public let settingsSHA256: String
  public let pricingSnapshotRevision: String
  public let mediaGrantRevision: String
  public let estimateRevision: String

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case model
    case uniqueSourceAudioSeconds = "unique_source_audio_seconds"
    case transcriptionSeconds = "transcription_seconds"
    case maxWindows = "max_windows"
    case maxImages = "max_images"
    case maxImageTokens = "max_image_tokens"
    case costUpperBound = "cost_upper_bound"
    case settingsSHA256 = "settings_sha256"
    case pricingSnapshotRevision = "pricing_snapshot_revision"
    case mediaGrantRevision = "media_grant_revision"
    case estimateRevision = "estimate_revision"
  }
}

public struct CancellationRequestMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let attemptID: String
  public let requestedAt: String
  /// `user` | `active_project_changed` — Python CancellationRequest.reason.
  public let reason: String
  public let cancellationRevision: String

  public init(
    inputRevision: String,
    attemptID: String,
    requestedAt: String,
    reason: String,
    cancellationRevision: String
  ) {
    self.inputRevision = inputRevision
    self.attemptID = attemptID
    self.requestedAt = requestedAt
    self.reason = reason
    self.cancellationRevision = cancellationRevision
  }

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case attemptID = "attempt_id"
    case requestedAt = "requested_at"
    case reason
    case cancellationRevision = "cancellation_revision"
  }
}

public struct ActualUsageMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let attemptID: String
  public let windowID: String?
  public let model: String
  public let inputTokens: Int
  public let outputTokens: Int
  public let audioSeconds: String
  public let cost: String
  public let usageRevision: String

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case attemptID = "attempt_id"
    case windowID = "window_id"
    case model
    case inputTokens = "input_tokens"
    case outputTokens = "output_tokens"
    case audioSeconds = "audio_seconds"
    case cost
    case usageRevision = "usage_revision"
  }
}

public struct RationalRangeMessage: Codable, Equatable, Sendable {
  public let start: String
  public let end: String
}

public struct CutCandidateMessage: Codable, Equatable, Sendable {
  public let candidateID: String
  public let inputRevision: String
  public let clipInstanceID: String
  public let sourceRange: RationalRangeMessage
  public let timelineRange: RationalRangeMessage
  public let reason: String
  public let evidence: [String]
  public let confidence: String
  public let windowID: String
  public let decisionRevision: String
  public let selectedByDefault: Bool

  enum CodingKeys: String, CodingKey {
    case candidateID = "candidate_id"
    case inputRevision = "input_revision"
    case clipInstanceID = "clip_instance_id"
    case sourceRange = "source_range"
    case timelineRange = "timeline_range"
    case reason
    case evidence
    case confidence
    case windowID = "window_id"
    case decisionRevision = "decision_revision"
    case selectedByDefault = "selected_by_default"
  }
}

public struct AnalysisAuthorizationMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let estimateRevision: String
  public let pricingSnapshotRevision: String
  public let mediaGrantRevision: String
  public let authorizedAt: String

  public init(
    inputRevision: String,
    estimateRevision: String,
    pricingSnapshotRevision: String,
    mediaGrantRevision: String,
    authorizedAt: String
  ) {
    self.inputRevision = inputRevision
    self.estimateRevision = estimateRevision
    self.pricingSnapshotRevision = pricingSnapshotRevision
    self.mediaGrantRevision = mediaGrantRevision
    self.authorizedAt = authorizedAt
  }

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case estimateRevision = "estimate_revision"
    case pricingSnapshotRevision = "pricing_snapshot_revision"
    case mediaGrantRevision = "media_grant_revision"
    case authorizedAt = "authorized_at"
  }
}

public struct CandidatePreviewMessage: Codable, Equatable, Sendable {
  public let candidateID: String
  public let inputRevision: String
  public let frameID: String
  public let thumbnailPath: String
  public let thumbnailSHA256: String
  public let reason: String
  public let confidence: String
  public let removalDuration: String
  public let previewRevision: String

  enum CodingKeys: String, CodingKey {
    case candidateID = "candidate_id"
    case inputRevision = "input_revision"
    case frameID = "frame_id"
    case thumbnailPath = "thumbnail_path"
    case thumbnailSHA256 = "thumbnail_sha256"
    case reason
    case confidence
    case removalDuration = "removal_duration"
    case previewRevision = "preview_revision"
  }
}

public struct CaptionEstimateMessage: Codable, Equatable, Sendable {
  public let requestRevision: String
  public let originRevision: String
  public let finalRevision: String
  public let renderAudioSHA256: String
  public let audioSeconds: String
  public let transcriptionSeconds: String
  public let costUpperBound: String
  public let pricingSnapshotRevision: String
  public let settingsSHA256: String
  public let estimateRevision: String

  enum CodingKeys: String, CodingKey {
    case requestRevision = "request_revision"
    case originRevision = "origin_revision"
    case finalRevision = "final_revision"
    case renderAudioSHA256 = "render_audio_sha256"
    case audioSeconds = "audio_seconds"
    case transcriptionSeconds = "transcription_seconds"
    case costUpperBound = "cost_upper_bound"
    case pricingSnapshotRevision = "pricing_snapshot_revision"
    case settingsSHA256 = "settings_sha256"
    case estimateRevision = "estimate_revision"
  }
}

public struct CaptionAuthorizationMessage: Codable, Equatable, Sendable {
  public let requestRevision: String
  public let estimateRevision: String
  public let pricingSnapshotRevision: String
  public let authorizedAt: String

  public init(
    requestRevision: String,
    estimateRevision: String,
    pricingSnapshotRevision: String,
    authorizedAt: String
  ) {
    self.requestRevision = requestRevision
    self.estimateRevision = estimateRevision
    self.pricingSnapshotRevision = pricingSnapshotRevision
    self.authorizedAt = authorizedAt
  }

  enum CodingKeys: String, CodingKey {
    case requestRevision = "request_revision"
    case estimateRevision = "estimate_revision"
    case pricingSnapshotRevision = "pricing_snapshot_revision"
    case authorizedAt = "authorized_at"
  }
}

/// 자막 실행의 결과(S4A) — 성공한 무발화와 실패한 전사가 갈라져 있다.
/// `srtPath`는 오직 `speech`일 때만 있고, 실패 결과는 앱이 시도 기록에서 본다.
public struct CaptionResultMessage: Codable, Equatable, Sendable {
  public let finalRevision: String
  public let srtPath: String?
  public let warnings: [String]
  public let quality: [String]
  public let speechStatus: String

  enum CodingKeys: String, CodingKey {
    case finalRevision = "final_revision"
    case srtPath = "srt_path"
    case warnings
    case quality
    case speechStatus = "speech_status"
  }
}

/// 자막 시도 하나의 상태(S4A) — `pending → running → 결과`의 내구 진행 기록.
/// 실패에만 오류 문면이 실리고, 그 외 상태는 오류를 가질 수 없다.
public struct CaptionAttemptMessage: Codable, Equatable, Sendable {
  public let requestRevision: String
  public let estimateRevision: String
  public let attemptID: String
  public let status: String
  public let startedAt: String
  public let error: String?

  enum CodingKeys: String, CodingKey {
    case requestRevision = "request_revision"
    case estimateRevision = "estimate_revision"
    case attemptID = "attempt_id"
    case status
    case startedAt = "started_at"
    case error
  }
}

private struct DecodedEnvelope<Payload: Decodable>: Decodable {
  let payload: Payload
  let protocolVersion: String
  let type: String

  enum CodingKeys: String, CodingKey {
    case payload
    case protocolVersion = "protocol_version"
    case type
  }
}

public struct CaptionRequest: Codable, Equatable, Sendable {
  public let originRevision: String
  public let finalRevision: String
  public let renderAudio: String
  public let renderAudioSHA256: String
  public let finalFCPXML: String
  public let insertionMethod: String

  public init(
    originRevision: String,
    finalRevision: String,
    renderAudio: String,
    renderAudioSHA256: String,
    finalFCPXML: String,
    insertionMethod: String = "relative"
  ) {
    self.originRevision = originRevision
    self.finalRevision = finalRevision
    self.renderAudio = renderAudio
    self.renderAudioSHA256 = renderAudioSHA256
    self.finalFCPXML = finalFCPXML
    self.insertionMethod = insertionMethod
  }

  enum CodingKeys: String, CodingKey {
    case originRevision = "origin_revision"
    case finalRevision = "final_revision"
    case renderAudio = "render_audio"
    case renderAudioSHA256 = "render_audio_sha256"
    case finalFCPXML = "final_fcpxml"
    case insertionMethod = "insertion_method"
  }
}

private struct Envelope<Payload: Encodable>: Encodable {
  let payload: Payload
  let protocolVersion: String
  let type: String

  enum CodingKeys: String, CodingKey {
    case payload
    case protocolVersion = "protocol_version"
    case type
  }
}

public enum ProtocolCodecError: Error, Equatable {
  case invalidUTF8
  case unsupportedVersion(String)
  case typeMismatch(expected: String, actual: String)
}

/// Python AnalysisAttempt 메시지 — 시도의 상태가 집계와 같은 job에 묶였는지의 근거.
public struct AnalysisAttemptMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let estimateRevision: String
  public let attemptID: String
  /// `pending` | `running` | `succeeded` | `failed` | `cancelled`
  public let status: String
  public let startedAt: String
  public let error: String?

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case estimateRevision = "estimate_revision"
    case attemptID = "attempt_id"
    case status
    case startedAt = "started_at"
    case error
  }
}

/// 상류(오디오·전사·VAD) 단계의 시도·실패·미적용 수 — Python UpstreamStages와 같은 모양.
/// skipped는 선언된 무오디오 등 "적용하지 않았다"다 — 성공도 실패도 아니다.
public struct UpstreamStageCounts: Codable, Equatable, Sendable {
  public let total: Int
  public let failed: Int
  public let skipped: Int
}

/// 소스 단위 수 — usable은 오디오·전사·VAD가 모두 성공한 소스의 수다.
public struct UpstreamSourceCounts: Codable, Equatable, Sendable {
  public let total: Int
  public let usable: Int
}

/// 시도 하나의 집계 — Python AnalysisResult 메시지(프로토콜 1.3).
public struct AnalysisResultMessage: Codable, Equatable, Sendable {
  public let inputRevision: String
  public let attemptID: String
  public let estimateRevision: String
  /// `success` | `partial_error` | `error`
  public let status: String
  public let windows: WindowCounts
  public let candidateCount: Int
  public let upstream: UpstreamCounts
  public let reasonCodes: [String]
  public let resultRevision: String

  public struct WindowCounts: Codable, Equatable, Sendable {
    public let total: Int
    public let successful: Int
    public let failed: Int
    public let skipped: Int
  }

  public struct UpstreamCounts: Codable, Equatable, Sendable {
    public let audio: UpstreamStageCounts
    public let transcription: UpstreamStageCounts
    public let vad: UpstreamStageCounts
    public let sources: UpstreamSourceCounts
  }

  enum CodingKeys: String, CodingKey {
    case inputRevision = "input_revision"
    case attemptID = "attempt_id"
    case estimateRevision = "estimate_revision"
    case status
    case windows
    case candidateCount = "candidate_count"
    case upstream
    case reasonCodes = "reason_codes"
    case resultRevision = "result_revision"
  }
}

/// 앱이 사용자에게 보여주는 분석 결과의 네 가지 얼굴 — 성공(후보 0건 포함)·
/// 부분 실패·전체 실패·알 수 없음(구형·낡은·중단·진행 중 기록). 알 수 없음은
/// 새 성공 증거도 재실행 근거도 아니다 — "모른다"로 보여줄 뿐이다. 종료 코드
/// 0만으로 전체 성공 요약을 내리지 않는다.
public enum AnalysisOutcome: Equatable, Sendable {
  case success(candidateCount: Int)
  case partial(candidateCount: Int, windowsFailed: Int, windowsSkipped: Int, reasons: [String])
  case failed(reasons: [String])
  case legacyUnknown

  public static func make(_ result: AnalysisResultMessage?) -> AnalysisOutcome {
    guard let result else { return .legacyUnknown }
    switch result.status {
    case "success":
      return AnalysisOutcome.success(candidateCount: result.candidateCount)
    case "partial_error":
      return AnalysisOutcome.partial(
        candidateCount: result.candidateCount,
        windowsFailed: result.windows.failed,
        windowsSkipped: result.windows.skipped,
        reasons: result.reasonCodes
      )
    default:
      return AnalysisOutcome.failed(reasons: result.reasonCodes)
    }
  }

  /// 네 상태가 각각 다르게 읽히는 사용자 문장 — 후보 0건 성공이 실패로
  /// 읽히거나 실패가 "완료"로 읽히면 안 된다.
  public var userFacingSummary: String {
    switch self {
    case .success(let candidateCount):
      candidateCount == 0
        ? "분석 완료 — 컷 후보 0건 (발화·후보 없음, 정상)"
        : "분석 완료 — 컷 후보 \(candidateCount)건"
    case .partial(let candidateCount, let windowsFailed, let windowsSkipped, let reasons):
      "분석 부분 실패 — 창 실패 \(windowsFailed)개·미처리 \(windowsSkipped)개"
        + " (사유: \(reasons.joined(separator: ", "))) · 실패한 구간은 보존"
        + (candidateCount == 0 ? " · 후보 0건" : " · 안전 후보 \(candidateCount)건 검토 가능")
    case .failed(let reasons):
      "분석 실패 — 성공한 창이 없다 (사유: \(reasons.joined(separator: ", "))) · 영상은 보존됨"
    case .legacyUnknown:
      "분석 상태를 알 수 없다 — 기록이 구형·낡았거나 진행 중·중단이다 · 자동 재실행하지 않는다"
    }
  }

  /// 지금 시도의 결과로 후보 검토 화면을 열어도 되는가 — 검토는 **검증된 지금
  /// success·partial 결과**에만 켠다. 옛 시도가 남긴 candidates 파일이 있어도
  /// 알 수 없음·실패는 검토 가능으로 보이지 않는다(S1B-R1).
  public static func shouldLoadCandidates(
    outcome: AnalysisOutcome, candidatesFileExists: Bool
  ) -> Bool {
    switch outcome {
    case .success, .partial: candidatesFileExists
    case .failed, .legacyUnknown: false
    }
  }

  /// 실패한 작업의 다음 요청이 어디로 가는가 — 같은 작업 재시도는 명시적이다.
  /// 첫 실패는 안내만 보여주고(재시도 가능), 사용자가 다시 누르면 엔진을 다시
  /// 부른다. 알 수 없음은 재실행하지 않는다 — 실패와 섞지 않는다.
  public static func retryRoute(outcome: AnalysisOutcome, retryArmed: Bool) -> AnalysisRetryRoute {
    switch outcome {
    case .failed:
      retryArmed ? .retryEngine : .showFailure(canRetry: true)
    case .legacyUnknown, .success, .partial:
      .noOutcome
    }
  }
}

/// 실패 뒤 사용자 요청의 행선지 — 재시도는 같은 작업에 대한 명시적 선택이다.
public enum AnalysisRetryRoute: Equatable, Sendable {
  case showFailure(canRetry: Bool)
  case retryEngine
  case noOutcome
}

/// job 기록의 셋 중 하나 — 새 작업과 모호한 기록을 갈라 말한다(S1B-R1).
/// `fresh`는 시도·집계·후보 기록이 전혀 없는 새 job이고, `unknown`은 기록은
/// 있으되 지금 job의 리비전·시도·상태와 묶을 수 없는 것이다 — 낡은 집계,
/// 진행 중·취소 시도, 없거나 깨진 추정/시도 기록이 모두 여기다.
public enum AnalysisRecordState: Equatable, Sendable {
  case fresh
  case valid(AnalysisResultMessage)
  case unknown
}

/// 검토 화면의 행선지 — 모호한 기록은 요약만 보여주고 정지한다(재실행 없음).
public enum AnalysisReviewRoute: Equatable, Sendable {
  /// 새 작업 — 승인·분석 요청 흐름으로 간다.
  case freshJob
  /// 검증된 success·partial — 후보 검토와 함께 요약을 보여준다.
  case reviewCandidates(AnalysisOutcome)
  /// 검증된 실패 — 안내만 보여준다(재시도는 사용자의 다음 누름).
  case showFailure(AnalysisOutcome)
  /// 검증된 실패의 명시적 재시도 — 엔진을 다시 부른다.
  case retryEngine
  /// 모호한 기록 — 정직한 unknown 요약을 보여주고 정지한다.
  case showUnknown
}

/// 기록 상태를 화면 행선으로 정규화하는 공유 라우터 — extension과 앱이 같은
/// 표를 쓴다. unknown은 어떤 경우도 엔진 호출·후보 검토로 가지 않는다.
public enum AnalysisReviewRouter {
  public static func route(
    record: AnalysisRecordState,
    candidatesFileExists: Bool,
    retryArmed: Bool
  ) -> AnalysisReviewRoute {
    switch record {
    case .fresh:
      return AnalysisReviewRoute.freshJob
    case .unknown:
      return AnalysisReviewRoute.showUnknown
    case .valid(let result):
      let outcome = AnalysisOutcome.make(result)
      switch outcome {
      case .success, .partial:
        return AnalysisReviewRoute.reviewCandidates(outcome)
      case .failed:
        return retryArmed ? .retryEngine : .showFailure(outcome)
      case .legacyUnknown:
        return AnalysisReviewRoute.showUnknown
      }
    }
  }
}
