import AppKit
import Foundation

/// SDEF Apple event 응답 계층 — AppleScript가 닿는 자리.
///
/// 문서로 정해진 생명주기만 담당한다("Responding to Apple Events from Final Cut
/// Cut Pro"): make(자리표시자 만들기) → get location info/library info/metadata/
/// data options(위치·선택 답하기) → open(내보낸 파일 받기). open은
/// NSApplication 위임(application(_:open:))이 이미 인박스 스테이징으로 잇고
/// 있어 여기에 중복으로 두지 않는다.
///
/// 클래스명이 SDEF의 `<cocoa class>`와 정확히 일치해야 한다 — 명시적
/// `@objc(...)` 이름이 모듈 접두 없이 런타임 조회(NSClassFromString)에 닿게
/// 한다. 앱·코어 어느 쪽에 링크돼도 같은 이름으로 발견된다.

/// SDEF `asset` 클래스의 노출 구현 — Get Property는 KVC로 이 속성들을 읽는다.
@objc(MediaAssetProxy)
public final class MediaAssetProxy: NSObject {
  /// code `ID  ` — 자산의 고유 식별자. NSIDSpecifier가 이 값으로 자산을 되찾는다.
  @objc public let uniqueID: String
  /// code `pnam` — 자산 이름. Final Cut이 프로젝트 이름에서 만든다.
  @objc public let name: String
  /// make가 실어온 공유 메타데이터 원본(usrf 레코드).
  public let receivedMetadata: NSDictionary
  /// make가 실어온 데이터 선택 후보 — availableMetadataSets·availableVersions.
  public let receivedDataOptions: NSDictionary
  /// 내보내기 출력을 받을 폴더 — location info의 folder(asfd)에 답하는 값.
  public let deliveryFolder: URL

  public init(
    uniqueID: String,
    name: String,
    receivedMetadata: NSDictionary = [:],
    receivedDataOptions: NSDictionary = [:],
    deliveryFolder: URL
  ) {
    self.uniqueID = uniqueID
    self.name = name
    self.receivedMetadata = receivedMetadata
    self.receivedDataOptions = receivedDataOptions
    self.deliveryFolder = deliveryFolder
    super.init()
  }

  /// code `locn` — asset location 레코드. XML 전용: 미디어 없음·FCPXML 참.
  @objc public var locationInfo: NSDictionary {
    AssetLocationRecord.xmlOnly(folder: deliveryFolder, baseName: name).cocoaDictionary
  }

  /// code `lbry` — library location 레코드. 라이브러리 보관은 원하지 않는다.
  @objc public var libraryInfo: NSDictionary {
    LibraryLocationRecord.none().cocoaDictionary
  }

  /// code `meta` — 받은 공유 메타데이터에 이 자산의 추적 ID(share.id)를 보탠다.
  @objc public var metadata: NSDictionary {
    ShareMetadataReply.reply(
      received: receivedMetadata as? [String: Any] ?? [:], uniqueID: uniqueID)
  }

  /// code `dopt` — 후보에서 고른 답(metadataSet·descriptionVersion).
  @objc public var dataOptions: NSDictionary {
    DataOptionsReply.choose(
      availableMetadataSets: MediaAssetScripting.stringList(
        in: receivedDataOptions, key: ShareDataOptionKeys.availableMetadataSets),
      availableVersions: MediaAssetScripting.stringList(
        in: receivedDataOptions, key: ShareDataOptionKeys.availableVersions)
    ).cocoaDictionary
  }

  /// Final Cut이 이 자산을 다시 찾을 때 쓰는 지시자 — application의 `assets`
  /// 요소 안에서 id로 지목한다. make의 결과가 이 지시자로 흘러가고, 이후의
  /// get 질문이 같은 지시자로 되돌아온다.
  override public var objectSpecifier: NSScriptObjectSpecifier? {
    guard
      let applicationDescription = NSClassDescription(
        for: NSApplication.self
      ) as? NSScriptClassDescription
    else { return nil }
    return NSUniqueIDSpecifier(
      containerClassDescription: applicationDescription,
      containerSpecifier: nil,
      key: MediaAssetScripting.assetsElementKey,
      uniqueID: uniqueID
    )
  }
}

