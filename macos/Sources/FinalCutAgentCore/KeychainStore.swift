import Foundation
import Security

/// 저장 연산의 좁은 문 — 진짜 Keychain과 시험 가짜가 같은 계약을 쓴다(S2A-2).
/// 구현체가 둘(실물·가짜) 생겼으므로 이 추상화는 미리 만든 것이 아니다.
/// 어떤 구현도 오류 문면에 제출된 값을 실어서는 안 된다 — 모델이 그 표면을
/// 다시 가린다(CredentialEntryModel).
public protocol SecureKeyValueStorage: Sendable {
  func save(_ value: String, account: String) throws
  func load(account: String) throws -> String?
  /// 항목을 지운다 — 없어도 멱등 성공이다(S2A-2 단위2). 역할마다 독립이라
  /// 한 계정을 지워도 다른 계정은 그대로다.
  func remove(account: String) throws
}

public struct KeychainStore: Sendable, SecureKeyValueStorage {
  public let service: String

  public init(service: String = "com.ssojux2.vlogsrt.api-keys") {
    self.service = service
  }

  public func save(_ value: String, account: String) throws {
    let data = Data(value.utf8)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
    ]
    // 먼저 갱신한다 — 실패해도 이전 키가 지워지지 않는다(교체 보존 계약, S2A-2).
    // 없던 항목일 때만 삽입한다. 지우고 다시 넣는 옛 길은 삽입이 실패하면
    // 이전 키까지 묻혀 지웠다. 갱신 문은 바뀐 값 속성만 실을 수 있다 —
    // class·service·account는 찾는 쪽(query)에만 있다.
    let updateStatus = SecItemUpdate(
      query as CFDictionary,
      [kSecValueData as String: data] as CFDictionary
    )
    if updateStatus == errSecSuccess { return }
    guard updateStatus == errSecItemNotFound else {
      throw KeychainError.status(updateStatus)
    }
    var insert = query
    insert[kSecValueData as String] = data
    let status = SecItemAdd(insert as CFDictionary, nil)
    guard status == errSecSuccess else { throw KeychainError.status(status) }
  }

  public func load(account: String) throws -> String? {
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
      kSecReturnData as String: true,
      kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = result as? Data,
      let value = String(data: data, encoding: .utf8)
    else { throw KeychainError.status(status) }
    return value
  }

  public func remove(account: String) throws {
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
    ]
    let status = SecItemDelete(query as CFDictionary)
    // 없는 항목은 이미 목표 상태다 — 지움 동작의 멱등 성공이다.
    guard status == errSecSuccess || status == errSecItemNotFound else {
      throw KeychainError.status(status)
    }
  }
}

public enum KeychainError: Error {
  case status(OSStatus)
}
