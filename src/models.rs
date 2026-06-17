use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// A discovered Grimoire-ecosystem app in the watched directory.
#[derive(Debug, Clone, Serialize, Deserialize)]
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
