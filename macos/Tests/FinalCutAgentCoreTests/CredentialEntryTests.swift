import Foundation
import Testing

@testable import FinalCutAgentCore

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-2 — 자격 증명 저장·입력 모델 · 사적 운반 봉투
// ─────────────────────────────────────────────────────────────

/// 시험 전용 가짜 저장 — Security 프레임워크를 절대 부르지 않는다(계약).
/// 오류를 일부러 키 문면에 실어 돌려주는 모드도 있다: 모델의 마지막 가림막을
/// 증명하는 재료다.
final class FakeKeyValueStorage: SecureKeyValueStorage, @unchecked Sendable {
  enum Failure: Error {
    case loadFails
    case saveFails
    case removeFails
  }

  private(set) var saved: [String: String] = [:]
  private(set) var saveCalls: [(value: String, account: String)] = []
  private(set) var removeCalls: [String] = []
  var failSave: Bool = false
  var failLoad: Bool = false
  var failRemove: Bool = false

  func save(_ value: String, account: String) throws {
    saveCalls.append((value, account))
    if failSave { throw Failure.saveFails }
    saved[account] = value
  }

  func load(account: String) throws -> String? {
    if failLoad { throw Failure.loadFails }
    return saved[account]
  }

  func remove(account: String) throws {
    removeCalls.append(account)
    if failRemove { throw Failure.removeFails }
    saved[account] = nil
  }
}

/// 오류 문면에 제출된 값을 실어 버리는 저장 — 모델의 가림막 증명용.
struct EchoingKeyValueStorage: SecureKeyValueStorage {
  func save(_ value: String, account: String) throws {
    throw NSError(
      domain: "synthetic-storage", code: 1,
      userInfo: [NSLocalizedDescriptionKey: "거절됨: \(value)를 저장할 수 없다"])
  }

  func load(account: String) throws -> String? { nil }

  func remove(account: String) throws {}
}

/// 읽기 오류 문면에 **저장돼 있던** 값을 실어 버리는 저장 — 이 시점에 모델은
/// 그 값을 모르므로 문면 가림으로는 막을 수 없다. 정해진 실패 글로만 말해야
/// 한다(모델 계약). 저장은 성공한다 — 값이 실제로 들어간 다음에 죽는 길이다.
final class EchoingLoadStorage: SecureKeyValueStorage, @unchecked Sendable {
  private var stored: String = ""

  func save(_ value: String, account: String) throws {
    stored = value
  }

  func load(account: String) throws -> String? {
    throw NSError(
      domain: "synthetic-storage", code: 2,
      userInfo: [NSLocalizedDescriptionKey: "복호화 실패: \(stored)"])
  }

  func remove(account: String) throws {}
}

/// 지움 오류 문면에 값을 실어 버리는 저장 — clear의 정해진 실패 글 증명용.
final class EchoingRemoveStorage: SecureKeyValueStorage, @unchecked Sendable {
  func save(_ value: String, account: String) throws {}

  func load(account: String) throws -> String? { nil }

  func remove(account: String) throws {
    throw NSError(
      domain: "synthetic-storage", code: 3,
      userInfo: [NSLocalizedDescriptionKey: "지움 거절: \(account) 값을 지울 수 없다"])
  }
}

@MainActor
@Test func apiKeyEntryModelSavesAndClearsSubmittedText() throws {
  let storage = FakeKeyValueStorage()
  let model = CredentialEntryModel(storage: storage, account: "engine")

  model.refresh()
  #expect(model.status.phase == .missing)

  model.updateSubmitted("synthetic-first-key")
  model.save()
  #expect(model.status.phase == .saved)
  #expect(model.submittedText == "")  // 성공 저장은 제출 텍스트를 지운다
  #expect(storage.saved["engine"] == "synthetic-first-key")

  // 교체 — 이전 값 위에 새 값이 들어가고 상태는 여전히 저장됨.
  model.updateSubmitted("synthetic-replacement-key")
  model.save()
  #expect(storage.saved["engine"] == "synthetic-replacement-key")
  #expect(storage.saveCalls.count == 2)

  // 저장 없이 새로고침 — 있음 상태가 말하는 것은 유무뿐이다.
  model.refresh()
  #expect(model.status.phase == .saved)
  #expect(!model.status.message.contains("synthetic"))
}

