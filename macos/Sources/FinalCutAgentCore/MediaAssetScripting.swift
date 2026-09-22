import AppKit
import Foundation

/// SDEF Apple event 응답 계층 — AppleScript(OSAScriptingDefinition)가 닿는 자리.
///
/// 이 계층은 **Apple event**만 담당한다. Final Cut 앱이 확장과 실제로 통신하는
/// Media Asset Protocol XPC(ProExtensionHost) 와이어링은 여전히 구현되지
/// 않았다 — SDK가 있는 빌드에서 별도 어댑터가 `MediaAssetProperty`·
/// `MediaDataOptions` 값을 연결해야 한다. 두 계층을 섞지 않는다.
///
/// 클래스명이 SDEF의 `<cocoa class>`와 정확히 일치해야 한다 — 명시적
/// `@objc(...)` 이름이 모듈 접두 없이 런타임 조회(NSClassFromString)에 닿게
/// 한다. 앱·코어 어느 쪽에 링크돼도 같은 이름으로 발견된다.

/// SDEF `asset` 클래스의 노출 구현 — Get Property는 KVC로 이 속성들을 읽는다.
@objc(MediaAssetProxy)
public final class MediaAssetProxy: NSObject {
  /// code `aloc` — 프로젝트·이벤트 자산의 실제 파일 위치.
  @objc public dynamic var assetLocation: NSURL?
  /// code `lloc` — 라이브러리 저장 위치. 만들어진 자산이 모르면 nil(missing value)이다.
  @objc public dynamic var libraryLocation: NSURL?
  /// code `dopt` — 자산 데이터 접근 옵션 레코드(원본/프록시·최대 해상도).
  @objc public dynamic var dataOptions: NSDictionary?

  public let assetID: String

  public init(
    assetID: String,
    assetLocation: NSURL?,
    libraryLocation: NSURL? = nil,
    dataOptions: NSDictionary? = MediaAssetScripting.analysisOptions
  ) {
    self.assetID = assetID
    self.assetLocation = assetLocation
    self.libraryLocation = libraryLocation
    self.dataOptions = dataOptions
    super.init()
  }
}

/// 만들어진 자산의 보관소 — assetID로 다시 찾는다(한 프로세스 안에서).
public final class MediaAssetRegistry: @unchecked Sendable {
  public static let shared = MediaAssetRegistry()

  private let lock = NSLock()
  private var proxies: [String: MediaAssetProxy] = [:]

  public func register(_ proxy: MediaAssetProxy) {
    lock.lock()
    defer { lock.unlock() }
    proxies[proxy.assetID] = proxy
  }

  public func proxy(for assetID: String) -> MediaAssetProxy? {
    lock.lock()
    defer { lock.unlock() }
    return proxies[assetID]
  }

  public func assetIDs() -> [String] {
    lock.lock()
    defer { lock.unlock() }
    return Array(proxies.keys)
  }

  public func removeAll() {
    lock.lock()
    defer { lock.unlock() }
    proxies.removeAll()
  }
}

/// make 실패의 종류 — 하나하나가 AppleScript에 그대로 보이는 오류가 된다.
public enum AssetCreationFailure: Error, Equatable {
  /// direct parameter가 없다.
  case missingParameter
  /// 배열 등으로 여러 자산을 한 번에 요청했다 — 한 요청은 한 자산이다.
  case multipleAssets(Int)
  /// file URL이 아니다(다른 스킴·해석 불가 문자열).
  case notAFileURL(String)
  /// 가리키는 파일이 없다.
  case fileMissing(String)
  /// 허용된 미디어 루트 밖이다(배치 거부).
  case outsideAllowedRoots(String)

  public var scriptErrorNumber: Int {
    switch self {
    case .missingParameter: -2701
    case .multipleAssets: -2702
    case .notAFileURL: -2703
    case .fileMissing: -2704
    case .outsideAllowedRoots: -2705
    }
  }

  public var scriptErrorString: String {
    switch self {
    case .missingParameter:
      "새 자산 파일(file)이 필요합니다"
    case .multipleAssets(let count):
      "한 번의 make는 자산 하나만 만듭니다 — 요청이 \(count)개입니다"
    case .notAFileURL(let description):
      "file URL이어야 합니다: \(description)"
    case .fileMissing(let path):
      "자산 파일이 없습니다: \(path)"
    case .outsideAllowedRoots(let path):
      "허용된 미디어 루트 밖의 위치입니다: \(path)"
    }
  }
}

