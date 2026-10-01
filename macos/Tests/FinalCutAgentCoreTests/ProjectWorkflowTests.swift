import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  FCP 프로젝트 워크플로 회귀 — 직접 프로젝트 UID 흐름·원본 무변형·
//  라이브러리 권한 불일치 폐쇄·SRT 번들 옆 배달·수동 가져오기 안내
// ─────────────────────────────────────────────────────────────

private func projectXML(uid: String, name: String = "브이로그") -> Data {
  Data(
    """
    <?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\
    <fcpxml version="1.14"><resources/>\
    <library><event name="이벤트"><project name="\(name)" uid="\(uid)">\
    <sequence format="r1" duration="4s" tcStart="0s"><spine/>\
    </sequence></project></event></library></fcpxml>
    """.utf8)
}

private func activeSelection(uid: String) -> ActiveProjectSelection {
  ActiveProjectSelection(
    projectUID: uid,
    projectName: "브이로그",
    timelineStart: RationalTime(value: 0, timescale: 600),
    duration: RationalTime(value: 2400, timescale: 600),
    frameDuration: RationalTime(value: 100, timescale: 3000)
  )
}

private func writeShare(root: URL, uid: String) throws -> URL {
  let incoming = root.appendingPathComponent("incoming", isDirectory: true)
  try FileManager.default.createDirectory(at: incoming, withIntermediateDirectories: true)
  let xml = incoming.appendingPathComponent("export.fcpxml")
  try projectXML(uid: uid).write(to: xml)
  return xml
}

// ─── 직접 프로젝트 UID 흐름 — 인박스 매칭과 job 시작 ────────────────

@Test func roughCutInboxStagesTheCurrentProjectExportByUID() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let xml = try writeShare(root: root, uid: "SOURCE-UID-1")

  // 스테이징만 한다 — job·분석 기록은 남지 않는다.
  let entry = try RoughCutInbox.stage(urls: [xml], workspace: workspace)
  #expect(entry.projectUID == "SOURCE-UID-1")
  #expect(try RoughCutInbox.entry(forProjectUID: "SOURCE-UID-1", workspace: workspace) != nil)
  #expect(try RoughCutInbox.entry(forProjectUID: "OTHER", workspace: workspace) == nil)
  #expect(
    !FileManager.default.fileExists(
      atPath: root.appendingPathComponent("Jobs").appendingPathComponent("roundtrip-1").path))
}

@Test func roughCutIntakeStartsAJobFromActiveContextWithoutTouchingTheSource() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let workspace = AppGroupWorkspace(rootURL: root)
  let xml = try writeShare(root: root, uid: "SOURCE-UID-1")
  let sourceBytes = try Data(contentsOf: xml)
  _ = try RoughCutInbox.stage(urls: [xml], workspace: workspace)

  // 활성 프로젝트 문맥으로 job이 시작된다 — 드래그·직접 선택은 없다.
  let job = try RoughCutIntake.startJob(
    activeProject: activeSelection(uid: "SOURCE-UID-1"), workspace: workspace)
  #expect(
    FileManager.default.fileExists(atPath: job.inputURL.path))
  #expect(
    FileManager.default.fileExists(atPath: job.projectSelectionURL.path))
  #expect(try Data(contentsOf: job.inputURL) == sourceBytes)
  // 원본 소스는 바이트 하나 바뀌지 않는다.
  #expect(try Data(contentsOf: xml) == sourceBytes)
  // 인박스는 소비됐다 — 같은 내보내기의 이중 job이 없다.
  #expect(try RoughCutInbox.entries(workspace: workspace).isEmpty)

  // 활성 프로젝트의 받은 내보내기가 없으면 폐쇄다.
  #expect(throws: RoughCutInboxError.self) {
    _ = try RoughCutIntake.startJob(
      activeProject: activeSelection(uid: "SOURCE-UID-1"), workspace: workspace)
  }

  // UID가 다른 프로젝트가 활성이면 그 내보내기를 못 쓴다 — 게이트가 닫는다.
  let other = try writeShare(root: root, uid: "SOMEONE-ELSE")
  _ = try RoughCutInbox.stage(urls: [other], workspace: workspace)
  #expect(throws: RoughCutInboxError.self) {
    _ = try RoughCutIntake.startJob(
      activeProject: activeSelection(uid: "SOMEONE-ELSE-2"), workspace: workspace)
  }
}

// ─── 라이브러리 부모 권한 — 불일치·번들 사실 검증은 폐쇄로 ──────────

private func grantFixture() throws -> (
  root: URL, libraryParent: URL, workspace: AppGroupWorkspace
) {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  let parent = root.appendingPathComponent("Movies", isDirectory: true)
  try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
  try FileManager.default.createDirectory(
    at: parent.appendingPathComponent("Vlog.fcpbundle"), withIntermediateDirectories: true)
  return (root, parent, AppGroupWorkspace(rootURL: root))
}

