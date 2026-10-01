import Foundation

// ─────────────────────────────────────────────────────────────
//  FCP 프로젝트 워크플로 — AI Rough Cut 공유의 인박스와 job 시작
//
//  AI Rough Cut 공유 대상이 보낸 현재 프로젝트의 FCPXML을 UID 하나당 한 벌로
//  스테이징한다(호스트는 창 없는 실행기 — 스테이징 외에 아무 것도 하지 않는다).
//  패널은 활성 프로젝트 UID로 인박스를 폴링해 자기 내보내기를 찾고, job은
//  **활성 프로젝트 문맥**(project-selection)과 함께 만든다 — 드래그·직접
//  선택의 길은 없다. 원본 소스는 읽기만 한다(바이트를 하나도 고치지 않는다).
// ─────────────────────────────────────────────────────────────

/// 러프컷 인박스 한 항목의 내구 기록 — 폴더 안의 `entry.json`이다.
public struct RoughCutInboxEntry: Codable, Equatable, Sendable {
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

public enum RoughCutInboxError: Error, Equatable, LocalizedError {
  case missingProjectUID
  case unreadableEntry(String)
  case noIntakeForActiveProject
  case uidMismatch(expected: String, actual: String)

  public var errorDescription: String? {
    switch self {
    case .missingProjectUID:
      "공유받은 FCPXML에 프로젝트 UID가 없다 — Final Cut에서 다시 공유해주세요"
    case .unreadableEntry(let path):
      "공유 받기 기록을 읽을 수 없다: \(path) — 다시 공유해주세요"
    case .noIntakeForActiveProject:
      "활성 프로젝트의 받은 내보내기가 없다 — AI Rough Cut 공유 메뉴로 먼저 보내주세요"
    case .uidMismatch(let expected, let actual):
      "활성 프로젝트(\(expected.prefix(12))…)와 공유된 내보내기(\(actual.prefix(12))…)가"
        + " 다른 프로젝트다 — 그 프로젝트를 열고 다시 시도하세요"
    }
  }
}

public enum RoughCutInbox {
  public static let directoryName = "RoughCutInbox"
  static let entryFileName = "entry.json"
  static let inputFileName = "input.fcpxml"

  /// 공유된 FCPXML 한 벌을 스테이징한다 — job·분석·실행은 일어나지 않는다.
  ///
  /// 같은 UID의 이전 내보내기는 새 것으로 덮인다. 소스는 읽기만 하고 사본을
  /// 인박스에 남긴다(원본 수정의 길이 없다).
  @discardableResult
  public static func stage(
    urls: [URL],
    workspace: AppGroupWorkspace,
    fileManager: FileManager = .default,
    now: @escaping () -> String = { ISO8601DateFormatter().string(from: Date()) }
  ) throws -> RoughCutInboxEntry {
    let xml = urls.filter { ["fcpxml", "fcpxmld"].contains($0.pathExtension.lowercased()) }
    guard xml.count == 1 else {
      throw ShareInboxError.expectedOneAudioAndOneFCPXML(audioCount: 0, xmlCount: xml.count)
    }
    let resolved = try FCPXMLDocument.resolve(xml[0])
    let data = try Data(contentsOf: resolved)
    guard !data.isEmpty else { throw ShareInboxError.emptyFile(resolved.lastPathComponent) }
    guard let uid = ShareInbox.finalProjectUID(data) else {
      throw RoughCutInboxError.missingProjectUID
    }
    let inbox = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
    let destination = inbox.appendingPathComponent(
      CaptionInbox.safeDirectoryName(uid), isDirectory: true)
    try fileManager.createDirectory(at: inbox, withIntermediateDirectories: true)
    let staging = inbox.appendingPathComponent(
      ".staging-\(UUID().uuidString)", isDirectory: true)
    try fileManager.createDirectory(at: staging, withIntermediateDirectories: true)
    try data.write(to: staging.appendingPathComponent(inputFileName))
    let entry = RoughCutInboxEntry(projectUID: uid, receivedAt: now())
    try JSONEncoder().encode(entry).write(
      to: staging.appendingPathComponent(entryFileName))
    if fileManager.fileExists(atPath: destination.path) {
      try fileManager.removeItem(at: destination)
    }
    try fileManager.moveItem(at: staging, to: destination)
    return entry
  }

