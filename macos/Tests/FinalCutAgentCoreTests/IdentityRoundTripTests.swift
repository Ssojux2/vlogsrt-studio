import Foundation
import Testing

@testable import FinalCutAgentCore

private let libraryWrappedFCPXML = """
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE fcpxml>
  <fcpxml version="1.9">
    <resources>
      <format id="r1" name="FFVideoFormat1080p30" frameDuration="100/3000s" width="1920" height="1080"/>
      <asset id="r2" name="beach" uid="A1" start="0s" duration="3000/3000s" hasVideo="1" format="r1">
        <media-rep kind="original-media" src="file:///tmp/beach.mov"/>
      </asset>
    </resources>
    <library>
      <event name="브이로그 이벤트">
        <project name="원본 프로젝트" uid="P1" modDate="2026-01-01 00:00:00 UTC">
          <sequence format="r1" duration="3000/3000s" tcStart="0s" tcFormat="NDF">
            <spine>
              <asset-clip name="beach" ref="r2" offset="0s" duration="3000/3000s"/>
            </spine>
          </sequence>
        </project>
      </event>
    </library>
  </fcpxml>
  """

private let directlyNestedFCPXML = """
  <?xml version="1.0" encoding="UTF-8"?>
  <fcpxml version="1.10">
    <import-options>
      <option key="language" value="ko"/>
    </import-options>
    <project name="타임라인 내보내기" uid="P2">
      <sequence format="r1" duration="10s">
        <spine/>
      </sequence>
    </project>
  </fcpxml>
  """

@Test func roundTripRewritesLibraryWrappedProject() throws {
  let pair = try tryRoundTrip(libraryWrappedFCPXML, jobID: "roundtrip-lib")
  let project = try findProject(in: pair.outputDocument)
  #expect(project.attribute(forName: "name")?.stringValue == "AI Roundtrip · 원본 프로젝트")
  #expect(project.attribute(forName: "uid") == nil)
  #expect(pair.inputBytesAfter == pair.inputBytesBefore)
}

@Test func roundTripAcceptsProjectWithoutLibraryWrapper() throws {
  let pair = try tryRoundTrip(directlyNestedFCPXML, jobID: "roundtrip-direct")
  let project = try findProject(in: pair.outputDocument)
  #expect(project.attribute(forName: "name")?.stringValue == "AI Roundtrip · 타임라인 내보내기")
  #expect(project.attribute(forName: "uid") == nil)
}

@Test func roundTripPreservesEverythingBeyondNameAndUID() throws {
  let pair = try tryRoundTrip(libraryWrappedFCPXML, jobID: "roundtrip-keep")
  let before = try XMLDocument(data: pair.inputBytesBefore)
  let differences = semanticDifferences(before.rootElement(), pair.outputDocument.rootElement())
  #expect(differences.isEmpty)
  // 의미 보존은 곧 멱등성이다: 결과를 다시 변환하면 이름만 한 번 더 감싸진다.
  let twice = try findProject(in: pair.outputDocument).attribute(forName: "name")?.stringValue
  #expect(twice == "AI Roundtrip · 원본 프로젝트")
}

@Test func roundTripRejectsProjectCountProblems() throws {
  let noProject = "<fcpxml version=\"1.9\"><resources/></fcpxml>"
  #expect(throws: RoundTripError.self) {
    _ = try tryRoundTrip(noProject, jobID: "roundtrip-zero")
  }
  let twoProjects = """
    <fcpxml version="1.9">
      <library><event name="e">
        <project name="하나"><sequence format="r1"><spine/></sequence></project>
        <project name="둘"><sequence format="r1"><spine/></sequence></project>
      </event></library>
    </fcpxml>
    """
  #expect(throws: RoundTripError.self) {
    _ = try tryRoundTrip(twoProjects, jobID: "roundtrip-two")
  }
}

@Test func roundTripRejectsEmptyAndMalformedInputs() throws {
  #expect(throws: RoundTripError.self) {
    _ = try tryRoundTrip(
      "<fcpxml version=\"1.9\"><library><event name=\"e\"><project name=\"\"/></event></library></fcpxml>",
      jobID: "roundtrip-empty"
    )
  }
  #expect(throws: RoundTripError.self) {
    _ = try tryRoundTrip("<fcpxml version=\"1.9\"><library", jobID: "roundtrip-malformed")
  }
}

@Test func roundTripRejectsEntityDeclarations() throws {
  let withEntity = """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE fcpxml [<!ENTITY bad "value">]>
    <fcpxml version="1.9">
      <library><event name="e"><project name="이름"/></event></library>
    </fcpxml>
    """
  #expect(throws: RoundTripError.self) {
    _ = try tryRoundTrip(withEntity, jobID: "roundtrip-entity")
  }
  let withExternalDTD = """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE fcpxml SYSTEM "https://example.invalid/fcpxml.dtd">
    <fcpxml version="1.9">
      <library><event name="e"><project name="이름"/></event></library>
    </fcpxml>
    """
  #expect(throws: RoundTripError.self) {
    _ = try tryRoundTrip(withExternalDTD, jobID: "roundtrip-external")
  }
}

