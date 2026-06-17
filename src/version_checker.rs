// VersionChecker: background task that periodically checks each app's
// framework version against the latest git tag on its remote.

use crate::models::{AppEntry, VersionCheckResult};
use std::{
    collections::HashMap,
    sync::{Arc, RwLock},
};

/// Runs a periodic background sweep that checks each monitored app's
/// `framework_version` against the latest git tag visible on the remote.
/// Results are stored in a shared map keyed by app name.
pub struct VersionChecker {
    results: Arc<RwLock<HashMap<String, VersionCheckResult>>>,
}

impl VersionChecker {
    /// Create a new `VersionChecker` with an empty results map.
    pub fn new() -> Self {
        VersionChecker {
            results: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Return the shared results map so the UI can read it without copying.
    pub fn results(&self) -> Arc<RwLock<HashMap<String, VersionCheckResult>>> {
        Arc::clone(&self.results)
    }

    /// Spawn a background tokio task that sweeps `entries` every `interval_secs`.
    ///
    /// If `interval_secs` is 0 the method is a no-op (version checking disabled).
    /// The task runs inside the provided `runtime` handle so it integrates with
    /// the existing single tokio runtime in `main`.
    pub fn start(
        &self,
        entries: Vec<AppEntry>,
        interval_secs: u64,
        runtime: tokio::runtime::Handle,
    ) {
        if interval_secs == 0 {
            return;
        }

        let results = Arc::clone(&self.results);
        runtime.spawn(async move {
            loop {
                for entry in &entries {
                    let result = check_entry(entry).await;
                    if let Ok(mut map) = results.write() {
                        map.insert(entry.name.clone(), result);
                    }
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(interval_secs)).await;
            }
        });
    }
}

/// Run `git ls-remote --tags --sort=-version:refname origin` in the app
/// directory and compare the latest tag to the app's recorded framework version.
async fn check_entry(entry: &AppEntry) -> VersionCheckResult {
    let output = tokio::process::Command::new("git")
        .args([
            "-C",
            entry.dir.to_string_lossy().as_ref(),
            "ls-remote",
            "--tags",
            "--sort=-version:refname",
            "origin",
        ])
        .output()
        .await;

    let output = match output {
        Ok(o) => o,
        Err(_) => return VersionCheckResult::Unknown,
    };

    if !output.status.success() {
        return VersionCheckResult::Unknown;
    }

    let stdout = match std::str::from_utf8(&output.stdout) {
        Ok(s) => s,
        Err(_) => return VersionCheckResult::Unknown,
    };

    // Each line is: "<sha>\trefs/tags/<tag>"
    // After `--sort=-version:refname` the first non-peeled tag is the latest.
    let latest_tag = stdout
        .lines()
        .filter_map(|line| {
            let tab = line.find('\t')?;
            let refname = &line[tab + 1..];
            // Skip peeled refs (refs/tags/<tag>^{})
            if refname.ends_with("^{}") {
                return None;
            }
            refname.strip_prefix("refs/tags/").map(str::to_owned)
        })
        .next();

    let latest_tag = match latest_tag {
        Some(t) => t,
        None => return VersionCheckResult::Unknown,
    };

    match &entry.framework_version {
        Some(current) if current == &latest_tag => VersionCheckResult::UpToDate,
        Some(_) => VersionCheckResult::UpdateAvailable { latest: latest_tag },
        None => VersionCheckResult::UpdateAvailable { latest: latest_tag },
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_produces_empty_results() {
        let checker = VersionChecker::new();
        let arc = checker.results();
        let map = arc.read().unwrap();
        assert!(map.is_empty(), "fresh VersionChecker must have no results");
    }

    #[test]
    fn test_update_available_reads_back_correctly() {
        let checker = VersionChecker::new();
        {
            let arc = checker.results();
            let mut map = arc.write().unwrap();
            map.insert(
                "my-app".to_string(),
                VersionCheckResult::UpdateAvailable {
                    latest: "v0.5.0".to_string(),
                },
            );
        }
        let arc = checker.results();
        let map = arc.read().unwrap();
        match map.get("my-app") {
            Some(VersionCheckResult::UpdateAvailable { latest }) => {
                assert_eq!(latest, "v0.5.0");
            }
            other => panic!("expected UpdateAvailable, got {:?}", other),
        }
    }
}