@MainActor
@Test func apiKeyEntryModelRejectsEmptyAndWhitespaceOnlyInput() throws {
  let storage = FakeKeyValueStorage()
  let model = CredentialEntryModel(storage: storage, account: "engine")

  model.updateSubmitted("")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(storage.saveCalls.isEmpty)

  model.updateSubmitted("   \n\t ")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(storage.saveCalls.isEmpty)
}

@MainActor
@Test func apiKeyEntryModelReportsStorageFailureWithoutEchoingSubmittedValue() throws {
  // 저장 실패 — 상태 문면에 제출값이 실리면 안 된다.
  let echoing = EchoingKeyValueStorage()
  let model = CredentialEntryModel(storage: echoing, account: "engine")
  model.updateSubmitted("synthetic-leaky-key")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(!model.status.message.contains("synthetic-leaky-key"))
  #expect(model.submittedText == "synthetic-leaky-key")  // 실패는 제출을 지우지 않는다

  // 읽기 실패의 문면은 정해진 글이다 — 저장돼 있던 값을 모르는 자리에서 임의
  // 오류 문면을 옮기면 그 값이 새는 문이다(SDK형 부분 반향 포함).
  let echoingLoad = EchoingLoadStorage()
  let stored = CredentialEntryModel(storage: echoingLoad, account: "engine")
  stored.updateSubmitted("synthetic-stored-key")
  stored.save()
  stored.refresh()
  #expect(stored.status.phase == .failed)
  #expect(!stored.status.message.contains("synthetic-stored-key"))
  #expect(!stored.status.message.contains("synthetic"))
  // 저장 실패도 정해진 글이다 — 부분값 반향까지 원천 막는다(가림이 아니라 거절).
  #expect(
    model.status.message
      == "API 키 저장에 실패했습니다 — 다시 시도하거나 다시 입력하세요")

  // 단순 읽기 실패도 실패 상태로 나간다 — 교정 동작이 문면에 있다.
  let failing = FakeKeyValueStorage()
  failing.failLoad = true
  let reload = CredentialEntryModel(storage: failing, account: "engine")
  reload.refresh()
  #expect(reload.status.phase == .failed)
}

@Test func credentialEnvelopeWireMatchesTheEngineReaderContract() throws {
  // 봉투 바이트는 엔진 진입점이 읽는 모양 그대로다: 마법줄·버전·십진 **바이트**
  // 길이·본문·끝표식 줄바꿈. 파이촌 쪽 판독 시험(test_engine_carrier.py)과 짝을
  // 이룬다 — 끝표식이 있어 읽는 쪽은 쓰기 끝의 닫힘 시점과 갈라선다.
  let key = try EngineCredentialEnvelope.key("synthetic-wire-key").wire
  #expect(
    String(decoding: key, as: UTF8.self) == "VLOGSRT-ENGINE-CARRIER 1\n18\nsynthetic-wire-key\n")

  let none = try EngineCredentialEnvelope.explicitNoKey.wire
  #expect(String(decoding: none, as: UTF8.self) == "VLOGSRT-ENGINE-CARRIER 1\n0\n\n")

  // 길이는 바이트 수다 — 한글 값이 문자 수로 세여 잘리지 않는다.
  let hangul = try EngineCredentialEnvelope.key("합성-키").wire
  #expect(
    String(decoding: hangul, as: UTF8.self)
      == "VLOGSRT-ENGINE-CARRIER 1\n\(Data("합성-키".utf8).count)\n합성-키\n")

  // 상한을 넘는 키는 봉투가 되지 않는다 — 실행기가 시작 전에 실패 닫힌다.
  let oversized = String(repeating: "k", count: EngineCredentialEnvelope.maximumByteCount + 1)
  #expect(throws: DevelopmentEngineError.credentialEnvelopeTooLarge) {
    _ = try EngineCredentialEnvelope.key(oversized).wire
  }
  // NUL이 섞인 키도 만들지 않는다 — C 세계의 문자열 경계를 찾는 길이다.
  #expect(throws: DevelopmentEngineError.credentialEnvelopeContainsNUL) {
    _ = try EngineCredentialEnvelope.key("synthetic\u{0}nul-key").wire
  }
  // 상한 안 최대 키는 만들어진다 — 파이촌 판독기 상한과 같은 값이다.
  let maximal = String(repeating: "k", count: EngineCredentialEnvelope.maximumByteCount)
  let wire = try EngineCredentialEnvelope.key(maximal).wire
  #expect(wire.count == "VLOGSRT-ENGINE-CARRIER 1\n8192\n".utf8.count + 8192 + 1)
}

