const { IOSConfig, withInfoPlist, withXcodeProject } = require("@expo/config-plugins");
const fs = require("fs");
const path = require("path");

/**
 * Adds the ActivityKit native module to the main app target.
 * The WidgetKit Live Activity UI lives in targets/widget via
 * @bacons/apple-targets. Lock-screen text is only a count.
 */
function withInstanterLiveActivity(config) {
  config = withInfoPlist(config, (mod) => {
    mod.modResults.NSSupportsLiveActivities = true;
    return mod;
  });
  config = withXcodeProject(config, (mod) => {
    const project = mod.modResults;
    const projectName = IOSConfig.XcodeUtils.getProjectName(mod.modRequest.projectRoot);
    const iosRoot = mod.modRequest.platformProjectRoot;
    const src = path.join(mod.modRequest.projectRoot, "ios-live-activity");
    const destDir = path.join(iosRoot, projectName, "InstanterLiveActivity");
    fs.mkdirSync(destDir, { recursive: true });
    for (const file of ["InstanterLiveActivityModule.swift", "InstanterLiveActivityBridge.m"]) {
      const from = path.join(src, file);
      const rel = path.join(projectName, "InstanterLiveActivity", file);
      fs.copyFileSync(from, path.join(iosRoot, rel));
      if (!project.hasFile(rel)) {
        IOSConfig.XcodeUtils.addBuildSourceFileToGroup({
          filepath: rel,
          groupName: `${projectName}/InstanterLiveActivity`,
          project,
        });
      }
    }
    IOSConfig.XcodeUtils.addFramework({
      project,
      projectName,
      framework: "ActivityKit.framework",
    });
    return mod;
  });
  return config;
}

module.exports = withInstanterLiveActivity;
