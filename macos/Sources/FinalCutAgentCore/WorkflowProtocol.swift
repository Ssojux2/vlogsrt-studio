import Foundation

public enum ProtocolCodec {
  public static let version = "1.0"

  public static func encode<Payload: Encodable>(
    _ payload: Payload,
    type: String
  ) throws -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(Envelope(payload: payload, protocolVersion: version, type: type))
    guard let text = String(data: data, encoding: .utf8) else {
      throw ProtocolCodecError.invalidUTF8
    }
    return text + "\n"
  }
}

public struct CaptionRequest: Codable, Equatable, Sendable {
  public let originRevision: String
  public let finalRevision: String
  public let renderAudio: String
  public let finalFCPXML: String
  public let insertionMethod: String

  public init(
    originRevision: String,
    finalRevision: String,
    renderAudio: String,
    finalFCPXML: String,
    insertionMethod: String = "relative"
  ) {
    self.originRevision = originRevision
    self.finalRevision = finalRevision
    self.renderAudio = renderAudio
    self.finalFCPXML = finalFCPXML
    self.insertionMethod = insertionMethod
  }

  enum CodingKeys: String, CodingKey {
    case originRevision = "origin_revision"
    case finalRevision = "final_revision"
    case renderAudio = "render_audio"
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

public enum ProtocolCodecError: Error {
  case invalidUTF8
}