// ─────────────────────────────────────────────────────────────
//  STAB-S2A-2 단위2 — 역할별 저장·지우기 · 비전 엔드포인트 · V2 페이로드
// ─────────────────────────────────────────────────────────────

@MainActor
@Test func rolesSaveAndClearIndependentlyOverOneStorage() throws {
  /// 한 저장 위의 두 역할 — 한쪽 교체·지움이 다른 쪽을 건드리지 않는다.
  let storage = FakeKeyValueStorage()
  let stt = CredentialEntryModel(
    storage: storage, account: EngineRoleCredentialStore.sttAccount)
  let vision = CredentialEntryModel(
    storage: storage, account: EngineRoleCredentialStore.visionAccount, roleNoun: "비전 키")

  stt.updateSubmitted("synthetic-stt-key")
  stt.save()
  vision.updateSubmitted("synthetic-vision-key")
  vision.save()
  #expect(storage.saved["engine"] == "synthetic-stt-key")
  #expect(storage.saved["vision"] == "synthetic-vision-key")

  // STT 교체는 비전 값을 그대로 둔다.
  stt.updateSubmitted("synthetic-stt-replacement")
  stt.save()
  #expect(storage.saved["vision"] == "synthetic-vision-key")

  // STT 지우기도 비전 값을 그대로 둔다 — 계정이 갈라져 있다.
  stt.clear()
  #expect(stt.status.phase == .missing)
  #expect(storage.saved["vision"] == "synthetic-vision-key")
  #expect(storage.removeCalls == ["engine"])
  vision.refresh()
  #expect(vision.status.phase == .saved)

  // 비전 문면은 역할 이름으로 말한다 — STT 문면과 갈라진 표시다.
  #expect(vision.status.message.contains("비전 키"))
  #expect(!vision.status.message.contains("synthetic"))
}

@MainActor
@Test func clearingAMissingKeyIsAnIdempotentSuccess() throws {
  /// 없는 항목 지우기도 성공이다 — remove가 불렸고 상태는 부재다.
  let storage = FakeKeyValueStorage()
  let model = CredentialEntryModel(storage: storage, account: "engine")

  model.clear()

  #expect(storage.removeCalls == ["engine"])
  #expect(model.status.phase == .missing)
  #expect(model.submittedText == "")
}

@MainActor
@Test func clearFailureUsesFixedTextAndNeverEchoesValues() throws {
  /// 지움 오류 문면이 계정·값을 실어 돌려줘도 상태는 정해진 글뿐이다.
  let echoing = EchoingRemoveStorage()
  let model = CredentialEntryModel(storage: echoing, account: "engine")

  model.clear()

  #expect(model.status.phase == .failed)
  #expect(model.status.message == "API 키 삭제에 실패했습니다 — 다시 시도하세요")
  #expect(!model.status.message.contains("engine"))
}

