import Foundation

/// extension이 container에 보내는 왕복 요청. ProtocolCodec 1.0 JSONL envelope.
public struct RoundTripRequest: Codable, Equatable, Sendable {
  public let jobID: String
  public let inputFileName: String
  public let outputFileName: String

  public init(jobID: String, inputFileName: String, outputFileName: String) {
    self.jobID = jobID
    self.inputFileName = inputFileName
    self.outputFileName = outputFileName
  }

  enum CodingKeys: String, CodingKey {
    case jobID = "job_id"
    case inputFileName = "input_file_name"
    case outputFileName = "output_file_name"
  }
}

/// container가 기록하는 왕복 결과. 성공은 출력 파일명을, 실패는 오류 문장을
/// 담는다 — 성공에 오류가 있거나 실패에 오류가 없으면 만들 수 없다.
public struct RoundTripResult: Codable, Equatable, Sendable {
  public let jobID: String
  public let status: RoundTripStatus
  public let outputFileName: String?
  public let error: String?

  public init(
    jobID: String,
    status: RoundTripStatus,
    outputFileName: String?,
    error: String?
  ) throws {
    switch status {
    case .roundtripReady:
      guard outputFileName != nil, error == nil else {
        throw RoundTripResultError.invalidPayload("roundtrip_ready")
      }
    case .failed:
      guard error != nil else {
        throw RoundTripResultError.invalidPayload("failed")
      }
    case .received, .roundtripRequested:
      // Result는 종결 상태만 담는다.
      throw RoundTripResultError.invalidPayload(status.rawValue)
    }
    self.jobID = jobID
    self.status = status
    self.outputFileName = outputFileName
    self.error = error
  }

  enum CodingKeys: String, CodingKey {
    case jobID = "job_id"
    case status
    case outputFileName = "output_file_name"
    case error
  }
}

public enum RoundTripResultError: Error, Equatable {
  case invalidPayload(String)
}
