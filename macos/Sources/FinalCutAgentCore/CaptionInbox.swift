import Foundation

// ─────────────────────────────────────────────────────────────
//  STAB-S4B 패널 시대 — 공유 받기의 내구 자막 인박스
//
//  Final Cut의 AI Captions 공유 메뉴는 이제 **스테이징만** 한다: 호스트 앱
//  (창 없는 실행기)가 WAV+FCPXML을 검증해 App Group의 `CaptionInbox/`에
//  최종 프로젝트 UID 하나당 한 벌로 남긴다. job도, 추정도, 유료 실행도
//  일어나지 않는다 — 흐름을 여는 것은 확장 패널의 명시적 동작뿐이다.
//
//  확장 패널은 이 인박스를 **폴링**해서 활성 프로젝트 UID에 묶인 항목을 찾는다:
//  사용자가 최종 편집 프로젝트를 열고 있을 때만 자기 내보내기가 보이고, 남의
//  프로젝트 항목은 조용히 기다린다(UID가 키다 — 마지막 job에 묻지 않는다).
// ─────────────────────────────────────────────────────────────

/// 인박스 한 항목의 내구 기록 — 폴더 안의 `entry.json`이다.
public struct CaptionInboxEntry: Codable, Equatable, Sendable {
  public let projectUID: String
  public let receivedAt: String

  public init(projectUID: String, receivedAt: String) {
    self.projectUID = projectUID
    self.receivedAt = receivedAt
  }

  enum CodingKeys: String, CodingKey {
    case projectUID = "project_uid"
    case receivedAt = "received_at"
  }
}

public enum CaptionInboxError: Error, Equatable, LocalizedError {
  case missingProjectUID
  case unreadableEntry(String)

  public var errorDescription: String? {
    switch self {
    case .missingProjectUID:
      "공유받은 FCPXML에 프로젝트 UID가 없다 — Final Cut에서 다시 내보내주세요"
    case .unreadableEntry(let path):
      "공유 받기 기록을 읽을 수 없다: \(path) — 다시 공유해주세요"
    }
  }
}

public enum CaptionInbox {
  public static let directoryName = "CaptionInbox"
  static let entryFileName = "entry.json"

  /// 공유 한 벌을 스테이징한다 — 검증이 통과해야 인박스에 들어간다.
  ///
  /// 같은 UID의 이전 내보내기는 새 것으로 덮인다(마지막 공유가 정본). 폴더 이름은
  /// UID의 안전한 조각이고 정본 UID는 `entry.json`에 있다. 이 문은 job을 만들지
  /// 않고 엔진을 부르지도 않는다 — 유료 실행으로 가는 길은 여기 없다.
  @discardableResult
  public static func stage(
    urls: [URL],
    workspace: AppGroupWorkspace,
    fileManager: FileManager = .default,
    now: @escaping () -> String = { ISO8601DateFormatter().string(from: Date()) }
  ) throws -> CaptionInboxEntry {
    let export = try ShareInbox.classify(urls)
    try ShareInbox.validate(export)
    let resolvedXML = try FCPXMLDocument.resolve(export.fcpxml)
    let xmlData = try Data(contentsOf: resolvedXML)
    guard let uid = ShareInbox.finalProjectUID(xmlData) else {
      throw CaptionInboxError.missingProjectUID
    }
    let inbox = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
    let destination = inbox.appendingPathComponent(safeDirectoryName(uid), isDirectory: true)
    try fileManager.createDirectory(at: inbox, withIntermediateDirectories: true)
    // 임시 폴더에 옮긴 뒤 자리바꿈치기 — 반쪽 벌이 인박스로 보이지 않는다.
    let staging = inbox.appendingPathComponent(
      ".staging-\(UUID().uuidString)", isDirectory: true)
    try fileManager.createDirectory(at: staging, withIntermediateDirectories: true)
    let audio = staging.appendingPathComponent("final.wav")
    let xml = staging.appendingPathComponent("final.fcpxml")
    try fileManager.copyItem(at: export.audio, to: audio)
    try fileManager.copyItem(at: resolvedXML, to: xml)
    let entry = CaptionInboxEntry(projectUID: uid, receivedAt: now())
    let entryData = try JSONEncoder().encode(entry)
    try entryData.write(to: staging.appendingPathComponent(entryFileName))
    if fileManager.fileExists(atPath: destination.path) {
      try fileManager.removeItem(at: destination)
    }
    try fileManager.moveItem(at: staging, to: destination)
    return entry
  }

  /// 인박스의 모든 항목 — 해석에 실패하는 항목이 하나라도 있으면 그 자체가
  /// 오류다(조용히 건너뛰는 받은 내보내기가 없게).
  public static func entries(
    workspace: AppGroupWorkspace, fileManager: FileManager = .default
  ) throws -> [CaptionInboxEntry] {
    let inbox = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
    let names = (try? fileManager.contentsOfDirectory(atPath: inbox.path)) ?? []
    var result: [CaptionInboxEntry] = []
    for name in names.sorted() {
      guard !name.hasPrefix(".") else { continue }
      let entryURL = inbox.appendingPathComponent(name, isDirectory: true)
        .appendingPathComponent(entryFileName)
      guard let data = try? Data(contentsOf: entryURL) else { continue }
      guard let entry = try? JSONDecoder().decode(CaptionInboxEntry.self, from: data) else {
        throw CaptionInboxError.unreadableEntry(entryURL.path)
      }
      result.append(entry)
    }
    return result
  }

  /// 활성 프로젝트 UID에 묶인 항목 — 폴링이 찾는 것.
  public static func entry(
    forProjectUID uid: String, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) throws -> CaptionInboxEntry? {
    try entries(workspace: workspace, fileManager: fileManager)
      .first { $0.projectUID == uid }
  }

  /// 항목의 스테이징된 파일 한 쌍 — job으로 옮길 때 쓴다.
  public static func stagedExport(
    for entry: CaptionInboxEntry, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) -> SharedExport? {
    let folder = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
      .appendingPathComponent(safeDirectoryName(entry.projectUID), isDirectory: true)
    let audio = folder.appendingPathComponent("final.wav")
    let xml = folder.appendingPathComponent("final.fcpxml")
    guard
      fileManager.fileExists(atPath: audio.path),
      fileManager.fileExists(atPath: xml.path)
    else { return nil }
    return SharedExport(audio: audio, fcpxml: xml)
  }

  /// 소비 표시 — job으로 옮겨간 항목은 인박스에서 사라진다(두 번 스테이징 금지).
  public static func consume(
    projectUID: String, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) throws {
    let folder = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
      .appendingPathComponent(safeDirectoryName(projectUID), isDirectory: true)
    if fileManager.fileExists(atPath: folder.path) {
      try fileManager.removeItem(at: folder)
    }
  }

  /// UID의 안전한 폴더 조각 — 경계·제어 문자는 '_'로, 빈 조각은 고정 글자로.
  static func safeDirectoryName(_ uid: String) -> String {
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
    let sanitized = String(
      uid.unicodeScalars.map { allowed.contains($0) ? Character($0) : "_" })
    return sanitized.isEmpty ? "unnamed" : sanitized
  }
}
