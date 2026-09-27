import Foundation

/// API 키 입력의 UI 모델 — 상태 문면·제출 텍스트 수명이 전부 여기서 정해진다(S2A-2).
///
/// 계약: 값 자체는 어떤 표면에도 나가지 않는다. 상태는 '저장됨/없음/실패'와
/// 교정 동작뿐이고, 성공 저장은 제출 텍스트를 지운다. 저장 연산은 주입받은
/// storage(진짜 Keychain 또는 시험 가짜)만 안다 — Security 프레임워크를 직접
/// 부르지 않는 시험이 강제된다. 오류 문면은 제출된 값을 절대 되뇌지 않는다:
/// 어떤 storage가 값을 오류에 실어 돌려줘도 모델이 그 표면을 마지막으로 가린다.
/// 역할마다 모델을 하나씩 둔다(S2A-2 단위2) — `roleNoun`은 문면의 이름만
/// 갈라놓고, 지우기(clear)는 그 계정만 지운다.
@MainActor
public final class CredentialEntryModel {
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
  /// 역할 이름 — 문면만 갈라진다(예: "API 키" / "비전 키"). 계약은 역할 무관.
  private let roleNoun: String

  public init(
    storage: any SecureKeyValueStorage,
    account: String,
    roleNoun: String = "API 키",
    initialStatus: Status = Status(phase: .idle, message: "")
  ) {
    self.storage = storage
    self.account = account
    self.roleNoun = roleNoun
    self.status = initialStatus
  }

  /// 보안 입력 필드의 반영점 — 화면 어디에도 값 표시가 없다는 뜻은 아니다
  /// (필드 자체가 secure다). 모델이 값을 아는 이유는 저장 뒤 지우기 위해서다.
  public func updateSubmitted(_ text: String) {
    submittedText = text
  }

  /// 저장 동작 — 빈·공백 입력은 거절하고, 성공하면 제출 텍스트를 지운다.
  public func save() {
    let submitted = submittedText
    guard !submitted.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      status = Status(
        phase: .failed, message: "빈 \(roleNoun)는 저장하지 않습니다 — 입력하거나 지우기를 누르세요"
      )
      return
    }
    do {
      try storage.save(submitted, account: account)
    } catch {
      // 저장 실패의 문면도 **정해진 글**이다: 임의 오류 문면을 가려 옮기면
      // 알 수 없는 부분값(예: 뒷자리 몇 글자)까지 새는 문이 남는다. 교정
      // 동작만 말한다.
      status = Status(
        phase: .failed,
        message: "\(roleNoun) 저장에 실패했습니다 — 다시 시도하거나 다시 입력하세요"
      )
      return
    }
    // 성공 — 제출 텍스트는 여기서 끝난다. 남아 있으면 다음 저장이 낡은 값을
    // 다시 제출하는 통로가 된다.
    submittedText = ""
    status = Status(phase: .saved, message: "\(roleNoun)를 저장했습니다 — 다음 실행부터 쓰인다")
  }

  /// 저장된 키를 지운다 — 없는 항목도 멱등 성공이다(S2A-2 단위2).
  ///
  /// 한 역할의 지움이 다른 역할을 건드리지 않는다(계정이 갈라져 있다). 실패
  /// 문면은 정해진 글이다 — 저장돼 있던 값을 모르는 자리에서 임의 오류를 옮기면
  /// 그 값이 새는 문이 생긴다.
  public func clear() {
    do {
      try storage.remove(account: account)
    } catch {
      status = Status(
        phase: .failed, message: "\(roleNoun) 삭제에 실패했습니다 — 다시 시도하세요")
      return
    }
    submittedText = ""
    status = Status(phase: .missing, message: "저장된 \(roleNoun)를 지웠습니다")
  }

  /// 저장된 키의 유무 — 값은 읽되 표면에는 없음/있음만 남는다.
  ///
  /// 읽기 실패의 문면은 **정해진 글**이다: 이 시점에 모델은 어떤 값도 모르므로
  /// 임의의 오류 문면을 그대로 옮기면 저장돼 있던 값이 새어 나갈 문이 생긴다.
  /// 교정 동작(다시 시도)만 말한다.
  public func refresh() {
    let stored: String?
    do {
      stored = try storage.load(account: account)
    } catch {
      status = Status(
        phase: .failed,
        message: "저장된 \(roleNoun)를 확인하지 못했습니다 — 다시 시도하거나 다시 저장하세요"
      )
      return
    }
    status = (stored?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
      ? Status(phase: .saved, message: "저장된 \(roleNoun)가 있습니다")
      : Status(
        phase: .missing, message: "저장된 \(roleNoun)가 없습니다 — 유료 실행 전 저장이 필요하다"
      )
  }

  /// 저장 오류 문면은 어떤 형태로도 모델 밖으로 나가지 않는다 — 저장 실패의
  /// 상태는 정해진 글뿐이다(save 참조). 이 문서 주석은 계약의 이유를 남긴다:
  /// storage마다 오류 모양이 제각각이므로 가림(치환)이 아니라 **거절**이 유일하게
  /// 안전한 경계다.
}
