import Foundation
import CryptoKit

public struct RationalTime: Codable, Equatable, Sendable {
  public let value: Int64
  public let timescale: Int32

  public init(value: Int64, timescale: Int32) {
    self.value = value
    self.timescale = timescale
  }

  public var secondsText: String {
    let divisor = Self.greatestCommonDivisor(abs(value), Int64(timescale))
    return "\(value / divisor)/\(Int64(timescale) / divisor)"
  }

  private static func greatestCommonDivisor(_ left: Int64, _ right: Int64) -> Int64 {
    var a = left
    var b = right
    while b != 0 {
      (a, b) = (b, a % b)
    }
    return max(a, 1)
  }
}

public struct ActiveProjectSelection: Codable, Equatable, Sendable {
  public let projectUID: String
  public let projectName: String
  public let timelineStart: RationalTime
  public let duration: RationalTime
  public let frameDuration: RationalTime

  public init(
    projectUID: String,
    projectName: String,
    timelineStart: RationalTime,
    duration: RationalTime,
    frameDuration: RationalTime
  ) {
    self.projectUID = projectUID
    self.projectName = projectName
    self.timelineStart = timelineStart
    self.duration = duration
    self.frameDuration = frameDuration
  }

  public static func validated(
    projectUID: String,
    projectName: String,
    timelineStart: RationalTime,
    duration: RationalTime,
    frameDuration: RationalTime
  ) throws -> Self {
    let selection = Self(
      projectUID: projectUID,
      projectName: projectName,
      timelineStart: timelineStart,
      duration: duration,
      frameDuration: frameDuration
    )
    try selection.requireValid()
    return selection
  }

  public func requireValid() throws {
    guard !projectUID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      throw ActiveProjectError.missingActiveProjectUID
    }
    guard !projectName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      throw ActiveProjectError.emptyProjectName
    }
    guard timelineStart.timescale > 0, timelineStart.value >= 0 else {
      throw ActiveProjectError.invalidTimelineTime("timelineStart")
    }
    guard duration.timescale > 0, duration.value > 0 else {
      throw ActiveProjectError.invalidTimelineTime("duration")
    }
    guard frameDuration.timescale > 0, frameDuration.value > 0 else {
      throw ActiveProjectError.invalidTimelineTime("frameDuration")
    }
  }
}

public struct ValidatedProjectInput: Equatable, Sendable {
  public let projectUID: String
  public let projectName: String
}

public struct ProjectSelectionMessage: Codable, Equatable, Sendable {
  public let projectUID: String
  public let projectName: String
  public let timelineStart: String
  public let duration: String
  public let frameDuration: String
  public let selectionRevision: String

  public static func create(_ selection: ActiveProjectSelection) throws -> Self {
    try selection.requireValid()
    let payload = ProjectSelectionDigestPayload(
      projectUID: selection.projectUID,
      projectName: selection.projectName,
      timelineStart: selection.timelineStart.secondsText,
      duration: selection.duration.secondsText,
      frameDuration: selection.frameDuration.secondsText
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    let digest = SHA256.hash(data: try encoder.encode(payload)).map {
      String(format: "%02x", $0)
    }.joined()
    return Self(
      projectUID: payload.projectUID,
      projectName: payload.projectName,
      timelineStart: payload.timelineStart,
      duration: payload.duration,
      frameDuration: payload.frameDuration,
      selectionRevision: digest
    )
  }

  enum CodingKeys: String, CodingKey {
    case projectUID = "project_uid"
    case projectName = "project_name"
    case timelineStart = "timeline_start"
    case duration
    case frameDuration = "frame_duration"
    case selectionRevision = "selection_revision"
  }
}

private struct ProjectSelectionDigestPayload: Encodable {
  let projectUID: String
  let projectName: String
  let timelineStart: String
  let duration: String
  let frameDuration: String

  enum CodingKeys: String, CodingKey {
    case projectUID = "project_uid"
    case projectName = "project_name"
    case timelineStart = "timeline_start"
    case duration
    case frameDuration = "frame_duration"
  }
}

public enum FCPXMLProjectGate {
  public static func validate(
    _ data: Data,
    activeProject: ActiveProjectSelection?
  ) throws -> ValidatedProjectInput {
    guard let activeProject else { throw ActiveProjectError.noActiveProject }
    try activeProject.requireValid()
    let document: XMLDocument
    do {
      document = try IdentityRoundTripService.parse(data)
    } catch {
      throw ActiveProjectError.invalidXML(String(describing: error))
    }
    let projects = (try? document.nodes(forXPath: "//project")) ?? []
    guard projects.count == 1 else { throw ActiveProjectError.projectCount(projects.count) }
    guard let element = projects[0] as? XMLElement else {
      throw ActiveProjectError.projectCount(0)
    }
    guard
      let uid = element.attribute(forName: "uid")?.stringValue?.trimmingCharacters(
        in: .whitespacesAndNewlines
      ),
      !uid.isEmpty
    else { throw ActiveProjectError.missingXMLProjectUID }
    guard uid == activeProject.projectUID else {
      throw ActiveProjectError.projectUIDMismatch(expected: activeProject.projectUID, actual: uid)
    }
    guard
      let name = element.attribute(forName: "name")?.stringValue?.trimmingCharacters(
        in: .whitespacesAndNewlines
      ),
      !name.isEmpty
    else { throw ActiveProjectError.emptyProjectName }
    return ValidatedProjectInput(projectUID: uid, projectName: name)
  }
}

public enum ActiveProjectError: Error, Equatable, LocalizedError {
  case noActiveProject
  case missingActiveProjectUID
  case missingXMLProjectUID
  case emptyProjectName
  case invalidTimelineTime(String)
  case projectCount(Int)
  case projectUIDMismatch(expected: String, actual: String)
  case invalidXML(String)

  public var errorDescription: String? {
    switch self {
    case .noActiveProject:
      "Final Cut 타임라인에 열린 프로젝트가 없습니다"
    case .missingActiveProjectUID:
      "활성 프로젝트 UID가 없습니다"
    case .missingXMLProjectUID:
      "FCPXML 프로젝트 UID가 없습니다"
    case .emptyProjectName:
      "프로젝트 이름이 비어 있습니다"
    case .invalidTimelineTime(let field):
      "활성 프로젝트의 \(field) 값이 올바르지 않습니다"
    case .projectCount(let count):
      "FCPXML에는 프로젝트가 정확히 하나여야 합니다 (현재 \(count)개)"
    case .projectUIDMismatch(let expected, let actual):
      "타임라인 프로젝트 UID(\(expected))와 FCPXML UID(\(actual))가 다릅니다"
    case .invalidXML(let reason):
      "FCPXML을 해석할 수 없습니다: \(reason)"
    }
  }
}
