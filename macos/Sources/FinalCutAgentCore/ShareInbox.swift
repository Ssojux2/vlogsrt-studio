import CryptoKit
import Foundation

public struct SharedExport: Equatable, Sendable {
  public let audio: URL
  public let fcpxml: URL
}

public enum ShareInbox {
  public static func classify(_ urls: [URL]) throws -> SharedExport {
    let audio = urls.filter { $0.pathExtension.lowercased() == "wav" }
    let xml = urls.filter { ["fcpxml", "fcpxmld"].contains($0.pathExtension.lowercased()) }
    guard audio.count == 1, xml.count == 1 else {
      throw ShareInboxError.expectedOneAudioAndOneFCPXML(
        audioCount: audio.count,
        xmlCount: xml.count
      )
    }
    return SharedExport(audio: audio[0], fcpxml: xml[0])
  }

  /// 공유 받기를 승인하기 전에 돈이 되는 입력만 검사한다 — WAV+FCPXML 결합.
  ///
  /// 누락·중복은 `classify`가, 0바이트·48kHz 아님·PCM 아님은 여기가 잡는다.
  /// 오디오-타임라인 길이 불일치는 엔진이 FCPXML duration과 대조해 실패시킨다
  /// (final_caption.py). 여기서 거부된 공유는 작업 폴더로 들어오지 않는다.
  public static func validate(_ export: SharedExport) throws {
    let resolvedXML = try FCPXMLDocument.resolve(export.fcpxml)
    for url in [export.audio, resolvedXML] {
      let size = try FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int
      guard let size, size > 0 else {
        throw ShareInboxError.emptyFile(url.lastPathComponent)
      }
    }
    try validateWAVHeader(export.audio)
  }

  /// RIFF/WAVE 헤더에서 압축 형식과 샘플 레이트를 본다 — 48kHz PCM만 받는다.
  static func validateWAVHeader(_ url: URL) throws {
    let file = try FileHandle(forReadingFrom: url)
    defer { try? file.close() }
    guard let header = try file.read(upToCount: 44), header.count == 44 else {
      throw ShareInboxError.truncatedWAVHeader
    }
    let bytes = [UInt8](header)
    guard Array(bytes[0...3]) == Array("RIFF".utf8), Array(bytes[8...11]) == Array("WAVE".utf8)
    else {
      throw ShareInboxError.notAWAVFile
    }
    // fmt 청크: 압축 형식(1=PCM), 채널 수, 샘플 레이트.
    guard Array(bytes[12...15]) == Array("fmt ".utf8) else {
      throw ShareInboxError.truncatedWAVHeader
    }
    let format = UInt16(bytes[20]) | (UInt16(bytes[21]) << 8)
    let rate = UInt32(bytes[24]) | (UInt32(bytes[25]) << 8)
      | (UInt32(bytes[26]) << 16) | (UInt32(bytes[27]) << 24)
    guard format == 1 else {
      throw ShareInboxError.notPCM(format)
    }
    guard rate == 48_000 else {
      throw ShareInboxError.sampleRate(rate)
    }
  }

  /// 공유 받기 하나를 잡으로 만드는 단일 경계 — 검증이 통과해야 잡이 생긴다.
  ///
  /// classify → validate → job 생성 → 스테이징 순서가 계약이다: WAV·FCPXML 둘
  /// 중 하나라도 유효하지 않으면 예외로 닫히고 **잡 디렉터리 자체가 없다**.
  /// 반쪽 job이 남는 경로는 없다.
  @discardableResult
  public static func receive(
    urls: [URL],
    workspace: AppGroupWorkspace,
    jobID: String,
    originRevision: String,
    fileManager: FileManager = .default
  ) throws -> URL {
    let export = try classify(urls)
    try validate(export)
    let job = try workspace.createJob(id: jobID, fileManager: fileManager)
    return try stageCaptionRequest(
      export, originRevision: originRevision, in: job, fileManager: fileManager
    )
  }

  public static func stage(
    _ export: SharedExport,
    in job: URL,
    fileManager: FileManager = .default
  ) throws -> SharedExport {
    let sourceXML = try FCPXMLDocument.resolve(export.fcpxml)
    let audio = job.appendingPathComponent("final.wav")
    let xml = job.appendingPathComponent("final.fcpxml")
    for (source, target) in [(export.audio, audio), (sourceXML, xml)] {
      if fileManager.fileExists(atPath: target.path) {
        throw ShareInboxError.destinationExists(target)
      }
      try fileManager.copyItem(at: source, to: target)
    }
    return SharedExport(audio: audio, fcpxml: xml)
  }

  public static func stageCaptionRequest(
    _ export: SharedExport,
    originRevision: String,
    in job: URL,
    fileManager: FileManager = .default
  ) throws -> URL {
    let staged = try stage(export, in: job, fileManager: fileManager)
    let xmlData = try Data(contentsOf: staged.fcpxml)
    let digest = SHA256.hash(data: xmlData).map { String(format: "%02x", $0) }.joined()
    let audioData = try Data(contentsOf: staged.audio)
    let audioDigest = SHA256.hash(data: audioData).map { String(format: "%02x", $0) }.joined()
    let request = CaptionRequest(
      originRevision: originRevision,
      finalRevision: digest,
      renderAudio: staged.audio.path,
      renderAudioSHA256: audioDigest,
      finalFCPXML: staged.fcpxml.path
    )
    let requestURL = job.appendingPathComponent("caption-request.jsonl")
    try Data(
      ProtocolCodec.encode(request, type: "CaptionRequest", version: ProtocolCodec.captionVersion)
        .utf8
    )
    .write(to: requestURL, options: .atomic)
    return requestURL
  }
}

public enum FCPXMLDocument {
  public static func resolve(_ url: URL) throws -> URL {
    if url.pathExtension.lowercased() == "fcpxmld" {
      let info = url.appendingPathComponent("Info.fcpxml")
      guard FileManager.default.isReadableFile(atPath: info.path) else {
        throw ShareInboxError.missingBundleInfo(info)
      }
      return info
    }
    guard url.pathExtension.lowercased() == "fcpxml" else {
      throw ShareInboxError.unsupportedFCPXML(url)
    }
    return url
  }
}

public enum ShareInboxError: Error {
  case expectedOneAudioAndOneFCPXML(audioCount: Int, xmlCount: Int)
  case destinationExists(URL)
  case missingBundleInfo(URL)
  case unsupportedFCPXML(URL)
  case emptyFile(String)
  case truncatedWAVHeader
  case notAWAVFile
  case notPCM(UInt16)
  case sampleRate(UInt32)
}