/// 만들어진 자산의 보관소 — uniqueID로 다시 찾는다(한 프로세스 안에서).
/// get 질문의 지시자 평가(NSApplication.assets → id 일치)가 여기에 닿는다.
public final class MediaAssetRegistry: @unchecked Sendable {
  public static let shared = MediaAssetRegistry()

  private let lock = NSLock()
  private var proxies: [String: MediaAssetProxy] = [:]

  public func register(_ proxy: MediaAssetProxy) {
    lock.lock()
    defer { lock.unlock() }
    proxies[proxy.uniqueID] = proxy
  }

  public func proxy(for uniqueID: String) -> MediaAssetProxy? {
    lock.lock()
    defer { lock.unlock() }
    return proxies[uniqueID]
  }

  /// 지시자 평가가 보는 전체 목록 — uniqueID 순서로 안정적으로 돌려준다.
  public func allProxies() -> [MediaAssetProxy] {
    lock.lock()
    defer { lock.unlock() }
    return proxies.keys.sorted().compactMap { proxies[$0] }
  }

  public func removeAll() {
    lock.lock()
    defer { lock.unlock() }
    proxies.removeAll()
  }
}

extension NSApplication {
  /// SDEF class-extension `application`의 `asset` 요소(cocoa key "assets") —
  /// Cocoa scripting이 NSIDSpecifier 평가 중 KVC로 도달하는 길. 등록된 자리표시자
  /// 전부가 이 요소의 내용이다.
  @objc public var assets: [MediaAssetProxy] {
    MediaAssetRegistry.shared.allProxies()
  }
}

/// make(corecrel)가 실어온 with properties를 정규화한 요청.
public struct ShareAssetRequest: Equatable {
  /// pnam — 자산 이름. location info의 base name도 이 값으로 답한다.
  public let name: String
  /// meta — 공유 메타데이터 레코드.
  public let metadata: NSDictionary
  /// dopt — 데이터 선택 후보 레코드(availableMetadataSets·availableVersions).
  public let dataOptions: NSDictionary

  public init(name: String, metadata: NSDictionary = [:], dataOptions: NSDictionary = [:]) {
    self.name = name
    self.metadata = metadata
    self.dataOptions = dataOptions
  }
}

/// make 실패의 종류 — 하나하나가 AppleScript에 그대로 보이는 오류가 된다.
public enum MakeAssetFailure: Error, Equatable {
  /// with properties에 name이 없다 — base name을 계산할 수 없다.
  case missingAssetName
  /// new(kocl)가 asset 클래스가 아니다.
  case notAnAssetClass(String)
  /// with properties가 레코드가 아니다.
  case propertiesNotARecord(String)

  public var scriptErrorNumber: Int {
    switch self {
    case .missingAssetName: -2701
    case .notAnAssetClass: -2702
    case .propertiesNotARecord: -2703
    }
  }

  public var scriptErrorString: String {
    switch self {
    case .missingAssetName:
      "자산 이름(name)이 필요합니다"
    case .notAnAssetClass(let description):
      "make의 대상은 asset 클래스여야 합니다: \(description)"
    case .propertiesNotARecord(let description):
      "with properties는 레코드여야 합니다: \(description)"
    }
  }
}

/// 순수 판정 계층 — Apple event 없이 단위 시험이 도는 몸통. NSScriptCommand는 얇은 껍데기다.
public enum MakeAssetEvaluator {
  /// SDEF의 asset 클래스 이름·코드 — new(kocl)가 이 둘 중 하나로 온다.
  static let assetClassName = "asset"
  static let assetClassCode = "aset"