@Test func roundTripRejectsExistingOutputAndKeepsOriginal() throws {
  let root = try makeJobRoot(jobID: "roundtrip-exists")
  let workspace = AppGroupWorkspace(rootURL: root.workspaceRoot)
  let job = try RoundTripJob(workspace: workspace, id: "roundtrip-exists")
  let inputBytes = Data(libraryWrappedFCPXML.utf8)
  try inputBytes.write(to: job.inputURL)
  try Data("<stub/>".utf8).write(to: job.outputURL)
  defer { try? FileManager.default.removeItem(at: root.workspaceRoot) }

  let service = IdentityRoundTripService()
  #expect(throws: RoundTripError.self) {
    _ = try service.roundTrip(inputURL: job.inputURL, outputURL: job.outputURL)
  }
  #expect(try Data(contentsOf: job.inputURL) == inputBytes)
  #expect(try Data(contentsOf: job.outputURL) == Data("<stub/>".utf8))
}

@Test func roundTripDoesNotLoadExternalFileEntities() throws {
  // 외부 entity 로드 차단은 parse 옵션이 보장한다 — 검사가 아니라 옵션이다.
  // secret 파일을 entity로 참조해 두고, parse 결과에 그 내용이 없음을 증명한다.
  let secretRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: secretRoot, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: secretRoot) }
  let secret = secretRoot.appendingPathComponent("secret.txt")
  let marker = "SECRET-MARKER-DO-NOT-LEAK"
  try Data(marker.utf8).write(to: secret)

  let withExternalEntity = """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE fcpxml [<!ENTITY leak SYSTEM "\(secret.absoluteString)">]>
    <fcpxml version="1.9">
      <library><event name="e"><note>&leak;</note><project name="이름"><sequence format="r1"><spine/></sequence></project></event></library>
    </fcpxml>
    """
  // no-load 옵션이 보장하는 것: parse는 성공하되 secret 내용은 어디에도 없다.
  let document = try IdentityRoundTripService.parseXML(Data(withExternalEntity.utf8))
  #expect(!document.xmlString.contains(marker))

  let root = try makeJobRoot(jobID: "roundtrip-external-entity")
  let workspace = AppGroupWorkspace(rootURL: root.workspaceRoot)
  let job = try RoundTripJob(workspace: workspace, id: "roundtrip-external-entity")
  try Data(withExternalEntity.utf8).write(to: job.inputURL)
  defer { try? FileManager.default.removeItem(at: root.workspaceRoot) }

  #expect(throws: RoundTripError.self) {
    _ = try IdentityRoundTripService().roundTrip(
      inputURL: job.inputURL,
      outputURL: job.outputURL
    )
  }
}

// ─── helpers ────────────────────────────────────────────────

private struct JobRoot {
  let workspaceRoot: URL
}

private func makeJobRoot(jobID: String) throws -> JobRoot {
  let workspaceRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let workspace = AppGroupWorkspace(rootURL: workspaceRoot)
  _ = try workspace.createJob(id: jobID)
  return JobRoot(workspaceRoot: workspaceRoot)
}

private func tryRoundTrip(_ xml: String, jobID: String) throws -> (
  inputBytesBefore: Data, inputBytesAfter: Data, outputDocument: XMLDocument
) {
  let root = try makeJobRoot(jobID: jobID)
  let workspace = AppGroupWorkspace(rootURL: root.workspaceRoot)
  let job = try RoundTripJob(workspace: workspace, id: jobID)
  let inputBytes = Data(xml.utf8)
  try inputBytes.write(to: job.inputURL)
  defer { try? FileManager.default.removeItem(at: root.workspaceRoot) }

  let service = IdentityRoundTripService()
  _ = try service.roundTrip(inputURL: job.inputURL, outputURL: job.outputURL)
  let outputBytes = try Data(contentsOf: job.outputURL)
  let outputDocument = try XMLDocument(data: outputBytes)
  return (inputBytes, try Data(contentsOf: job.inputURL), outputDocument)
}

private func findProject(in document: XMLDocument) throws -> XMLElement {
  let nodes = try document.nodes(forXPath: "//project")
  guard let project = nodes.first as? XMLElement else {
    throw RoundTripError.projectCount(nodes.count)
  }
  return project
}

/// project의 name·uid 속성만 빼고 두 트리가 같은지 재귀 비교한다.
private func semanticDifferences(_ a: XMLNode?, _ b: XMLNode?) -> [String] {
  guard let a, let b else { return (a == nil && b == nil) ? [] : ["한쪽만 nil"] }
  var differences: [String] = []
  if a.name != b.name { differences.append("요소 이름 \(a.name ?? "?") != \(b.name ?? "?")") }
  if let element = a as? XMLElement {
    let skipProjectIdentity = element.name == "project"
    let before = attributeMap(element, skipIdentity: skipProjectIdentity)
    let after = attributeMap(b as? XMLElement, skipIdentity: skipProjectIdentity)
    if before != after { differences.append("\(element.name ?? "?") 속성 \(before) != \(after)") }
  }
  let aText = a.kind == .text ? a.stringValue ?? "" : ""
  let bText = b.kind == .text ? b.stringValue ?? "" : ""
  if aText != bText { differences.append("텍스트 \(aText) != \(bText)") }
  let aChildren = a.children ?? []
  let bChildren = b.children ?? []
  if aChildren.count != bChildren.count {
    differences.append("자식 수 \(aChildren.count) != \(bChildren.count) (\(a.name ?? "?"))")
    return differences
  }
  for (childA, childB) in zip(aChildren, bChildren) {
    differences.append(contentsOf: semanticDifferences(childA, childB))
  }
  return differences
}

private func attributeMap(_ element: XMLElement?, skipIdentity: Bool) -> [String: String] {
  guard let element else { return [:] }
  var map: [String: String] = [:]
  for attribute in element.attributes ?? [] {
    guard let name = attribute.name else { continue }
    if skipIdentity && (name == "name" || name == "uid") { continue }
    map[name] = attribute.stringValue ?? ""
  }
  return map
}