@Test func visionEndpointValidatorAcceptsBlankAndPlainHTTPS() throws {
  /// 빈 값은 부재(정상) — https + 호스트(+범위 안 포트)는 통과한다.
  #expect(VisionEndpointValidator.validate("", knownSecrets: []) == nil)
  #expect(
    VisionEndpointValidator.validate("https://vision.example/v1", knownSecrets: []) == nil)
  #expect(
    VisionEndpointValidator.validate("https://vision.example:8443/v1", knownSecrets: []) == nil)
}

@Test func visionEndpointValidatorClosesPortsDelimitersAndEncoding() throws {
  /// 포트·빈 구분자·퍼센트 부호형·해석 불가 모양 — 파이촌 관문과 같은 규칙.
  let cases: [(String, VisionEndpointError)] = [
    ("http://vision.example/v1", .notHTTPS),
    ("vision.example/v1", .notHTTPS),
    ("https:/vision.example", .missingHost),
    ("https://", .missingHost),
    ("https://provider.invalid:99999/v1", .portOutOfRange),
    ("https://user:pass@vision.example/v1", .hasUserinfo),
    ("https://user@vision.example/v1", .hasUserinfo),
    ("https://vision.example/v1?q=1", .hasQueryOrFragment),
    ("https://vision.example/v1#frag", .hasQueryOrFragment),
    ("https://vision.example/v1?", .hasQueryOrFragment),
    ("https://vision.example/v1#", .hasQueryOrFragment),
    ("https://vision.example/v1 path", .hasControlsOrWhitespace),
    ("https://vision.example/v1%20path", .hasControlsOrWhitespace),
    ("https://vision.example/v1%00path", .hasControlsOrWhitespace),
    ("https://[::1/v1", .malformed),
  ]
  for (endpoint, expected) in cases {
    #expect(
      VisionEndpointValidator.validate(endpoint, knownSecrets: []) == expected,
      "엔드포인트 규칙 위반이 기대와 다르다")
  }
}

@Test func visionEndpointValidatorRejectsKnownSecretMaterialInEveryForm() throws {
  /// 키 재료는 원문·퍼센트해독형·퍼센트부호화형 어느 모양으로도 못 들어간다.
  let secret = "synthetic/vision-key"
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/v1/synthetic/vision-key", knownSecrets: [secret])
      == .containsSecretMaterial)
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/v1%2Fsynthetic%2Fvision%2Dkey", knownSecrets: [secret])
      == .containsSecretMaterial)
  // 퍼센트해독형 — URL 안의 %2F가 해독되면 키 원문이 된다.
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/prefix%2Fsynthetic%2Fvision-key", knownSecrets: [secret])
      == .containsSecretMaterial)
  // 재료가 없으면 같은 자리가 아니라는 뜻이다 — 통과.
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/v1/synthetic/vision-key", knownSecrets: [])
      == nil)
}

@MainActor
@Test func visionEndpointEntryModelSavesValidatesAndClears() throws {
  /// 저장·검증·지우기 수명 — 상태 문면에 URL 값은 절대 나오지 않는다.
  let storage = FakeKeyValueStorage()
  let secrets = Box(value: "synthetic-stored-key")
  let model = VisionEndpointEntryModel(
    storage: storage, knownSecrets: { [secrets] in [secrets.value] })

  model.refresh()
  #expect(model.status.phase == .missing)

  // 빈 입력은 거절 — 부재는 지우기 버튼으로만 만든다.
  model.updateSubmitted("   ")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(storage.saveCalls.isEmpty)

  // 잘못된 스킴 — 정해진 글뿐, URL은 없다.
  model.updateSubmitted("http://synthetic-endpoint.example/v1")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(!model.status.message.contains("synthetic-endpoint"))
  #expect(storage.saveCalls.isEmpty)

  // 저장된 키 재료가 겹치는 엔드포인트 — 문을 닫는다.
  model.updateSubmitted("https://host.example/synthetic-stored-key")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(storage.saveCalls.isEmpty)

  // 정상 저장 — 제출 텍스트는 지우고 상태는 있음.
  model.updateSubmitted("https://synthetic-vision.example/v1")
  model.save()
  #expect(model.status.phase == .saved)
  #expect(model.submittedText == "")
  #expect(storage.saved["vision-base-url"] == "https://synthetic-vision.example/v1")
  #expect(!model.status.message.contains("synthetic-vision.example"))

  // 지우기 — 멱등이고 계정 하나만 건드린다.
  model.clear()
  #expect(model.status.phase == .missing)
  #expect(storage.removeCalls == ["vision-base-url"])
  model.refresh()
  #expect(model.status.phase == .missing)
}

