import Foundation

/// V2 사적 운반자의 논리 역할 값 — STT 키·비전 키·비전 base URL(S2A-2 단위2).
///
/// 세 값은 언제나 함께 다닌다: 빈 문자열은 **권위적 부재**다(상속·dotenv 부활
/// 없음). 역할마다 값이 독립이므로 한 역할을 지워도 다른 역할은 그대로 산다.
/// 값은 이 구조체 안에서만 살고, 어떤 문면에도 되뇌어지지 않는다.
public struct EngineRoleCredentials: Equatable, Sendable {
  public static let sttKeyField = "stt_key"
  public static let visionKeyField = "vision_key"
  public static let visionBaseURLField = "vision_base_url"

  public let sttKey: String
  public let visionKey: String
  public let visionBaseURL: String

  public init(sttKey: String = "", visionKey: String = "", visionBaseURL: String = "") {
    self.sttKey = sttKey
    self.visionKey = visionKey
    self.visionBaseURL = visionBaseURL
  }

  /// V2 페이로드의 JSON 부호화 — 결정적 키 순서, 상한은 부호화 **포함** 바이트다.
  ///
  /// NUL이 섞인 값은 만들지 않는다(C 문자열 경계). 파이썬 판독기와 같은
  /// 8192바이트 상한이 JSON 따옴표·이스케이프까지 포함해 잰다.
  public func encodedPayload() throws -> Data {
    for field in [sttKey, visionKey, visionBaseURL] where field.contains("\0") {
      throw DevelopmentEngineError.credentialEnvelopeContainsNUL
    }
    let object: [String: String] = [
      Self.sttKeyField: sttKey,
      Self.visionKeyField: visionKey,
      Self.visionBaseURLField: visionBaseURL,
    ]
    let payload = try JSONSerialization.data(
      withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    guard payload.count <= EngineCredentialEnvelope.maximumByteCount else {
      throw DevelopmentEngineError.credentialEnvelopeTooLarge
    }
    return payload
  }
}

/// 비전 엔드포인트 검증 오류 — 문면은 정해진 교정 글이고 URL을 절대 되뇌지 않는다.
public enum VisionEndpointError: Error, Equatable, Sendable {
  case notHTTPS
  case missingHost
  case portOutOfRange
  case hasUserinfo
  case hasQueryOrFragment
  case hasControlsOrWhitespace
  case malformed
  case containsSecretMaterial

  public var message: String {
    switch self {
    case .notHTTPS:
      "비전 base URL은 https:// 로 시작해야 한다 — http·상대 경로는 쓸 수 없다"
    case .missingHost:
      "비전 base URL에 호스트가 없다 — 엔드포인트를 다시 확인하세요"
    case .portOutOfRange:
      "비전 base URL의 포트가 범위(0~65535)를 벗어난다 — 엔드포인트를 다시 확인하세요"
    case .hasUserinfo:
      "비전 base URL에 사용자 정보(userinfo)가 들어 있다 — 자격증명은 URL로 보내지 않는다"
    case .hasQueryOrFragment:
      "비전 base URL에 쿼리·프래그먼트는 들어갈 수 없다"
    case .hasControlsOrWhitespace:
      "비전 base URL에 공백·제어 문자가 들어 있다 — 엔드포인트를 다시 확인하세요"
    case .malformed:
      "비전 base URL이 해석되지 않는다 — 엔드포인트를 다시 확인하세요"
    case .containsSecretMaterial:
      "비전 base URL에 자격증명 값이 들어 있다 — 엔드포인트에서 키를 빼세요"
    }
  }
}

/// 비전 엔드포인트의 순수 검증기 — 앱 저장 시점과 실행 직전이 같은 규칙을 쓴다.
///
/// 규칙은 파이썬 관문(`vlogsrt.env.validate_vision_base_url`)과 갈라져 있지
/// 않다: https + 호스트, 포트 범위, userinfo·쿼리·프래그먼트(빈 구분자 포함),
/// 제어 문자·NUL·공백(퍼센트 부호형 포함), 해석 불가 모양, 알려진 키 재료
/// (원문·퍼센트해독형·퍼센트부호화형). 빈 값은 부재로 정상이다.
public enum VisionEndpointValidator {
  /// 빈 값은 nil(정상·권위적 부재). 비지 않은 값은 첫 위반을 돌려준다.
  public static func validate(
    _ raw: String, knownSecrets: [String]
  ) -> VisionEndpointError? {
    let text = raw
    guard !text.isEmpty else { return nil }
    if containsForbiddenScalars(text) || text.removingPercentEncoding.map(containsForbiddenScalars) == true {
      return .hasControlsOrWhitespace
    }
    // 구분자가 **있으면** 닫는다 — 뒤가 비어도 조용히 지나가지 않는다.
    if text.contains("?") || text.contains("#") { return .hasQueryOrFragment }
    guard let components = URLComponents(string: text) else { return .malformed }
    guard components.scheme?.lowercased() == "https" else { return .notHTTPS }
    guard let host = components.host, !host.isEmpty else { return .missingHost }
    if let port = components.port, !(0...65535).contains(port) {
      return .portOutOfRange
    }
    if components.user != nil || components.password != nil { return .hasUserinfo }
    if components.query != nil || components.fragment != nil { return .hasQueryOrFragment }
    for secret in knownSecrets where !secret.isEmpty {
      // 다듬은 값도 재료다 — 공백을 붙여 넣은 키가 검사를 흉내 내게 두지 않는다
      // (소비자는 strip한 값으로 키를 쓴다).
      let materials = [secret, secret.trimmingCharacters(in: .whitespacesAndNewlines)]
      for material in materials where !material.isEmpty {
        if text.contains(material) { return .containsSecretMaterial }
        if let decoded = text.removingPercentEncoding, decoded.contains(material) {
          return .containsSecretMaterial
        }
        if let encoded = material.addingPercentEncoding(withAllowedCharacters: .alphanumerics),
          text.contains(encoded)
        {
          return .containsSecretMaterial
        }
      }
    }
    return nil
  }

