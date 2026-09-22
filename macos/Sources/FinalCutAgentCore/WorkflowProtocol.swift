import Foundation

public enum ProtocolCodec {
  public static let version = "1.0"
  public static let analysisVersion = "1.2"
  public static let legacyAnalysisVersion = "1.1"
  public static let captionVersion = "1.1"

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
