import Foundation

// ─────────────────────────────────────────────────────────────
//  FCP 프로젝트 워크플로 — 라이브러리 부모 권한과 SRT 배달
//
//  SRT는 Final Cut 라이브러리 번들(.fcpbundle) **옆에** 원자적으로 저장된다 —
//  번들 안이 아니고 자동 가져오기도 아니다(File > Import > Captions 안내뿐).
//  그 자리에 쓰려면 라이브러리의 부모 폴더가 쓰기 가능해야 한다: 첫 실행 권한
//  흐름이 사용자가 고른 그 폴더의 보안 북마크를 남기고, 배달은 매번 북마크와
//  폴더 사실(단일 .fcpbundle)이 어긋나지 않았는지 다시 잰다(어긋나면 폐쇄).
// ─────────────────────────────────────────────────────────────

/// 라이브러리 부모 권한의 내구 기록 — App Group 뿌리의 `library-grant.json`.
public struct LibraryGrant: Codable, Equatable, Sendable {
  public let rootPath: String
  public let bundleName: String
  public let grantedAt: String

  public init(rootPath: String, bundleName: String, grantedAt: String) {
    self.rootPath = rootPath
    self.bundleName = bundleName
    self.grantedAt = grantedAt
  }

  enum CodingKeys: String, CodingKey {
    case rootPath = "root_path"
    case bundleName = "bundle_name"
    case grantedAt = "granted_at"
  }
}

public enum LibraryGrantError: Error, Equatable, LocalizedError {
  case notGranted
  case mismatch(detail: String)
  case bundleNotFound(String)
  case ambiguousBundles(String)
  case targetInsideBundle(String)

  public var errorDescription: String? {
    switch self {
    case .notGranted:
      "라이브러리 폴더 권한이 없다 — 패널의 권한 단계에서 먼저 부여하세요"
    case .mismatch(let detail):
      "저장된 라이브러리 폴더 권한이 지금 사실과 어긋난다(\(detail))"
        + " — 권한을 다시 부여하세요"
    case .bundleNotFound(let root):
      "권한 폴더에 .fcpbundle가 없다: \(root) — 라이브러리가 있는 폴더인지 확인하세요"
    case .ambiguousBundles(let root):
      "권한 폴더에 .fcpbundle가 여러 개다: \(root) — 라이브러리 바로 위 폴더를 지정하세요"
    case .targetInsideBundle(let path):
      "SRT는 라이브러리 번들 안에 저장하지 않는다: \(path) — 번들 옆 자리여야 한다"
    }
  }
}

/// 권한 기록의 읽기·검증 — 북마크는 호스트 앱이 권한 흐름에서 남긴 것이다.
public enum LibraryGrantStore {
  public static let fileName = "library-grant.json"
  /// 권한 흐름이 호스트에 남기는 보안 북마크의 저장 키.
  public static let bookmarkKey = "library-parent"

  public static func fileURL(workspace: AppGroupWorkspace) -> URL {
    workspace.rootURL.appendingPathComponent(fileName)
  }

  public static func save(
    _ grant: LibraryGrant, workspace: AppGroupWorkspace,
    fileManager: FileManager = .default
  ) throws {
    let data = try JSONEncoder().encode(grant)
    try fileManager.createDirectory(
      at: workspace.rootURL, withIntermediateDirectories: true)
    try data.write(to: fileURL(workspace: workspace), options: [.atomic])
  }

  public static func loadIfPresent(
    workspace: AppGroupWorkspace, fileManager: FileManager = .default
  ) -> LibraryGrant? {
    guard let data = try? Data(contentsOf: fileURL(workspace: workspace)) else { return nil }
    return try? JSONDecoder().decode(LibraryGrant.self, from: data)
  }

  /// 권한의 현재 유효성 — 북마크·기록·폴더 사실이 모두 같아야 한다.
  ///
  /// 어긋남(북마크가 다른 폴더를 가리킴·루트가 사라짐·번들 목록 변동)은
  /// 폐쇄로 닫는다: 조용히 다른 폴더에 쓰는 길이 없다.
  public static func validate(
    workspace: AppGroupWorkspace,
    bookmarkedRoot: URL?,
    fileManager: FileManager = .default
  ) throws -> LibraryGrant {
    guard let grant = loadIfPresent(workspace: workspace, fileManager: fileManager) else {
      throw LibraryGrantError.notGranted
    }
    guard let bookmarkedRoot else {
      throw LibraryGrantError.mismatch(detail: "보안 북마크가 없다")
    }
    let canonicalBookmark = bookmarkedRoot.resolvingSymlinksInPath().standardizedFileURL.path
    let canonicalRecorded = URL(fileURLWithPath: grant.rootPath)
      .resolvingSymlinksInPath().standardizedFileURL.path
    guard canonicalBookmark == canonicalRecorded else {
      throw LibraryGrantError.mismatch(detail: "북마크가 기록된 폴더와 다르다")
    }
    var isDirectory: ObjCBool = false
    guard fileManager.fileExists(atPath: canonicalRecorded, isDirectory: &isDirectory),
      isDirectory.boolValue
    else {
      throw LibraryGrantError.mismatch(detail: "폴더가 그 자리에 없다")
    }
    _ = try bundleName(inRoot: canonicalRecorded, fileManager: fileManager)
    return grant
  }

