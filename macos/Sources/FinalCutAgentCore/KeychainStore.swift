import Foundation
import Security

public struct KeychainStore: Sendable {
  public let service: String

  public init(service: String = "com.ssojux2.vlogsrt.api-keys") {
    self.service = service
  }

  public func save(_ value: String, account: String) throws {
    let data = Data(value.utf8)
    let key: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
    ]
    SecItemDelete(key as CFDictionary)
    var insert = key
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
}

public enum KeychainError: Error {
  case status(OSStatus)
}