@MainActor
@Test func visionEndpointEntryModelHidesStorageFailuresBehindFixedText() throws {
  /// 저장·읽기 실패는 정해진 글이다 — 오류 문면을 옮기지 않는다.
  let failing = FakeKeyValueStorage()
  failing.failLoad = true
  let model = VisionEndpointEntryModel(storage: failing)
  model.refresh()
  #expect(model.status.phase == .failed)
  #expect(!model.status.message.contains("synthetic"))

  failing.failLoad = false
  failing.failSave = true
  model.updateSubmitted("https://synthetic-vision.example/v1")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(model.status.message == "엔드포인트 저장에 실패했습니다 — 다시 시도하세요")

  failing.failSave = false
  failing.failRemove = true
  model.clear()
  #expect(model.status.phase == .failed)
  #expect(model.status.message == "엔드포인트 삭제에 실패했습니다 — 다시 시도하세요")
}

/// 클로저 캡처용 값 상자 — knownSecrets 공급자가 바뀌는 값을 전달하게 한다.
final class Box: @unchecked Sendable {
  var value: String
  init(value: String) { self.value = value }
}

@Test func roleCredentialStoreSnapshotReadsThreeAccounts() throws {
  /// 스냅숏은 세 계정을 한 덩어리로 — 빈 값은 부재일 뿐 실패가 아니다.
  /// 준비 상태는 저장 계약(save)으로 만든다 — 가짜의 내부를 직접 열지 않는다.
  let storage = FakeKeyValueStorage()
  try storage.save("synthetic-stt-key", account: "engine")
  try storage.save("synthetic-vision-key", account: "vision")
  try storage.save("https://synthetic-vision.example/v1", account: "vision-base-url")
  let store = EngineRoleCredentialStore(storage: storage)

  guard case .ready(let credentials) = store.loadSnapshot() else {
    Issue.record("ready여야 한다")
    return
  }
  #expect(credentials.sttKey == "synthetic-stt-key")
  #expect(credentials.visionKey == "synthetic-vision-key")
  #expect(credentials.visionBaseURL == "https://synthetic-vision.example/v1")

  // 한 계정도 없으면 부재 — 부재 스냅숏은 여전히 ready다(추정·러프컷은 키 없이).
  let empty = FakeKeyValueStorage()
  guard case .ready(let absent) = EngineRoleCredentialStore(storage: empty).loadSnapshot()
  else {
    Issue.record("빈 저장도 ready(부재)여야 한다")
    return
  }
  #expect(absent == EngineRoleCredentials())

  // 읽기 실패는 storageFailed — 값을 모르는 자리의 정해진 실패다.
  let failing = FakeKeyValueStorage()
  failing.failLoad = true
  #expect(EngineRoleCredentialStore(storage: failing).loadSnapshot() == .storageFailed)
}

