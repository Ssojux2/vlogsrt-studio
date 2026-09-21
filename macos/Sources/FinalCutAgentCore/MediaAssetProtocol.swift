import Foundation

/// Apple Media Asset Protocol — SDEF(Resources/FinalCutAgent.sdef)의 값 모델.
///
/// SDEF는 `asset`을 **클래스**로, asset location·library location·data options를
/// 그 **속성**으로, 새 자산 만들기를 표준 `make` **명령**으로 선언한다. 이
/// 타입은 그 계약의 순수한 값과 검증만 소유한다 — XPC·SDK 와이어링은 아직
/// 구현되지 않았다(build-blocked). SDK가 있는 빌드에서만 WorkflowExtension의
/// `#if canImport(ProExtensionHost)` 블록이 이 모델을 어댑터에 연결한다.
public enum MediaAssetProperty: String, Sendable, CaseIterable {
  /// 프로젝트·이벤트 자산의 실제 위치(파일 URL).
  case assetLocation = "asset location"
  /// 라이브러리 저장 위치.
  case libraryLocation = "library location"
  /// 자산 데이터 접근 옵션(원본/프록시, 해상도).
  case dataOptions = "data options"
}

/// 유일한 명령 — 새 자산을 만들어 등록한다.
public enum MediaAssetMakeCommand: String, Sendable {
  case make
}

/// 자산 위치 속성 값 — 허용된 루트 하위의 실제 파일만 답한다.
public struct MediaAssetLocation: Equatable, Sendable {
  public let assetID: String
  public let fileURL: URL
  public let isUnderAllowedRoot: Bool

  public static func validate(
    assetID: String, fileURL: URL, allowedRoots: [URL], fileManager: FileManager = .default
  ) -> MediaAssetLocation {
    MediaAssetLocation(
      assetID: assetID,
      fileURL: fileURL,
      isUnderAllowedRoot: SecurityScopedBookmarkStore.contains(
        fileURL, under: allowedRoots, fileManager: fileManager
      )
    )
  }
}

/// 데이터 옵션 속성 값 — 원본/프록시 선택과 최대 해상도.
public struct MediaDataOptions: Equatable, Sendable {
  public let preferProxy: Bool
  public let maxLongEdge: Int?

  public init(preferProxy: Bool = false, maxLongEdge: Int? = nil) {
    self.preferProxy = preferProxy
    self.maxLongEdge = maxLongEdge
  }

  /// 분석(비전 프레임)에 쓸 옵션 — 원본 우선, 긴 변 512 이하.
  public static var analysis: MediaDataOptions {
    MediaDataOptions(preferProxy: false, maxLongEdge: 512)
  }
}

/// `make` — 새 자산 등록 결과. 실패는 nil이 아니라 오류로 닫는다.
public struct MediaAssetCreation: Equatable, Sendable {
  public let assetID: String
  public let fileURL: URL

  public static func validate(assetID: String, fileURL: URL) throws -> MediaAssetCreation {
    guard !assetID.isEmpty else { throw MediaAssetProtocolError.emptyAssetID }
    let values = try fileURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true else {
      throw MediaAssetProtocolError.notRegularFile(fileURL.path)
    }
    return MediaAssetCreation(assetID: assetID, fileURL: fileURL)
  }
}

public enum MediaAssetProtocolError: Error, Equatable {
  case emptyAssetID
  case notRegularFile(String)

  public var localizedDescription: String {
    switch self {
    case .emptyAssetID:
      "자산 ID가 비어 있습니다"
    case .notRegularFile(let path):
      "자산이 일반 파일이 아니거나 symlink입니다: \(path)"
    }
  }
}
