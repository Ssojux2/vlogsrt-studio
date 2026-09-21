import AppKit
import Testing

@testable import WorkflowExtension

// Final Cut Workflow SDK(ProExtensionRequestHandling)는 XPC 응답 큐에서
// initWithNibName:bundle:로 주 클래스를 만든다. 2026-09-21 .ips의 크래시는
// Swift 6가 MainActor 서브클래스의 @objc init thunk에 심은 dispatch_assert_queue가
// 그 큐에서 실패한 것이다(EXC_BREAKPOINT). 이 검사는 SDK의 호출 경로를
// 메타클래스 alloc + perform로 그대로 되풀이한다.
@Test @MainActor func sdkInstantiatesPrincipalViewControllerOffMainWithoutTrap() throws {
  let done = DispatchSemaphore(value: 0)
  nonisolated(unsafe) var madeBox: AnyObject?

  DispatchQueue.global().async {
    let metaclass = WorkflowViewController.self as AnyObject
    let allocated = metaclass.perform(NSSelectorFromString("alloc"))!
      .takeUnretainedValue()
    madeBox = allocated
      .perform(NSSelectorFromString("initWithNibName:bundle:"), with: nil, with: nil)?
      .takeRetainedValue()
    done.signal()
  }

  #expect(done.wait(timeout: .now() + 10) == .success)
  let made = try #require(madeBox as? WorkflowViewController)

  // UI 구성은 뷰 로드(메인 액터)에서 처음 일어나야 한다.
  _ = made.view
  #expect(!made.view.subviews.isEmpty)
}
