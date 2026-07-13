use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// A discovered Grimoire-ecosystem app in the watched directory.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AppEntry {
    pub name: String,
    pub dir: PathBuf,
    /// The watched root directory this app was discovered under.
    pub root: PathBuf,
    pub framework_version: Option<String>,
    pub server_command: Option<String>,
    /// Port declared in grimoire-build-info.json environments.local.service_address.
    #[serde(default)]
    pub known_port: Option<u16>,
    /// Label of a launchd LaunchAgent plist found at the app dir root
    /// (e.g. `com.gooncave.server`). When the agent is actually loaded,
    /// start/stop must go through `launchctl` — a plain SIGTERM fights
    /// launchd's KeepAlive, which restarts the process (#53).
    #[serde(default)]
    pub launchd_label: Option<String>,
    /// GitHub repo slug (`owner/name`) for update checks, read from the
    /// grimoire config's issue-tracker block when present (#54). Deploy dirs
    /// are not git checkouts, so the version checker needs an explicit remote.
    #[serde(default)]
    pub repo: Option<String>,
}

/// Running state of a discovered app.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "state")]
pub enum AppStatus {
    Running { pid: u32 },
    Stopped,
    /// App exited unexpectedly — not via Stop or Restart button.
    Crashed,
    Unknown,
}

/// Port information resolved for an app.
#[derive(Debug, Clone, Default, Serialize)]
pub struct PortInfo {
    pub port: Option<u16>,
}

/// Result of a version-update check against the app's remote git tags.
#[derive(Debug, Clone, PartialEq)]
pub enum VersionCheckResult {
    UpToDate,
    UpdateAvailable { latest: String },
    Unknown,
}
