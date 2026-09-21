import Foundation

public enum ProtocolCodec {
  public static let version = "1.0"
  public static let analysisVersion = "1.1"

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
    from line: String
  ) throws -> Payload {
    let envelope = try JSONDecoder().decode(
      DecodedEnvelope<Payload>.self,
      from: Data(line.utf8)
    )
    guard envelope.protocolVersion == version else {
      throw ProtocolCodecError.unsupportedVersion(envelope.protocolVersion)
    }
    guard envelope.type == type else {
      throw ProtocolCodecError.typeMismatch(expected: type, actual: envelope.type)
    }
    return envelope.payload
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