  /// with properties의 키 정규화 — 대소문자·공백을 무시하고 cocoa key
  /// (name·metadata·dataOptions)와 4자 코드(pnam·meta·dopt)를 한 좌표로 묶는다.
  /// 두 철자가 다 흐를 수 있는 건 레코드 라벨의 마샬링이 sdef 시점에 따라 양쪽
  /// 다 나오기 때문이다 — 어느 쪽이 와도 같은 자리표시자가 만들어져야 한다.
  static func evaluate(objectClass: Any?, properties: Any?) -> Result<ShareAssetRequest, MakeAssetFailure> {
    let classDescription = objectClass.map { String(describing: $0) } ?? "nil"
    guard isAssetClass(objectClass) else {
      return .failure(.notAnAssetClass(classDescription))
    }
    if let properties, properties as? [String: Any] == nil {
      return .failure(.propertiesNotARecord(String(describing: properties)))
    }
    guard let name = propertyName(from: properties) else {
      return .failure(.missingAssetName)
    }
    let metadata = propertyRecord(from: properties, cocoaKeys: ["metadata"], codes: ["meta"])
    let dataOptions = propertyRecord(from: properties, cocoaKeys: ["dataoptions"], codes: ["dopt"])
    return .success(
      ShareAssetRequest(name: name, metadata: metadata, dataOptions: dataOptions))
  }

  /// new(kocl)가 asset을 가리키는지 — 이름("asset")·코드("aset")·스크립트 클래스
  /// 기술(NSScriptClassDescription) 어느 형태로 와도 답한다.
  static func isAssetClass(_ value: Any?) -> Bool {
    guard let value else { return false }
    if let description = value as? NSScriptClassDescription,
      description.className == assetClassName
    {
      return true
    }
    if let descriptor = value as? NSAppleEventDescriptor {
      if descriptor.stringValue == assetClassName { return true }
      if codeString(descriptor.typeCodeValue) == assetClassCode { return true }
    }
    let text = normalizedKey(String(describing: value))
    return text == assetClassName || text == assetClassCode
  }

  /// with properties에서 name을 꺼낸다 — 빈 문자열은 없는 것으로 본다.
  static func propertyName(from properties: Any?) -> String? {
    guard let record = properties as? [String: Any] else { return nil }
    for (key, value) in record {
      let normalized = normalizedKey(key)
      guard normalized == "name" || normalized == "pnam" else { continue }
      if let text = value as? String, !text.isEmpty { return text }
    }
    return nil
  }

  /// with properties에서 레코드 속성(meta·dopt)을 꺼낸다 — 없으면 빈 레코드다.
  static func propertyRecord(from properties: Any?, cocoaKeys: [String], codes: [String]) -> NSDictionary {
    guard let record = properties as? [String: Any] else { return [:] }
    let wanted = Set(cocoaKeys + codes)
    for (key, value) in record where wanted.contains(normalizedKey(key)) {
      if let dictionary = value as? NSDictionary { return dictionary }
      if let dictionary = value as? [String: Any] { return NSDictionary(dictionary: dictionary) }
    }
    return [:]
  }

  /// OSType 4바이트를 그 글자들로 — 'aset' 코드가 문자열 "aset"으로 걸린다.
  static func codeString(_ code: OSType) -> String {
    let bytes = [
      UInt8((code >> 24) & 0xFF), UInt8((code >> 16) & 0xFF),
      UInt8((code >> 8) & 0xFF), UInt8(code & 0xFF),
    ]
    return String(bytes: bytes, encoding: .macOSRoman) ?? ""
  }

  /// 키 정규화 — 소문자에 공백·언더바를 지운다("data options"·"dataOptions"·
  /// "data_options"가 같아진다).
  static func normalizedKey(_ key: String) -> String {
    key.lowercased().filter { $0.isLetter || $0.isNumber }
  }
}