@Test func roleCredentialStoreRevalidatesTheEndpointAgainstCurrentKeys() throws {
  /// 실행 직전 재검증 — 저장 때 통과한 값도 오늘의 키와 겹치면 막힌다.
  /// 회전 실측: 같은 엔드포인트가 낡은 키 아래에서는 통과하다가, 교체된 새 키
  /// 전체가 그 안에 들어 있는 순간 막힌다 — 검사 대상은 언제나 **현재** 키다.
  let endpoint = "https://host.example/deploy/synthetic-rotated-key"
  let beforeRotation = EngineRoleCredentials(
    sttKey: "synthetic-previous-stt-key",
    visionKey: "synthetic-previous-vision-key",
    visionBaseURL: endpoint)
  #expect(EngineRoleCredentialStore.launchBlocker(for: beforeRotation) == nil)

  let afterRotation = EngineRoleCredentials(
    sttKey: "synthetic-previous-stt-key",
    visionKey: "synthetic-rotated-key",
    visionBaseURL: endpoint)
  let blocker = try #require(EngineRoleCredentialStore.launchBlocker(for: afterRotation))
  #expect(blocker == VisionEndpointError.containsSecretMaterial.message)
  #expect(!blocker.contains("synthetic-rotated-key"))

  // 정상 값은 그대로 통과 — 재검증이 정상 경로를 막지 않는다.
  let clean = EngineRoleCredentials(
    sttKey: "synthetic-stt-key",
    visionKey: "synthetic-vision-key",
    visionBaseURL: "https://synthetic-vision.example/v1")
  #expect(EngineRoleCredentialStore.launchBlocker(for: clean) == nil)

  // 저장 실패 문면도 정해진 글이다.
  #expect(
    EngineRoleSnapshot.storageFailedMessage.contains("읽지 못했습니다"))
}

@Test func roleCredentialsPayloadEncodesDeterministicallyWithinTheBound() throws {
  /// V2 JSON 페이로드 — 결정적 키 순서, 상한은 부호화 포함, NUL은 거절.
  let roles = EngineRoleCredentials(
    sttKey: "synthetic-stt",
    visionKey: "synthetic-vision",
    visionBaseURL: "https://vision.example/v1")
  let payload = try roles.encodedPayload()
  #expect(
    String(decoding: payload, as: UTF8.self)
      == "{\"stt_key\":\"synthetic-stt\",\"vision_base_url\":\"https://vision.example/v1\","
        + "\"vision_key\":\"synthetic-vision\"}")

  // 상한 안 최대 — JSON 오버헤드를 포함해 정확히 8192바이트까지.
  let overhead = "{\"stt_key\":\"\",\"vision_base_url\":\"\",\"vision_key\":\"\"}".utf8.count
  let fitting = EngineRoleCredentials(
    sttKey: String(repeating: "k", count: EngineCredentialEnvelope.maximumByteCount - overhead))
  #expect(try fitting.encodedPayload().count == EngineCredentialEnvelope.maximumByteCount)

  // 한 바이트라도 넘으면 만들지 않는다 — 부호화 오버헤드가 상한을 넘겼다.
  let overflowing = EngineRoleCredentials(
    sttKey: String(repeating: "k", count: EngineCredentialEnvelope.maximumByteCount - overhead + 1))
  #expect(throws: DevelopmentEngineError.credentialEnvelopeTooLarge) {
    _ = try overflowing.encodedPayload()
  }

  // 값 안의 NUL — 어느 역할이든 거절한다.
  #expect(throws: DevelopmentEngineError.credentialEnvelopeContainsNUL) {
    _ = try EngineRoleCredentials(visionKey: "synthetic\u{0}nul").encodedPayload()
  }
}

@Test func credentialEnvelopeCarriesRolesAsV2Wire() throws {
  /// V2 봉투 바이트 — 마법줄·버전 2·바이트 길이·JSON·끝표식. 파이촌 판독기의
  /// `_ROLE_CARRIER_VERSION`와 같은 모양이다.
  let roles = EngineRoleCredentials(
    sttKey: "synthetic-wire-stt",
    visionKey: "synthetic-wire-vision",
    visionBaseURL: "https://vision.example/v1")
  let payload = try roles.encodedPayload()
  let wire = try EngineCredentialEnvelope.roles(roles).wire
  let expected =
    "VLOGSRT-ENGINE-CARRIER 2\n\(payload.count)\n".data(using: .utf8)! + payload
    + Data("\n".utf8)
  #expect(wire == expected)

  // V1 와이어는 그대로다 — 낡은 계약이 여전히 성립한다.
  #expect(
    String(decoding: try EngineCredentialEnvelope.key("legacy-key").wire, as: UTF8.self)
      == "VLOGSRT-ENGINE-CARRIER 1\n10\nlegacy-key\n")
}

