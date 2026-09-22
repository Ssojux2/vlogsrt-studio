import Foundation

/// FCPXML 왕복 최소 변환: project 이름을 `AI Roundtrip · <원본 이름>`으로
/// 바꾸고 project의 uid만 지운다. 그 밖의 요소·속성·텍스트·미디어 URI·시각은
/// 그대로 보존하고, 입력 파일은 절대 고치지 않는다.
public struct IdentityRoundTripService: Sendable {
  public static let renamedPrefix = "AI Roundtrip · "

  public init() {}

  public func roundTrip(
    inputURL: URL,
    outputURL: URL,
    fileManager: FileManager = .default
  ) throws {
    let input = try Data(contentsOf: inputURL)
    let document = try Self.parse(input)
    let project = try Self.singleProject(in: document)
    try Self.rewrite(project: project)
    try Self.publish(document, to: outputURL, fileManager: fileManager)
  }

  // 외부 entity 로드는 parse 호출 전에 옵션으로 차단한다 — 검사가 아니라
  // 옵션이 보장한다. entity 선언 자체는 parse 뒤에 거부한다.
  static func parse(_ data: Data) throws -> XMLDocument {
    let document = try parseXML(data)
    try rejectEntityDeclarations(document)
    return document
  }

  static func parseXML(_ data: Data) throws -> XMLDocument {
    do {
      return try XMLDocument(
        data: data,
        options: [.nodeLoadExternalEntitiesNever]
      )
    } catch {
      throw RoundTripError.malformedXML(String(describing: error))
    }
  }

  // DTD 선언은 허용하되 entity 선언과 외부 entity 로드는 거부한다.
  static func rejectEntityDeclarations(_ document: XMLDocument) throws {
    guard let dtd = document.dtd else { return }
    let hasEntity = (dtd.children ?? []).contains { node in
      guard let declaration = node as? XMLDTDNode else { return false }
      // 외부(SYSTEM/PUBLIC) entity는 dtdKind가 0(미초기화)로 오므로
      // isExternal·systemID로 잡는다. 내부 entity는 종류로 잡는다.
      if declaration.isExternal || declaration.systemID != nil {
        return true
      }
      switch declaration.dtdKind {
      case .general, .parsed, .unparsed, .parameter:
        return true
      default:
        return false
      }
    }
    if hasEntity {
      throw RoundTripError.entityDeclarationRejected
    }
    if dtd.systemID != nil || dtd.publicID != nil {
      throw RoundTripError.externalDTDRejected
    }
  }

  static func singleProject(in document: XMLDocument) throws -> XMLNode {
    let projects = (try? document.nodes(forXPath: "//project")) ?? []
    guard projects.count == 1 else {
      throw RoundTripError.projectCount(projects.count)
    }
    return projects[0]
  }

  static func rewrite(project: XMLNode) throws {
    guard let element = project as? XMLElement else {
      throw RoundTripError.projectCount(0)
    }
    guard
      let nameAttribute = element.attribute(forName: "name"),
      let originalName = nameAttribute.stringValue,
      !originalName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    else {
      throw RoundTripError.emptyProjectName
    }
    nameAttribute.stringValue = Self.renamedPrefix + originalName
    element.removeAttribute(forName: "uid")
  }

  static func publish(
    _ document: XMLDocument,
    to outputURL: URL,
    fileManager: FileManager
  ) throws {
    let output = document.xmlData
    guard !fileManager.fileExists(atPath: outputURL.path) else {
      throw RoundTripError.outputExists(outputURL)
    }
    let tempURL = outputURL.deletingLastPathComponent()
      .appendingPathComponent(".\(UUID().uuidString).roundtrip.tmp")
    do {
      try output.write(to: tempURL, options: .atomic)
      try fileManager.moveItem(at: tempURL, to: outputURL)
    } catch {
      try? fileManager.removeItem(at: tempURL)
      throw error
    }
  }
}

public enum RoundTripError: Error, Equatable {
  case malformedXML(String)
  case entityDeclarationRejected
  case externalDTDRejected
  case projectCount(Int)
  case emptyProjectName
  case outputExists(URL)
}