@Test func libraryGrantValidatesOnlyTheGrantedParentWithOneBundle() throws {
  let fixture = try grantFixture()
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let grant = LibraryGrant(
    rootPath: fixture.libraryParent.path, bundleName: "Vlog.fcpbundle",
    grantedAt: "2026-10-01T00:00:00Z")
  try LibraryGrantStore.save(grant, workspace: fixture.workspace)

  // 북마크가 같은 폴더를 가리키면 유효하다.
  let valid = try LibraryGrantStore.validate(
    workspace: fixture.workspace, bookmarkedRoot: fixture.libraryParent)
  #expect(valid == grant)

  // 북마크가 다른 폴더를 가리키면 불일치 폐쇄다 — 조용히 다른 자리에 쓰지 않는다.
  let elsewhere = fixture.root.appendingPathComponent("elsewhere", isDirectory: true)
  try FileManager.default.createDirectory(at: elsewhere, withIntermediateDirectories: true)
  #expect(throws: LibraryGrantError.self) {
    _ = try LibraryGrantStore.validate(
      workspace: fixture.workspace, bookmarkedRoot: elsewhere)
  }

  // 북마크가 없어도 폐쇄다.
  #expect(throws: LibraryGrantError.self) {
    _ = try LibraryGrantStore.validate(workspace: fixture.workspace, bookmarkedRoot: nil)
  }

  // 번들 사실 — 없거나 여러 개면 그 자리는 라이브러리 부모가 아니다.
  #expect(throws: LibraryGrantError.self) {
    _ = try LibraryGrantStore.bundleName(inRoot: fixture.root.path)
  }
  try FileManager.default.createDirectory(
    at: fixture.libraryParent.appendingPathComponent("Other.fcpbundle"),
    withIntermediateDirectories: true)
  #expect(throws: LibraryGrantError.self) {
    _ = try LibraryGrantStore.bundleName(inRoot: fixture.libraryParent.path)
  }
}

// ─── SRT 배달 — 번들 옆, 원자적, 자동 가져오기 없음 ────────────────

@Test func srtDeliveryPlacesTheFileBesideTheBundleAtomically() throws {
  let fixture = try grantFixture()
  defer { try? FileManager.default.removeItem(at: fixture.root) }
  let job = try RoundTripJob(workspace: fixture.workspace, id: "caption-1")
  let captions = job.captionsDirectoryURL
  try FileManager.default.createDirectory(at: captions, withIntermediateDirectories: true)
  try Data("1\n00:00:01,000 --> 00:00:02,000\n첫 문장\n".utf8)
    .write(to: captions.appendingPathComponent("프로젝트.ko.srt"))

  let grant = LibraryGrant(
    rootPath: fixture.libraryParent.path, bundleName: "Vlog.fcpbundle",
    grantedAt: "2026-10-01T00:00:00Z")
  let placed = try SRTDelivery.placeBesideBundle(job: job, grant: grant)
  // 번들 **옆** — 같은 폴더, 번들 안이 아니다.
  #expect(placed.deletingLastPathComponent().path == fixture.libraryParent.path)
  #expect(placed.lastPathComponent == "프로젝트.ko.srt")
  #expect(
    FileManager.default.fileExists(
      atPath: fixture.libraryParent.appendingPathComponent("Vlog.fcpbundle/프로젝트.ko.srt")
        .path) == false)
  // 배달 기록이 job에 남는다 — 가져오기는 하지 않는다(안내 문구가 패널에 있다).
  #expect(
    FileManager.default.fileExists(
      atPath: job.directoryURL.appendingPathComponent(SRTDelivery.deliveryRecordName).path))
  // 원본은 job의 captions/ 에 그대로 산다(사본을 놓는다).
  #expect(FileManager.default.fileExists(atPath: captions.appendingPathComponent("프로젝트.ko.srt").path))

  // 배달할 SRT가 없으면 폐쇄다.
  try FileManager.default.removeItem(at: placed)
  try FileManager.default.removeItem(at: captions.appendingPathComponent("프로젝트.ko.srt"))
  #expect(throws: SRTDeliveryError.self) {
    _ = try SRTDelivery.placeBesideBundle(job: job, grant: grant, fileManager: .default)
  }
}

// ─── 새 에이전트 URL — 권한 요청과 배달 요청 ──────────────────────

@Test func agentURLCarriesTheLibraryGrantAndDeliveryHosts() throws {
  let grant = try AgentURL.libraryGrantURL()
  #expect(grant.absoluteString == "vlogsrt-agent://library-grant/grant")
  #expect(try AgentURL.request(grant) == .libraryGrant)
  #expect(throws: AgentURLError.self) {
    _ = try AgentURL.request(URL(string: "vlogsrt-agent://library-grant/other")!)
  }
  let deliver = try AgentURL.captionDeliverURL(jobID: "caption-1")
  #expect(deliver.absoluteString == "vlogsrt-agent://caption-deliver/caption-1")
  #expect(try AgentURL.request(deliver) == .captionDeliver("caption-1"))
}