@MainActor
@Test func endpointSaveFailsClosedWhenKeyMaterialCannotBeRead() throws {
  /// 키 재료를 읽지 못하면 검증을 지나가지 않는다 — 저장도 없다(실패 닫힘).
  let storage = FakeKeyValueStorage()
  let secrets = ThrowingSecrets()
  let model = VisionEndpointEntryModel(
    storage: storage, knownSecrets: { [secrets] in try secrets.values() })

  secrets.fail = true
  model.updateSubmitted("https://synthetic-vision.example/v1")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(
    model.status.message
      == "저장된 키를 확인하지 못해 엔드포인트를 검증할 수 없습니다 — 키를 다시 저장한 뒤 시도하세요")
  #expect(storage.saveCalls.isEmpty)  // 검증 못한 값은 저장되지 않는다

  secrets.fail = false
  model.save()  // 같은 제출값이 재료를 얻은 뒤에는 정상적으로 지나간다
  #expect(model.status.phase == .saved)
  #expect(storage.saved["vision-base-url"] == "https://synthetic-vision.example/v1")
}

@MainActor
@Test func endpointSaveValidatesAgainstUnsavedSubmittedKeysToo() throws {
  /// 저장되지 않았지만 필드에 남아 있는 제출 키도 키 재료다 — 반향 통로를 닫는다.
  let storage = FakeKeyValueStorage()
  try storage.save("stored-stt-material", account: "engine")
  let unsaved = "unsaved-but-submitted-vision-key"
  let model = VisionEndpointEntryModel(
    storage: storage, knownSecrets: { ["stored-stt-material", unsaved] })

  // 저장된 STT 키가 겹치는 엔드포인트 — 저장 재료로 거절(기존 규칙).
  model.updateSubmitted("https://host.example/stored-stt-material")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(storage.saved["vision-base-url"] == nil)  // 엔드포인트 자리는 안 생긴다

  // 막 제출되어 아직 저장 안 된 비전 키가 겹치는 엔드포인트 — 같은 문으로 거절.
  model.updateSubmitted("https://host.example/prefix-\(unsaved)")
  model.save()
  #expect(model.status.phase == .failed)
  #expect(!model.status.message.contains(unsaved))
  #expect(storage.saved["vision-base-url"] == nil)
}

/// 던지는 키 재료 공급자 — 읽기 실패의 실패 닫힘을 증명하는 재료.
final class ThrowingSecrets: @unchecked Sendable {
  var fail = false

  func values() throws -> [String] {
    if fail { throw FakeKeyValueStorage.Failure.loadFails }
    return ["synthetic-material"]
  }
}

@Test func visionEndpointValidatorRejectsTrimmedSecretMaterial() throws {
  /// 붙여 넣은 공백이 달린 키도 재료다 — 다듬은 값으로 URL을 견준다.
  let padded = "  synthetic-padded-secret \n"
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/v1/synthetic-padded-secret", knownSecrets: [padded])
      == .containsSecretMaterial)
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/v1%2Fsynthetic%2Dpadded%2Dsecret", knownSecrets: [padded])
      == .containsSecretMaterial)
  // 재료가 아니면 통과 — 다듬은 값 후보가 정상 엔드포인트를 막지 않는다.
  #expect(
    VisionEndpointValidator.validate(
      "https://vision.example/v1", knownSecrets: [padded])
      == nil)
}
