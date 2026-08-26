/** @type {import('@bacons/apple-targets/app.plugin').Config} */
module.exports = {
  type: "widget",
  name: "InstanterWidget",
  displayName: "Instanter",
  deploymentTarget: "16.2",
  bundleIdentifier: ".widget",
  frameworks: ["SwiftUI", "WidgetKit", "ActivityKit"],
  entitlements: {
    "com.apple.developer.usernotifications.live-activities": true,
  },
};