  /// 공백·제어(C0/C1)·서식 문자(Cf) — 부호화되지 않은 문면에서 잰다.
  private static func containsForbiddenScalars(_ text: String) -> Bool {
    text.unicodeScalars.contains { scalar in
      CharacterSet.whitespacesAndNewlines.contains(scalar)
        || CharacterSet.controlCharacters.contains(scalar)
        || scalar.properties.generalCategory == .format
    }
  }
}

/// 세 저장 값을 한 덩어리로 읽는 문 — 실행 직전 스냅숏(S2A-2 단위2).
///
/// 하나라도 읽지 못하면 `storageFailed`다: 이 시점에 호출자는 값들을 모르므로
/// 정해진 실패 글로만 말하고 실행은 없다. 빈 값은 부재일 뿐 실패가 아니다 —
/// 추정·러프컷은 키 없이도 돈다. 계정은 각자 독립이다(engine·vision·
/// vision-base-url) — 한 역할을 지워도 다른 계정은 그대로다.
public struct EngineRoleCredentialStore: Sendable {
  public static let sttAccount = "engine"
  public static let visionAccount = "vision"
  public static let visionBaseURLAccount = "vision-base-url"

  public let storage: any SecureKeyValueStorage

  public init(storage: any SecureKeyValueStorage) {
    self.storage = storage
  }

  public func loadSnapshot() -> EngineRoleSnapshot {
    do {
      let stt = try storage.load(account: Self.sttAccount) ?? ""
      let vision = try storage.load(account: Self.visionAccount) ?? ""
      let endpoint = try storage.load(account: Self.visionBaseURLAccount) ?? ""
      return .ready(
        EngineRoleCredentials(sttKey: stt, visionKey: vision, visionBaseURL: endpoint))
    } catch {
      return .storageFailed
    }
  }

  /// 실행 직전 재검증 — 저장된 엔드포인트가 **오늘의** 키 재료와 충돌하지 않는지.
  ///
  /// 저장 때 통과한 값이라도 키가 바뀐 뒤에는 다시 봐야 한다(짧은 키가 새 긴
  /// 키의 앞부분이 되는 일). 정상이면 nil, 문제가 있으면 정해진 글을 돌려준다.
  public static func launchBlocker(for credentials: EngineRoleCredentials) -> String? {
    VisionEndpointValidator.validate(
      credentials.visionBaseURL,
      knownSecrets: [credentials.sttKey, credentials.visionKey]
    )?.message
  }
}

/// 스냅숏의 두 결말 — 준비됨(값 포함) 또는 정해진 실패 글.
public enum EngineRoleSnapshot: Equatable, Sendable {
  case ready(EngineRoleCredentials)
  case storageFailed

