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
    try Data(ProtocolCodec.encode(request, type: "CaptionRequest").utf8)
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
}