  /// 루트 안의 단일 .fcpbundle 이름 — 없거나 여러 개면 폐쇄다.
  public static func bundleName(
    inRoot root: String, fileManager: FileManager = .default
  ) throws -> String {
    let names = ((try? fileManager.contentsOfDirectory(atPath: root)) ?? [])
      .filter { $0.hasSuffix(".fcpbundle") }
    guard names.count == 1, let name = names.first else {
      if names.isEmpty { throw LibraryGrantError.bundleNotFound(root) }
      throw LibraryGrantError.ambiguousBundles(root)
    }
    return name
  }
}

/// SRT 배달 실패 — job에 성공 SRT가 없다.
public enum SRTDeliveryError: Error, Equatable, LocalizedError {
  case noSRT(String)

  public var errorDescription: String? {
    switch self {
    case .noSRT(let path):
      "배달할 SRT가 없다: \(path) — 자막 실행을 먼저 끝내주세요"
    }
  }
}

/// 보안 스코프의 열기·닫기 주입점 — 배달 순서 시험이 가짜로 대체한다.
public struct LibraryScopeAccess: Sendable {
  public let start: @Sendable (URL) -> Bool
  public let stop: @Sendable (URL) -> Void

  public init(
    start: @escaping @Sendable (URL) -> Bool,
    stop: @escaping @Sendable (URL) -> Void
  ) {
    self.start = start
    self.stop = stop
  }

  /// 실물 — 북마크 루트의 보안 스코프를 건다.
  public static func live() -> LibraryScopeAccess {
    LibraryScopeAccess(
      start: { url in url.startAccessingSecurityScopedResource() },
      stop: { url in url.stopAccessingSecurityScopedResource() })
  }
}

/// SRT를 번들 옆에 원자적으로 놓는 문 — 자동 가져오기는 없다(안내 문구뿐).
public enum SRTDelivery {
  public static let deliveryRecordName = "srt-delivery.json"

  /// SRT 배달의 전체 문 — 북마크 스코프를 **먼저** 걸고 권한 검증과 복사가
  /// 그 안에서 일어나게 한다(어느 끝에서 실패해도 스코프는 닫는다).
  ///
  /// 순서가 계약이다: `LibraryGrantStore.validate`는 파일시스템 검사(폴더
  /// 있음·번들 목록)를 하는데, 샌드박스 안에서 권한 폴더는 스코프가 걸린
  /// 뒤에만 보인다. 검증을 먼저 하면 유효한 권한도 "폴더가 그 자리에 없다"로
  /// 폐쇄된다 — 스코프가 validate보다 앞서야 한다.
  @discardableResult
  public static func deliver(
    job: RoundTripJob,
    workspace: AppGroupWorkspace,
    bookmarks: BookmarkAccess,
    fileManager: FileManager = .default,
    scope: LibraryScopeAccess = .live()
  ) throws -> URL {
    guard let peeked = try bookmarks.peek(key: LibraryGrantStore.bookmarkKey) else {
      throw LibraryGrantError.mismatch(detail: "보안 북마크가 없다")
    }
    let root = peeked.url
    let accessing = scope.start(root)
    defer {
      if accessing { scope.stop(root) }
    }
    let grant = try LibraryGrantStore.validate(
      workspace: workspace, bookmarkedRoot: root, fileManager: fileManager)
    return try placeBesideBundle(job: job, grant: grant, fileManager: fileManager)
  }

  /// job의 captions/ 안에서 성공 SRT를 찾아 권한 폴더(번들 옆)로 원자 복사한다.
  ///
  /// 대상은 `<루트>/<SRT 이름>`이다 — 경로에 `.fcpbundle/` 구성요소가 있으면
  /// 폐쇄한다(번들 안 금지). 복사는 임시 쓰기 → 자리바꿈치기다(반쪽 파일이
  /// 보이지 않는다). 기록은 job에 남고 가져오기는 하지 않는다.
  @discardableResult
  public static func placeBesideBundle(
    job: RoundTripJob,
    grant: LibraryGrant,
    fileManager: FileManager = .default
  ) throws -> URL {
    let captions = job.captionsDirectoryURL
    let names = ((try? fileManager.contentsOfDirectory(atPath: captions.path)) ?? [])
      .filter { $0.hasSuffix(".srt") }
    guard names.count == 1, let name = names.first else {
      throw SRTDeliveryError.noSRT(captions.path)
    }
    let root = URL(fileURLWithPath: grant.rootPath)
      .resolvingSymlinksInPath().standardizedFileURL
    let destination = root.appendingPathComponent(name)
    // 번들 안 금지 — 대상 경로에 .fcpbundle 구성요소가 있으면 폐쇄다.
    if destination.path.contains(".fcpbundle/") {
      throw LibraryGrantError.targetInsideBundle(destination.path)
    }
    let source = captions.appendingPathComponent(name)
    let temporary = root.appendingPathComponent(".\(name).srtdelivery.tmp")
    try fileManager.copyItem(at: source, to: temporary)
    if fileManager.fileExists(atPath: destination.path) {
      try fileManager.removeItem(at: destination)
    }
    try fileManager.moveItem(at: temporary, to: destination)
    let record: [String: String] = [
      "path": destination.path, "placed_at": ISO8601DateFormatter().string(from: Date()),
    ]
    let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
    try data.write(
      to: job.directoryURL.appendingPathComponent(deliveryRecordName), options: [.atomic])
    return destination
  }
}
