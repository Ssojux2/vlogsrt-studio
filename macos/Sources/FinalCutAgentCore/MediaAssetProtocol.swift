import Foundation

// ─────────────────────────────────────────────────────────────
//  Apple Media Asset Protocol — SDEF(Resources/FinalCutAgent.sdef)의 값 모델
//
//  모양은 Apple의 정본 문서("Creating Scripting Definitions for Custom Share
//  Destinations" · "Responding to Apple Events from Final Cut Pro")를 따른다:
//  asset은 클래스(aset)이고, location info·library info는 **레코드 형** 속성,
//  metadata·data options는 user defined record(usrf)다. 이 파일은 그 계약의
//  순수한 값과 정책만 소유한다 — Apple event를 다루는 껍데기는
//  MediaAssetScripting.swift다. 두 계층을 섞지 않는다.
// ─────────────────────────────────────────────────────────────

/// SDEF `asset` 클래스의 속성 이름 — 선언 순서와 같다(테스트가 이 순서를 지킨다).
public enum MediaAssetProperty: String, Sendable, CaseIterable {
  case id
  case name
  case locationInfo = "location info"
  case libraryInfo = "library info"
  case metadata
  case dataOptions = "data options"
}

/// 유일한 명령 — 표준 suite의 `make`(corecrel)를 이 suite 안에 다시 선언한 것이다.
public enum MediaAssetMakeCommand: String, Sendable {
  case make
}

/// asset location 레코드(aslc)의 값 — Final Cut이 "어디에 무엇을 내보낼지"에
/// 대한 답. AI Rough Cut은 **XML 전용**이다: 미디어는 받지 않고 FCPXML만 받는다.
public struct AssetLocationRecord: Equatable, Sendable {
  /// 내보내기 출력 파일이 담길 폴더(asfd).
  public let folder: URL
  /// 내보내기 출력 파일의 기본 이름(asbn) — 자산 이름과 같아야 한다.
  public let baseName: String
  /// 렌더된 미디어를 받으면 참(ashm). XML 전용 내보내기는 거짓이다.
  public let hasMedia: Bool
  /// FCPXML 편집 기술을 받으면 참(ashd). XML 전용 내보내기는 참이다.
  public let hasDescription: Bool

  public init(folder: URL, baseName: String, hasMedia: Bool = false, hasDescription: Bool = true) {
    self.folder = folder
    self.baseName = baseName
    self.hasMedia = hasMedia
    self.hasDescription = hasDescription
  }

  /// XML 전용 내보내기의 정답 — 미디어 없음·FCPXML 참.
  public static func xmlOnly(folder: URL, baseName: String) -> AssetLocationRecord {
    AssetLocationRecord(folder: folder, baseName: baseName, hasMedia: false, hasDescription: true)
  }

  /// SDEF record-type의 cocoa 키(folder·basename·hasMedia·hasDescription)로 답한다.
  public var cocoaDictionary: NSDictionary {
    NSDictionary(dictionary: [
      "folder": folder,
      "basename": baseName,
      "hasMedia": hasMedia,
      "hasDescription": hasDescription,
    ])
  }
}

/// library location 레코드(lblc)의 값 — 라이브러리 보관 파일의 위치 답.
/// Final Cut 문서: 앱이 이 질문에 답하지 않으면 라이브러리 파일을 원하지 않는
/// 것으로 본다. 우리는 명시적으로 "원하지 않는다"고 답는다 — 애매함을 남기지 않는다.
public struct LibraryLocationRecord: Equatable, Sendable {
  /// 라이브러리 출력 폴더(lbfd). nil이면 키를 빼고 답한다.
  public let folder: URL?
  /// 라이브러리 출력 기본 이름(lbbn). nil이면 키를 빼고 답한다.
  public let baseName: String?
  /// 라이브러리 보관 파일을 받으면 참(lbha).
  public let hasArchive: Bool
  /// 라이브러리 FCPXML을 받으면 참(lbhd).
  public let hasLibraryDescription: Bool

  public init(
    folder: URL? = nil, baseName: String? = nil,
    hasArchive: Bool = false, hasLibraryDescription: Bool = false
  ) {
    self.folder = folder
    self.baseName = baseName
    self.hasArchive = hasArchive
    self.hasLibraryDescription = hasLibraryDescription
  }

  /// 라이브러리 보관을 원하지 않는답 — 두 플래그 모두 거짓.
  public static func none() -> LibraryLocationRecord {
    LibraryLocationRecord()
  }

  /// 폴더·이름이 nil이면 그 키는 빼고 답한다 — Final Cut이 문서의 기본값을
  /// 쓰게 하는 것이지 빈 값을 보내는 게 아니다.
  public var cocoaDictionary: NSDictionary {
    var values: [String: Any] = [
      "hasArchive": hasArchive,
      "hasDescription": hasLibraryDescription,
    ]
    if let folder { values["folder"] = folder }
    if let baseName { values["basename"] = baseName }
    return NSDictionary(dictionary: values)
  }
}