/// 순수 판정 계층 — Apple event 없이 단위 시험이 도는 몸통. NSScriptCommand는 얇은 껍데기다.
public enum AssetCreationEvaluator {
  /// direct parameter를 자산 파일 URL로 정규화한다.
  ///
  /// Apple event는 NSURL·NSString·NSArray 어느 쪽으로도 올 수 있다. 하나만
  /// 허용한다(배열 = multipleAssets). 루트가 하나라도 설정돼 있으면 그 하위만
  /// 받는다 — 루트가 없을 때의 배치 검사는 건너뛰는 게 아니라 그냥 없다(스크립트
  /// 세계가 미디어 권한을 아직 모르는 상태다).
  public static func evaluate(
    directParameter: Any?,
    roots: [URL],
    fileManager: FileManager = .default
  ) -> Result<URL, AssetCreationFailure> {
    let candidate: URL
    switch directParameter {
    case let url as URL:
      candidate = url
    case let text as NSString:
      candidate = URL(fileURLWithPath: text as String)
    case let array as NSArray:
      return .failure(.multipleAssets(array.count))
    case .some(let other):
      return .failure(.notAFileURL(String(describing: other)))
    case .none:
      return .failure(.missingParameter)
    }
    guard candidate.isFileURL, let scheme = candidate.scheme?.lowercased(),
      scheme == "file"
    else {
      return .failure(.notAFileURL(candidate.absoluteString))
    }
    let resolved = candidate.resolvingSymlinksInPath()
    guard fileManager.fileExists(atPath: resolved.path) else {
      return .failure(.fileMissing(resolved.path))
    }
    if !roots.isEmpty,
      !SecurityScopedBookmarkStore.contains(resolved, under: roots, fileManager: fileManager)
    {
      return .failure(.outsideAllowedRoots(resolved.path))
    }
    return .success(resolved)
  }

  /// 판정을 통과한 URL로 placeholder 자산을 만들어 보관소에 등록한다.
  public static func registerAsset(at url: URL) -> MediaAssetProxy {
    let proxy = MediaAssetProxy(
      assetID: "asset-\(UUID().uuidString)",
      assetLocation: url as NSURL
    )
    MediaAssetRegistry.shared.register(proxy)
    return proxy
  }
}

/// SDEF `make` 명령의 노출 구현 — direct parameter를 받아 placeholder 자산을
/// 만들고, 후속 Get Property가 KVC(assetLocation·libraryLocation·dataOptions)로
/// 읽게 한다. 누락·배치·잘못된 URL은 scriptErrorNumber/String으로 실패한다.
@objc(CreateAssetCommand)
public final class CreateAssetCommand: NSScriptCommand {
  override public func performDefaultImplementation() -> Any? {
    let roots = (try? SecurityScopedBookmarkStore().allowedRoots()) ?? []
    switch AssetCreationEvaluator.evaluate(
      directParameter: directParameter, roots: roots
    ) {
    case .success(let url):
      return AssetCreationEvaluator.registerAsset(at: url)
    case .failure(let failure):
      scriptErrorNumber = failure.scriptErrorNumber
      scriptErrorString = failure.scriptErrorString
      return nil
    }
  }
}

/// Apple event 계층의 공용 상수 — SDEF 속성과 같은 좌표를 공유한다.
public enum MediaAssetScripting {
  /// dopt 기본 레코드 — 분석 기본값(원본 우선·긴 변 512)과 같은 값.
  /// 저장된 전역이 아니라 계산 속성이다(공유 가변 상태가 없다).
  public static var analysisOptions: NSDictionary {
    NSDictionary(dictionary: ["preferProxy": false, "maxLongEdge": 512])
  }

  /// SDEF의 cocoa class 이름 — 구현 클래스의 @objc 이름과 정확히 일치해야 한다.
  public static let proxyCocoaClassName = "MediaAssetProxy"
  public static let commandCocoaClassName = "CreateAssetCommand"
}
