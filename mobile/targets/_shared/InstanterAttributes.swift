import ActivityKit
import Foundation

struct InstanterAttributes: ActivityAttributes {
  public struct ContentState: Codable, Hashable {
    var waiting: Int
    var status: String
  }
}
