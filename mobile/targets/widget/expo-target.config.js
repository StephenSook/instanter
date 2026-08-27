/** @type {import('@bacons/apple-targets/app.plugin').Config} */
module.exports = {
  type: "widget",
  name: "InstanterWidget",
  displayName: "Instanter",
  deploymentTarget: "16.4",
  bundleIdentifier: ".widget",
  frameworks: ["SwiftUI", "WidgetKit", "ActivityKit"],
};