/// Final Cut이 make의 data options로 보내는 후보 키, 그리고 get data options
/// 답의 키 — 문서의 Apple event 추적에 나오는 문자열 그대로다(usrf 레코드는
/// 키가 문자열로 흐른다).
public enum ShareDataOptionKeys {
  /// make가 실어오는 후보 — 사용 가능한 메타데이터 보기 목록.
  public static let availableMetadataSets = "availableMetadataSets"
  /// make가 실어오는 후보 — 사용 가능한 FCPXML DTD 버전 목록.
  public static let availableVersions = "availableVersions"
  /// 답의 키 — 고른 메타데이터 보기 이름.
  public static let metadataSet = "metadataSet"
  /// 답의 키 — 고른 DTD 버전.
  public static let descriptionVersion = "descriptionVersion"
}

/// get data options(dopt)의 답 — 메타데이터 보기와 DTD 버전.
/// 둘 다 비울 수 있다(키를 빼면 Final Cut이 각기 기본값 — 현재 선택된 보기·
/// 마지막 내보내기 버전 — 을 쓴다).
public struct DataOptionsReply: Equatable, Sendable {
  /// 고른 메타데이터 보기 이름(metadataSet). nil이면 키를 뺀다.
  public let metadataViewName: String?
  /// 고른 FCPXML DTD 버전(descriptionVersion). nil이면 키를 뺀다.
  public let dtdVersion: String?

  public init(metadataViewName: String?, dtdVersion: String?) {
    self.metadataViewName = metadataViewName
    self.dtdVersion = dtdVersion
  }

  public var cocoaDictionary: NSDictionary {
    var values: [String: Any] = [:]
    if let metadataViewName {
      values[ShareDataOptionKeys.metadataSet] = metadataViewName
    }
    if let dtdVersion {
      values[ShareDataOptionKeys.descriptionVersion] = dtdVersion
    }
    return NSDictionary(dictionary: values)
  }

  /// 문서의 추적에서 답한 모양 — 이 앱이 고르는 정책의 근거가 된다.
  public static let noFilteringViewName = "None"

  /// 이 앱이 받고 싶은 FCPXML 버전 — 붙여넣기 계약(com.apple.finalcutpro.xml.v1-14)
  /// 와 같은 1.14. Final Cut이 목록에 없으면 그 이하의 가장 높은 버전으로 물러난다.
  public static let preferredDTDVersion = "1.14"

  /// Final Cut이 보내준 후보에서 답을 고른다.
  ///
  /// 보기는 "None"(걸러냄 없음)을, 목록에 없으면 첫째 항을 고른다 — 이 앱은
  /// 공유 메타데이터로 걸러내는 일이 없다. 버전은 1.14에 가장 가까운 것을
  /// 고른다 — 같은 거리면 낮은 쪽(엔진이 아는 형태)이다.
  public static func choose(
    availableMetadataSets: [String], availableVersions: [String]
  ) -> DataOptionsReply {
    let view = availableMetadataSets.first { $0 == noFilteringViewName }
      ?? availableMetadataSets.first
    let version = chooseVersion(from: availableVersions)
    return DataOptionsReply(metadataViewName: view, dtdVersion: version)
  }

  /// 1.14에 가장 가까운 버전 — 거리가 같으면 낮은 쪽. 파싱이 안 되는 항목은 버린다.
  static func chooseVersion(from versions: [String]) -> String? {
    guard let preferred = parseVersion(preferredDTDVersion) else { return nil }
    let preferredScore = preferred.0 * 1_000 + preferred.1
    let parsed = versions.compactMap { version -> (String, Int)? in
      guard let tuple = parseVersion(version) else { return nil }
      return (version, tuple.0 * 1_000 + tuple.1)
    }
    return parsed.min { left, right in
      let leftDistance = abs(left.1 - preferredScore)
      let rightDistance = abs(right.1 - preferredScore)
      return leftDistance == rightDistance ? left.1 < right.1 : leftDistance < rightDistance
    }?.0
  }

  static func parseVersion(_ text: String) -> (Int, Int)? {
    let parts = text.split(separator: ".")
    guard parts.count == 2,
      let major = Int(parts[0]), let minor = Int(parts[1])
    else { return nil }
    return (major, minor)
  }
}

/// get metadata(meta)의 답 — 받은 공유 메타데이터를 되돌리고 이 앱의 추적 ID를
/// 보탠다. 문서: 앱은 com.apple.proapps.share.id 키로 미디어 자산의 고유 ID를
/// 만들어 Final Cut이 내보내기 파일에 싣게 할 수 있다.
public enum ShareMetadataReply {
  /// 공유 메타데이터의 고유 ID 키 — 문서의 Apple event 추적에 나오는 그대로.
  public static let shareIDKey = "com.apple.proapps.share.id"

  public static func reply(received: [String: Any], uniqueID: String) -> NSDictionary {
    var values = received
    values[shareIDKey] = uniqueID
    return NSDictionary(dictionary: values)
  }
}
