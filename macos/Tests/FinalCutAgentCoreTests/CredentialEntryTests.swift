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
  }

  private(set) var saved: [String: String] = [:]
  private(set) var saveCalls: [(value: String, account: String)] = []
  var failSave: Bool = false
  var failLoad: Bool = false

  func save(_ value: String, account: String) throws {
    saveCalls.append((value, account))
    if failSave { throw Failure.saveFails }
    saved[account] = value
  }

  func load(account: String) throws -> String? {
    if failLoad { throw Failure.loadFails }
    return saved[account]
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
      == "키 저장에 실패했습니다 — 다시 시도하거나 키를 다시 입력하세요")

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
