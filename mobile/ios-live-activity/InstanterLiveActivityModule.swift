import ActivityKit
import Foundation

@objc(InstanterLiveActivity)
class InstanterLiveActivity: NSObject {
  @objc
  func start(_ waiting: NSNumber) {
    guard #available(iOS 16.2, *) else { return }
    let attributes = InstanterAttributes()
    let state = InstanterAttributes.ContentState(waiting: waiting.intValue, status: "awaiting")
    let content = ActivityContent(state: state, staleDate: Date().addingTimeInterval(60 * 60 * 4))
    do {
      _ = try Activity<InstanterAttributes>.request(attributes: attributes, content: content)
    } catch {
      // Native start failed; JS already no-ops missing modules.
    }
  }

  @objc
  func end() {
    guard #available(iOS 16.2, *) else { return }
    Task {
      for activity in Activity<InstanterAttributes>.activities {
        let done = InstanterAttributes.ContentState(waiting: 0, status: "resolved")
        await activity.end(ActivityContent(state: done, staleDate: nil), dismissalPolicy: .immediate)
      }
    }
  }

  @objc
  static func requiresMainQueueSetup() -> Bool { true }
}