  public static let storageFailedMessage =
    "저장된 자격 증명을 읽지 못했습니다 — 키를 다시 저장한 뒤 실행하세요"
}

/// 비전 base URL 입력의 UI 모델 — 엔드포인트는 비밀이 아니다(S2A-2 단위2).
///
/// 키 입력 모델(CredentialEntryModel)과 같은 수명 계약: 값은 상태 문면에 절대
/// 나가지 않고, 성공 저장은 제출 텍스트를 지운다. 다른 점 두 가지 — 값이
/// 비밀이 아니어도 **문면 반향은 없다**(오류는 URL을 되뇌지 않는다), 저장 전에
/// 검증기가 문을 닫는다(빈 입력은 '지우기' 버튼으로만 부재가 된다). 저장된
/// 값은 앱 재시작에도 산다(별도 계정 vision-base-url).
@MainActor
public final class VisionEndpointEntryModel {
  public struct Status: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
      case idle
      case saved
      case missing
      case failed
    }

    public let phase: Phase
    public let message: String

    public init(phase: Phase, message: String) {
      self.phase = phase
      self.message = message
    }
  }

  public private(set) var status: Status
  public private(set) var submittedText: String = ""

  private let storage: any SecureKeyValueStorage
  private let account: String
  /// 키 재료 공급자 — 저장된 값 **와** 막 제출된 값 둘 다 말하게 한다. 읽기
  /// 실패는 던진다: 재료를 모르는 채 검증을 지나가는 것이 더 위험하기 때문이다.
  private let knownSecrets: @MainActor () throws -> [String]

  public init(
    storage: any SecureKeyValueStorage,
    account: String = EngineRoleCredentialStore.visionBaseURLAccount,
    knownSecrets: @escaping @MainActor () throws -> [String] = { [] as [String] },
    initialStatus: Status = Status(phase: .idle, message: "")
  ) {
    self.storage = storage
    self.account = account
    self.knownSecrets = knownSecrets
    self.status = initialStatus
  }

  public func updateSubmitted(_ text: String) {
    submittedText = text
  }

  /// 저장 동작 — 빈 입력은 거절(부재는 지우기 버튼), 검증을 통과해야 들어간다.
  ///
  /// 키 재료를 읽지 못하면 **실패로 닫는다**: 검증 없이 저장하면 방금 제출된
  /// 키가 엔드포인트에 섞여 들어간 채 살아남는다. 어떤 경우에도 실패한 검증
  /// 뒤에는 저장이 없다.
  public func save() {
    let submitted = submittedText.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !submitted.isEmpty else {
      status = Status(
        phase: .failed,
        message: "빈 주소는 저장하지 않습니다 — 지우려면 '엔드포인트 지우기'를 누르세요")
      return
    }
    let secrets: [String]
    do {
      secrets = try knownSecrets()
    } catch {
      status = Status(
        phase: .failed,
        message: "저장된 키를 확인하지 못해 엔드포인트를 검증할 수 없습니다 — 키를 다시 저장한 뒤 시도하세요")
      return
    }
    if let violation = VisionEndpointValidator.validate(submitted, knownSecrets: secrets) {
      // 오류 문면은 정해진 글이다 — URL을 조립해 되뇌면 그 자체가 반향이다.
      status = Status(phase: .failed, message: violation.message)
      return
    }
    do {
      try storage.save(submitted, account: account)
    } catch {
      status = Status(
        phase: .failed, message: "엔드포인트 저장에 실패했습니다 — 다시 시도하세요")
      return
    }
    submittedText = ""
    status = Status(phase: .saved, message: "비전 엔드포인트를 저장했습니다 — 다음 실행부터 쓰인다")
  }

  /// 부재 만들기 — 항목이 없어도 멱등 성공이다. 문면은 정해진 글뿐이다.
  public func clear() {
    do {
      try storage.remove(account: account)
    } catch {
      status = Status(
        phase: .failed, message: "엔드포인트 삭제에 실패했습니다 — 다시 시도하세요")
      return
    }
    submittedText = ""
    status = Status(phase: .missing, message: "저장된 비전 엔드포인트를 지웠습니다")
  }

  /// 저장된 값의 유무 — 값 자체는 읽되 표면에는 없음/있음만 남는다.
  public func refresh() {
    let stored: String?
    do {
      stored = try storage.load(account: account)
    } catch {
      status = Status(
        phase: .failed,
        message: "저장된 엔드포인트를 확인하지 못했습니다 — 다시 시도하거나 다시 저장하세요"
      )
      return
    }
    status = (stored?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
      ? Status(phase: .saved, message: "저장된 비전 엔드포인트가 있습니다")
      : Status(
        phase: .missing,
        message: "저장된 비전 엔드포인트가 없습니다 — 유료 분석 전 입력이 필요하다")
  }
}