/// 판정을 통과한 요청으로 자리표시자 자산을 만들어 보관소에 등록한다 —
/// 내보내기를 받을 폴더도 여기서 만든다(Final Cut이 곧 그 폴더에 쓴다).
public enum AssetPlaceholderFactory {
  @discardableResult
  public static func create(
    request: ShareAssetRequest,
    uniqueID: String = "asset-\(UUID().uuidString)",
    workspaceRoot: URL?,
    registry: MediaAssetRegistry = .shared,
    fileManager: FileManager = .default
  ) throws -> MediaAssetProxy {
    let base = workspaceRoot ?? fileManager.temporaryDirectory
    let folder = base
      .appendingPathComponent(MediaAssetScripting.stagingDirectoryName, isDirectory: true)
      .appendingPathComponent(uniqueID, isDirectory: true)
    try fileManager.createDirectory(at: folder, withIntermediateDirectories: true)
    let proxy = MediaAssetProxy(
      uniqueID: uniqueID,
      name: request.name,
      receivedMetadata: request.metadata,
      receivedDataOptions: request.dataOptions,
      deliveryFolder: folder
    )
    registry.register(proxy)
    return proxy
  }
}

/// SDEF `make` 명령의 노출 구현 — 문서의 정본 형태를 답한다:
/// `make new asset with properties {name, metadata, data options}`.
/// 새 자산을 만들어 그 **지시자**를 돌려주고, Final Cut이 이어서 보내는
/// get location info/library info/metadata/data options는 KVC가 답한다.
@objc(MakeAssetCommand)
public final class MakeAssetCommand: NSScriptCommand {
  override public func performDefaultImplementation() -> Any? {
    let arguments = evaluatedArguments ?? [:]
    switch MakeAssetEvaluator.evaluate(
      objectClass: arguments["ObjectClass"],
      properties: arguments["KeyDictionary"]
    ) {
    case .success(let request):
      let workspaceRoot = try? AppGroupWorkspace.live().rootURL
      guard
        let proxy = try? AssetPlaceholderFactory.create(
          request: request, workspaceRoot: workspaceRoot)
      else {
        scriptErrorNumber = -2704
        scriptErrorString = "내보내기 폴더를 만들 수 없습니다"
        return nil
      }
      return proxy
    case .failure(let failure):
      scriptErrorNumber = failure.scriptErrorNumber
      scriptErrorString = failure.scriptErrorString
      return nil
    }
  }
}

/// Apple event 계층의 공용 상수 — SDEF 속성과 같은 좌표를 공유한다.
public enum MediaAssetScripting {
  /// SDEF의 cocoa class 이름 — 구현 클래스의 @objc 이름과 정확히 일치해야 한다.
  public static let proxyCocoaClassName = "MediaAssetProxy"
  public static let commandCocoaClassName = "MakeAssetCommand"

  /// class-extension application의 asset 요소 cocoa 키 — 지시자 평가가
  /// NSApplication에 이 키로 KVC 질의한다.
  public static let assetsElementKey = "assets"

  /// 내보내기 출력을 모으는 폴더 이름 — App Group 루트(없으면 임시 폴더) 아래.
  /// 원본 미디어와 기존 인박스(RoughCutInbox·CaptionInbox)와는 다른 자리다.
  public static let stagingDirectoryName = "ShareStaging"

  /// usrf 레코드 값에서 문자열 목록을 꺼낸다 — 라벨 대소문자는 흐르는 쪽이
  /// 정하므로 무시해서 비긴다.
  public static func stringList(in record: NSDictionary, key: String) -> [String] {
    let lowered = key.lowercased()
    for (candidate, value) in record {
      guard (candidate as? String)?.lowercased() == lowered else { continue }
      if let list = value as? [Any] {
        return list.compactMap { $0 as? String }
      }
      if let text = value as? String { return [text] }
    }
    return []
  }
}