  public static func entries(
    workspace: AppGroupWorkspace, fileManager: FileManager = .default
  ) throws -> [RoughCutInboxEntry] {
    let inbox = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
    let names = (try? fileManager.contentsOfDirectory(atPath: inbox.path)) ?? []
    var result: [RoughCutInboxEntry] = []
    for name in names.sorted() {
      guard !name.hasPrefix(".") else { continue }
      let entryURL = inbox.appendingPathComponent(name, isDirectory: true)
        .appendingPathComponent(entryFileName)
      guard let data = try? Data(contentsOf: entryURL) else { continue }
      guard let entry = try? JSONDecoder().decode(RoughCutInboxEntry.self, from: data) else {
        throw RoughCutInboxError.unreadableEntry(entryURL.path)
      }
      result.append(entry)
    }
    return result
  }

  public static func entry(
    forProjectUID uid: String, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) throws -> RoughCutInboxEntry? {
    try entries(workspace: workspace, fileManager: fileManager)
      .first { $0.projectUID == uid }
  }

  /// 항목의 스테이징된 FCPXML 바이트 — 원본 소스가 아니라 인박스 사본이다.
  public static func stagedInput(
    for entry: RoughCutInboxEntry, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) -> Data? {
    let path = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
      .appendingPathComponent(CaptionInbox.safeDirectoryName(entry.projectUID), isDirectory: true)
      .appendingPathComponent(inputFileName)
    guard fileManager.fileExists(atPath: path.path) else { return nil }
    return try? Data(contentsOf: path)
  }

  public static func consume(
    projectUID: String, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) throws {
    let folder = workspace.rootURL.appendingPathComponent(directoryName, isDirectory: true)
      .appendingPathComponent(CaptionInbox.safeDirectoryName(projectUID), isDirectory: true)
    if fileManager.fileExists(atPath: folder.path) {
      try fileManager.removeItem(at: folder)
    }
  }
}

/// 인박스의 받은 내보내기를 활성 프로젝트 문맥으로 job로 시작하는 문(S4B 패널).
public enum RoughCutIntake {
  /// 활성 UID의 받은 내보내기를 job로 스테이징한다 — 원본 소스는 건드리지 않는다.
  ///
  /// 게이트는 인박스 사본과 활성 프로젝트의 UID 일치(`FCPXMLProjectGate`)까지
  /// 다시 잰다: 다른 프로젝트의 내보내기를 남의 job로 만드는 길이 없다.
  @discardableResult
  public static func startJob(
    activeProject: ActiveProjectSelection,
    workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) throws -> RoundTripJob {
    guard let entry = try RoughCutInbox.entry(
      forProjectUID: activeProject.projectUID, workspace: workspace, fileManager: fileManager)
    else { throw RoughCutInboxError.noIntakeForActiveProject }
    guard let data = RoughCutInbox.stagedInput(
      for: entry, workspace: workspace, fileManager: fileManager)
    else { throw RoughCutInboxError.noIntakeForActiveProject }
    _ = try FCPXMLProjectGate.validate(data, activeProject: activeProject)
    let jobID = "roundtrip-\(UUID().uuidString)"
    _ = try workspace.createJob(id: jobID, fileManager: fileManager)
    let job = try RoundTripJob(workspace: workspace, id: jobID)
    try job.writeProjectSelection(activeProject, fileManager: fileManager)
    try RoundTripJob.writeNewData(data, to: job.inputURL, fileManager: fileManager)
    try RoughCutInbox.consume(
      projectUID: entry.projectUID, workspace: workspace, fileManager: fileManager)
    return job
  }
}

extension RoundTripJob {
  /// 인박스 사본을 job 입력으로 쓰는 쓰기 보조 — 드래그 시절의 writeInput과 같은
  /// 계약(이미 있으면 덮어쓰지 않는다)에서 바이트만 인자로 받는다.
  static func writeNewData(_ data: Data, to url: URL, fileManager: FileManager) throws {
    guard !fileManager.fileExists(atPath: url.path) else {
      throw RoundTripJobError.destinationExists(url)
    }
    try data.write(to: url, options: [.atomic])
  }
}
