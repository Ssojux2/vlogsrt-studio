import AppKit
import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  사용자 지정 공유 대상 — Apple Media Asset Protocol Apple event 생명주기
//
//  문서("Responding to Apple Events from Final Cut Pro")가 정하는 순서:
//  make(자리표시자 만들기) → get location info·library info·metadata·
//  data options(위치·선택 답하기) → open(내보낸 파일 받기 — 인박스 스테이징).
//  여기서는 make의 판정과 자리표시자의 네 답을 단위 시험으로 잡는다.
// ─────────────────────────────────────────────────────────────

@Test func makeEvaluatorAcceptsEveryDocumentedKeySpelling() {
  // 문서의 AppleScript 예가 실어오는 그 모양 — cocoa key 철자.
  let request = MakeAssetEvaluator.evaluate(
    objectClass: "asset",
    properties: [
      "name": "MyNewAsset",
      "metadata": ["com.apple.proapps.share.episodeID": "MyNewEpisode"],
      "dataOptions": [
        "availableMetadataSets": ["Camera View", "General View"],
        "availableVersions": ["1.13", "1.14"],
      ],
    ] as [String: Any])
  guard case .success(let value) = request else {
    Issue.record("make이 자산을 만들어야 한다: \(request)")
    return
  }
  #expect(value.name == "MyNewAsset")
  #expect(
    value.metadata["com.apple.proapps.share.episodeID"] as? String == "MyNewEpisode")
  #expect(
    MediaAssetScripting.stringList(in: value.dataOptions, key: "availableMetadataSets")
      == ["Camera View", "General View"])

  // 4자 코드 철자(pnam·meta·dopt)도 같은 자리에 닿는다 — 라벨 마샬링이
  // 어느 철자로 흐르든 자리표시자는 하나여야 한다.
  let byCode = MakeAssetEvaluator.evaluate(
    objectClass: "aset",
    properties: ["pnam": "ByCode", "meta": ["k": "v"], "dopt": ["k2": "v2"]]
      as [String: Any])
  guard case .success(let coded) = byCode else {
    Issue.record("코드 철자도 통과해야 한다: \(byCode)")
    return
  }
  #expect(coded.name == "ByCode")
  #expect(coded.metadata["k"] as? String == "v")
  #expect(coded.dataOptions["k2"] as? String == "v2")

  // 라벨 대소문자·공백도 무시한다("data options" == "dataOptions").
  let spaced = MakeAssetEvaluator.evaluate(
    objectClass: "asset",
    properties: ["name": "Spaced", "data options": ["availableVersions": ["1.14"]]]
      as [String: Any])
  guard case .success(let unspaced) = spaced else {
    Issue.record("공백 철자도 통과해야 한다: \(spaced)")
    return
  }
  #expect(unspaced.dataOptions.count == 1)
}

@Test func makeEvaluatorRejectsUndocumentedRequests() {
  #expect(
    MakeAssetEvaluator.evaluate(objectClass: "document", properties: ["name": "x"] as [String: Any])
      == .failure(.notAnAssetClass("document")))
  #expect(
    MakeAssetEvaluator.evaluate(objectClass: nil, properties: ["name": "x"] as [String: Any])
      == .failure(.notAnAssetClass("nil")))
  // 이름이 없거나 비면 base name을 계산할 수 없다.
  #expect(
    MakeAssetEvaluator.evaluate(objectClass: "asset", properties: [:] as [String: Any])
      == .failure(.missingAssetName))
  #expect(
    MakeAssetEvaluator.evaluate(objectClass: "asset", properties: ["name": ""] as [String: Any])
      == .failure(.missingAssetName))
  #expect(
    MakeAssetEvaluator.evaluate(objectClass: "asset", properties: nil)
      == .failure(.missingAssetName))
  // with properties가 레코드가 아니다.
  #expect(
    MakeAssetEvaluator.evaluate(objectClass: "asset", properties: "name")
      == .failure(.propertiesNotARecord("name")))

  // 오류는 AppleScript에 그대로 보이는 번호·문장으로 나간다.
  #expect(MakeAssetFailure.notAnAssetClass("document").scriptErrorNumber == -2702)
  #expect(MakeAssetFailure.missingAssetName.scriptErrorNumber == -2701)
  #expect(MakeAssetFailure.propertiesNotARecord("x").scriptErrorNumber == -2703)
}

