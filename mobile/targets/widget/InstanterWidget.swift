import ActivityKit
import SwiftUI
import WidgetKit

@main
struct InstanterWidgetBundle: WidgetBundle {
  var body: some Widget {
    InstanterLiveActivityWidget()
  }
}

struct InstanterLiveActivityWidget: Widget {
  var body: some WidgetConfiguration {
    ActivityConfiguration(for: InstanterAttributes.self) { context in
      HStack {
        Text("INSTANTER")
          .font(.caption.weight(.bold))
        Spacer()
        if context.state.status == "awaiting" {
          Text("\(context.state.waiting) waiting on an attorney")
            .font(.caption)
        } else {
          Text("Decision recorded")
            .font(.caption)
        }
      }
      .padding()
      .activityBackgroundTint(Color.black)
      .activitySystemActionForegroundColor(Color.white)
    } dynamicIsland: { context in
      DynamicIsland {
        DynamicIslandExpandedRegion(.leading) {
          Text("INSTANTER")
        }
        DynamicIslandExpandedRegion(.trailing) {
          Text(context.state.status == "awaiting"
            ? "\(context.state.waiting) waiting"
            : "Done")
        }
        DynamicIslandExpandedRegion(.bottom) {
          Text(context.state.status == "awaiting"
            ? "A sweep is waiting on an attorney."
            : "Decision recorded.")
        }
      } compactLeading: {
        Text("IN")
      } compactTrailing: {
        Text("\(context.state.waiting)")
      } minimal: {
        Text("IN")
      }
    }
  }
}