@Test func placeholderFactoryCreatesDeliveryFolderAndRegistersTheProxy() throws {
  let root = FileManager.default.temporaryDirectory
    .appendingPathComponent(UUID().uuidString, isDirectory: true)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  // 보관소는 시험마다 사본을 쓴다 — .shared는 병렬 시험과 경합한다.
  let registry = MediaAssetRegistry()

  let request = ShareAssetRequest(
    name: "Smoke",
    metadata: [:],
    dataOptions: ["availableMetadataSets": ["None"], "availableVersions": ["1.14"]])
  let proxy = try AssetPlaceholderFactory.create(
    request: request, uniqueID: "asset-fixed-1", workspaceRoot: root, registry: registry)

  // 내보내기를 받을 폴더가 실제로 생긴다 — Final Cut이 곧 여기에 쓴다.
  #expect(
    FileManager.default.fileExists(atPath: proxy.deliveryFolder.path))
  #expect(
    proxy.deliveryFolder.path
      .hasSuffix("ShareStaging/asset-fixed-1"))
  #expect(proxy.deliveryFolder.path.contains(root.path))
  // 보관소 등록 — 지시자 평가(NSApplication.assets)가 여기를 읽는다.
  #expect(registry.proxy(for: "asset-fixed-1") === proxy)
  #expect(registry.allProxies() == [proxy])
  // ID 순서로 안정적으로 돌려준다 — 지시자 평가가 보는 창 순서다.
  try AssetPlaceholderFactory.create(
    request: ShareAssetRequest(name: "Earlier"), uniqueID: "asset-0000",
    workspaceRoot: root, registry: registry)
  #expect(registry.allProxies().map(\.uniqueID) == ["asset-0000", "asset-fixed-1"])

  // App Group이 없는 환경(단위 시험)은 임시 폴더로 물러난다 — 흐름이 끊기지 않는다.
  let fallback = try AssetPlaceholderFactory.create(
    request: ShareAssetRequest(name: "Fallback"), uniqueID: "asset-fallback",
    workspaceRoot: nil, registry: registry)
  #expect(
    fallback.deliveryFolder.path.hasPrefix(
      FileManager.default.temporaryDirectory.path))
}

// NSApplication.shared는 main thread 전용이다 — 지시자 평가 길(asset 요소)을
// 보는 이 시험은 @MainActor로만 돈다.
@Test @MainActor func proxyAnswersTheDocumentedGetLifecycle() throws {
  let proxy = MediaAssetProxy(
    uniqueID: "asset-lifecycle",
    name: "MyNewAsset",
    receivedMetadata: ["com.apple.proapps.share.episodeID": "MyNewEpisode"],
    receivedDataOptions: [
      "availableMetadataSets": ["Camera View", "None"],
      "availableVersions": ["1.13", "1.14", "1.15"],
    ],
    deliveryFolder: URL(fileURLWithPath: "/tmp/ignored", isDirectory: true)
  )

  // get location info — XML 전용: 미디어 없음·FCPXML 참·폴더·기본 이름.
  let location = proxy.locationInfo
  #expect(location["hasMedia"] as? Bool == false)
  #expect(location["hasDescription"] as? Bool == true)
  #expect(location["basename"] as? String == "MyNewAsset")
  #expect((location["folder"] as? URL) == proxy.deliveryFolder)

  // get library info — 라이브러리 보관을 원하지 않는다.
  let library = proxy.libraryInfo
  #expect(library["hasArchive"] as? Bool == false)
  #expect(library["hasDescription"] as? Bool == false)
  #expect(library.count == 2)

  // get metadata — 받은 키 + share.id(이 자산의 추적 ID).
  let metadata = proxy.metadata
  #expect(
    metadata["com.apple.proapps.share.episodeID"] as? String == "MyNewEpisode")
  #expect(metadata[ShareMetadataReply.shareIDKey] as? String == "asset-lifecycle")

  // get data options — 후보에서 고른 답.
  let options = proxy.dataOptions
  #expect(options["metadataSet"] as? String == "None")
  #expect(options["descriptionVersion"] as? String == "1.14")

  // application의 assets 요소는 KVC로 노출돼 있다 — 지시자 평가가 걸어가는
  // 바로 그 길이다. 보관소 내용 자체는 사본 보관소 시험이 잡는다(.shared는
  // 병렬 시험과 경합하니 여기서는 노출만 잡는다).
  #expect(NSApplication.shared.responds(to: NSSelectorFromString("assets")))
  #expect(NSApplication.shared.value(forKey: "assets") is [MediaAssetProxy])
}

@Test func objectSpecifierPointsIntoTheApplicationAssetsElement() {
  let proxy = MediaAssetProxy(
    uniqueID: "asset-spec", name: "Spec",
    deliveryFolder: URL(fileURLWithPath: "/tmp/ignored", isDirectory: true))

  // 앱 실행 중에는 sdef가 실려 묘사자가 있다 — 그때 지시자는 application의
  // assets 요소 안 id 지목이다. 단위 시험 실행기에는 sdef가 없어 묘사자가
  // nil이고, 그때 지시자를 만들지 않는 것까지가 계약이다(실제 왕복은 smoke가 본다).
  let description = NSClassDescription(for: NSApplication.self)
  guard let description else {
    #expect(proxy.objectSpecifier == nil)
    return
  }
  let specifier = proxy.objectSpecifier as? NSUniqueIDSpecifier
  #expect(specifier != nil)
  #expect(specifier?.key == MediaAssetScripting.assetsElementKey)
  #expect(specifier?.uniqueID as? String == "asset-spec")
}
