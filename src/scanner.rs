use crate::detector;
use crate::models::{AppEntry, AppStatus, PortInfo};
use crate::perf::write_perf_log;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::watch;
use tokio::task::JoinSet;
use tracing::{debug, info, warn};

/// Combined scan result: discovered entry, detected status, and port info.
pub type ScanResult = Vec<(AppEntry, AppStatus, PortInfo)>;

/// Starts a background tokio task that scans all `roots` every `interval`,
/// runs per-app detection concurrently, and sends the combined results through
/// the returned `watch::Receiver`. Also returns a `watch::Sender<()>` — send
/// any value to trigger an immediate scan without waiting for the interval.
/// Force-scan signals received while a scan is already in-flight are silently
/// dropped to prevent queuing redundant scans.
pub fn start(
    roots: Vec<PathBuf>,
    interval: Duration,
) -> (watch::Receiver<ScanResult>, watch::Sender<()>) {
    let (tx, rx) = watch::channel(Vec::new());
    let (force_tx, mut force_rx) = watch::channel(());
    let in_flight = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let in_flight_force = Arc::clone(&in_flight);

    tokio::spawn(async move {
        loop {
            in_flight.store(true, std::sync::atomic::Ordering::SeqCst);
            let t0 = Instant::now();
            let entries = scan_all(&roots);
            info!(
                "scan complete: {} app(s) across {} root(s)",
                entries.len(),
                roots.len()
            );

            // Run per-app detector calls concurrently with a 2 s timeout each.
            let results = run_detectors_concurrent(entries).await;
            let cycle_ms = t0.elapsed().as_millis();

            // Find slowest app by name for telemetry (not tracked individually yet; emit blank).
            write_perf_log(cycle_ms, 0u32, "");

            let _ = tx.send(results);
            in_flight.store(false, std::sync::atomic::Ordering::SeqCst);

            tokio::select! {
                _ = tokio::time::sleep(interval) => {}
                _ = force_rx.changed() => {
                    // Force-scan woke us; check wasn't in-flight at this point
                    // since we just cleared the flag above.
                }
            }
        }
    });

    // Wrap force_tx so that sends while a scan is in-flight are dropped.
    let raw_force_tx = force_tx;
    // Build a new channel pair for the caller; a relay task forwards non-in-flight triggers.
    let (caller_force_tx, mut caller_force_rx) = watch::channel(());
    tokio::spawn(async move {
        loop {
            if caller_force_rx.changed().await.is_err() {
                break;
            }
            if in_flight_force.load(std::sync::atomic::Ordering::SeqCst) {
                debug!("force-scan dropped: scan already in-flight");
            } else {
                let _ = raw_force_tx.send(());
            }
        }
    });

    (rx, caller_force_tx)
}

/// Run `detector::detect` for each entry concurrently using a `JoinSet`,
/// with a 2-second timeout per app. Timed-out apps are marked Unknown.
async fn run_detectors_concurrent(
    entries: Vec<AppEntry>,
) -> Vec<(AppEntry, AppStatus, PortInfo)> {
    let mut join_set: JoinSet<(AppEntry, AppStatus, PortInfo)> = JoinSet::new();

    for entry in entries {
        join_set.spawn(async move {
            let entry_for_detect = entry.clone();
            let detect_task = tokio::task::spawn_blocking(move || detector::detect(&entry_for_detect));
            match tokio::time::timeout(Duration::from_secs(2), detect_task).await {
                Ok(Ok((status, port_info))) => (entry, status, port_info),
                Ok(Err(_join_err)) => {
                    warn!("detector task panicked for {}", entry.name);
                    (entry, AppStatus::Unknown, PortInfo { port: None })
                }
                Err(_timeout) => {
                    debug!("detector timed out for {}", entry.name);
                    (entry, AppStatus::Unknown, PortInfo { port: None })
                }
            }
        });
    }

    let mut results = Vec::new();
    while let Some(res) = join_set.join_next().await {
        if let Ok(triple) = res {
            results.push(triple);
        }
        // Err(_) means the task panicked; already handled inside the spawned closure.
    }
    // Sort by app name, then dir, so the list order is a deterministic total
    // order across scan cycles — including when two apps in different watched
    // roots share the same name (dir is always unique).
    results.sort_by(|a, b| a.0.name.cmp(&b.0.name).then_with(|| a.0.dir.cmp(&b.0.dir)));
    results
}

/// Scans all `roots` synchronously and returns a combined list of `AppEntry` records.
/// Each entry's `root` field identifies which root directory it came from.
pub fn scan_all(roots: &[PathBuf]) -> Vec<AppEntry> {
    roots.iter().flat_map(|root| scan_once(root)).collect()
}

/// Scans `root` synchronously and returns discovered `AppEntry` records.
/// Each entry's `root` field is set to `root`.
pub fn scan_once(root: &Path) -> Vec<AppEntry> {
    let read = match std::fs::read_dir(root) {
        Ok(r) => r,
        Err(e) => {
            warn!("cannot read apps dir {}: {}", root.display(), e);
            return Vec::new();
        }
    };

    let mut apps = Vec::new();
    for entry in read.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        if is_app_dir(&dir) {
            let mut app = parse_app_dir(&dir);
            app.root = root.to_path_buf();
            debug!("discovered app: {} at {}", app.name, dir.display());
            apps.push(app);
        }
    }
    apps
}

/// Returns true if `dir` looks like a deployed or project-root app.
fn is_app_dir(dir: &Path) -> bool {
    dir.join("grimoire-build-info.json").exists()
        || dir.join("grimoire-config.json").exists()
        || dir.join("current").symlink_metadata().is_ok()
        || dir.join("versions").is_dir()
        || dir.join("start.sh").exists()
}

fn parse_app_dir(dir: &Path) -> AppEntry {
    // Precedence: a deployed build-info (root or `current/` symlink) wins over
    // project-source `grimoire-config.json` so the deployed `app_version` and
    // service address are surfaced; the bare dir-name fallback is last.
    let mut entry = parse_from_build_info(dir)
        .or_else(|| parse_from_grimoire_config(dir))
        .unwrap_or_else(|| {
            // Fallback: use directory name, no metadata.
            // `root` is populated by `scan_once` after this call.
            AppEntry {
                name: dir_name(dir),
                dir: dir.to_path_buf(),
                root: PathBuf::new(),
                framework_version: None,
                server_command: read_server_command(dir),
                known_port: None,
                launchd_label: launchd_label(dir),
                repo: None,
            }
        });
    // Deployed-version fallbacks (#54): several instances have no
    // grimoire-build-info.json at all — read the actual deployed version from
    // current/release.json, else the current -> versions/vX.Y.Z symlink name.
    if entry.framework_version.is_none() {
        entry.framework_version = deployed_version(dir);
    }
    entry
}

/// Read the deployed version of a `current/`-layout instance (#54):
/// 1. `current/release.json` `.version` (the asserting-publisher asset), else
/// 2. the basename of the `current` symlink target (`versions/v1.102.0`),
///    with a leading `v` stripped for consistency with app_version strings.
fn deployed_version(dir: &Path) -> Option<String> {
    let release_json = dir.join("current").join("release.json");
    if let Ok(text) = std::fs::read_to_string(&release_json) {
        if let Ok(val) = serde_json::from_str::<Value>(&text) {
            if let Some(v) = val.get("version").and_then(Value::as_str) {
                return Some(v.trim_start_matches('v').to_string());
            }
        }
    }
    let target = std::fs::read_link(dir.join("current")).ok()?;
    let base = target.file_name()?.to_str()?;
    let version = base.trim_start_matches('v');
    // Only trust the symlink name when it actually looks like a version.
    if version.chars().next().is_some_and(|c| c.is_ascii_digit()) {
        Some(version.to_string())
    } else {
        None
    }
}

/// Label of a LaunchAgent plist at the app-dir root, when present (#53).
fn launchd_label(dir: &Path) -> Option<String> {
    crate::launchd::find_agent(dir).map(|a| a.label)
}

/// Parse from Grimoire deployment build info (`grimoire-build-info.json`).
/// Checks `dir/grimoire-build-info.json` first, then `dir/current/grimoire-build-info.json`
/// for apps deployed with a versioned `current/` symlink structure.
fn parse_from_build_info(dir: &Path) -> Option<AppEntry> {
    let build_info_path = {
        let direct = dir.join("grimoire-build-info.json");
        let via_current = dir.join("current").join("grimoire-build-info.json");
        if direct.exists() {
            direct
        } else if via_current.exists() {
            via_current
        } else {
            return None;
        }
    };
    let text = std::fs::read_to_string(build_info_path).ok()?;
    let val: Value = serde_json::from_str(&text).ok()?;

    let cfg = val.get("grimoire_config")?;
    let name = cfg
        .get("name")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| dir_name(dir));

    // Prefer app_version (the deployed release) over framework_version.
    let framework_version = val
        .get("app_version")
        .and_then(Value::as_str)
        .map(str::to_string);

    let known_port = cfg
        .pointer("/environments/local/service_address")
        .and_then(Value::as_str)
        .and_then(parse_port_from_url);

    Some(AppEntry {
        name,
        dir: dir.to_path_buf(),
        root: PathBuf::new(),
        framework_version,
        server_command: read_server_command(dir),
        known_port,
        launchd_label: launchd_label(dir),
        repo: repo_from_grimoire_config(cfg),
    })
}

/// Extract the `owner/name` GitHub slug from a grimoire config value's
/// issue-tracker block (#54). Deploy dirs are not git checkouts, so this is
/// how the version checker learns which remote to query.
fn repo_from_grimoire_config(cfg: &Value) -> Option<String> {
    cfg.pointer("/issue-tracker/trackers/0/repo")
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Parse from Grimoire project source config (`grimoire-config.json`).
fn parse_from_grimoire_config(dir: &Path) -> Option<AppEntry> {
    let text = std::fs::read_to_string(dir.join("grimoire-config.json")).ok()?;
    let val: Value = serde_json::from_str(&text).ok()?;

    let fallback = dir_name(dir);
    let name = val
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(&fallback)
        .to_string();

    let framework_version = val
        .get("framework-version")
        .and_then(Value::as_str)
        .map(str::to_string);

    Some(AppEntry {
        name,
        dir: dir.to_path_buf(),
        root: PathBuf::new(),
        framework_version,
        server_command: read_server_command(dir),
        known_port: None,
        launchd_label: launchd_label(dir),
        repo: repo_from_grimoire_config(&val),
    })
}

fn read_server_command(dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(dir.join("recipes.json")).ok()?;
    let val: Value = serde_json::from_str(&text).ok()?;
    val.pointer("/targets/server/command")
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Parse the port number out of a URL like `http://localhost:8080`.
fn parse_port_from_url(url: &str) -> Option<u16> {
    let after_scheme = url.split("//").nth(1)?;
    let host_port = after_scheme.split('/').next()?;
    let port_str = host_port.split(':').nth(1)?;
    port_str.parse().ok()
}

fn dir_name(dir: &Path) -> String {
    dir.file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("unknown")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::Arc;
    use tempfile::TempDir;

    fn make_app(
        root: &Path,
        name: &str,
        framework_version: Option<&str>,
        server_cmd: Option<&str>,
    ) {
        let app_dir = root.join(name);
        fs::create_dir_all(&app_dir).unwrap();

        let mut config = serde_json::json!({"name": name});
        if let Some(v) = framework_version {
            config["framework-version"] = serde_json::Value::String(v.to_string());
        }
        fs::write(
            app_dir.join("grimoire-config.json"),
            config.to_string(),
        )
        .unwrap();

        if let Some(cmd) = server_cmd {
            let recipes =
                serde_json::json!({"targets": {"server": {"command": cmd}}});
            fs::write(app_dir.join("recipes.json"), recipes.to_string()).unwrap();
        }
    }

    #[test]
    fn discovers_apps_with_grimoire_config() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();

        make_app(&root, "my-app", Some("3.36"), Some("node server.js"));
        fs::create_dir_all(root.join("not-an-app")).unwrap();

        let apps = scan_once(&root);
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "my-app");
        assert_eq!(apps[0].framework_version.as_deref(), Some("3.36"));
        assert_eq!(apps[0].server_command.as_deref(), Some("node server.js"));
        assert_eq!(apps[0].root, root);
    }

    #[test]
    fn falls_back_to_dir_name_when_config_missing_name() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();

        let app_dir = root.join("my-unnamed-app");
        fs::create_dir_all(&app_dir).unwrap();
        fs::write(app_dir.join("grimoire-config.json"), "{}").unwrap();

        let apps = scan_once(&root);
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "my-unnamed-app");
        assert!(apps[0].framework_version.is_none());
        assert!(apps[0].server_command.is_none());
    }

    #[test]
    fn discovers_app_via_grimoire_build_info() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("my-deployed-app");
        fs::create_dir_all(&app_dir).unwrap();

        let build_info = serde_json::json!({
            "schema_version": 1,
            "app_version": "1.2.3",
            "grimoire_config": {
                "name": "my-deployed-app",
                "environments": {
                    "local": {
                        "service_address": "http://localhost:7700"
                    }
                }
            }
        });
        fs::write(
            app_dir.join("grimoire-build-info.json"),
            build_info.to_string(),
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "my-deployed-app");
        assert_eq!(apps[0].framework_version.as_deref(), Some("1.2.3"));
        assert_eq!(apps[0].known_port, Some(7700));
    }

    #[test]
    fn discovers_app_via_current_dir() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("legacy-app");
        fs::create_dir_all(app_dir.join("current")).unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "legacy-app");
    }

    /// Regression (v1.0.1): apps deployed with a versioned `current/` layout
    /// keep `grimoire-build-info.json` under `current/`, not at the app root.
    /// The version must still be read so it displays in the GUI.
    #[test]
    fn discovers_version_via_current_build_info() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("deployed-app");
        fs::create_dir_all(app_dir.join("current")).unwrap();

        let build_info = serde_json::json!({
            "schema_version": 1,
            "app_version": "2.5.0",
            "grimoire_config": {
                "name": "deployed-app",
                "environments": {
                    "local": { "service_address": "http://localhost:8800" }
                }
            }
        });
        fs::write(
            app_dir.join("current").join("grimoire-build-info.json"),
            build_info.to_string(),
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "deployed-app");
        assert_eq!(apps[0].framework_version.as_deref(), Some("2.5.0"));
        assert_eq!(apps[0].known_port, Some(8800));
    }

    /// Versioned layout WITHOUT grimoire-build-info.json (familiar/issue-tracker
    /// shape, #54): the deployed version comes from current/release.json.
    #[test]
    fn reads_version_from_current_release_json() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("familiar");
        fs::create_dir_all(app_dir.join("current")).unwrap();
        fs::write(
            app_dir.join("current").join("release.json"),
            r#"{"version": "1.102.0", "target_triple": "aarch64-apple-darwin"}"#,
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].framework_version.as_deref(), Some("1.102.0"));
    }

    /// Versioned layout with neither build-info nor release.json (harmony
    /// shape, #54): the version falls back to the current -> versions/vX.Y.Z
    /// symlink target name, with the leading v stripped.
    #[test]
    fn reads_version_from_current_symlink_target() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("harmony");
        fs::create_dir_all(app_dir.join("versions").join("v0.39.0")).unwrap();
        std::os::unix::fs::symlink(
            app_dir.join("versions").join("v0.39.0"),
            app_dir.join("current"),
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].framework_version.as_deref(), Some("0.39.0"));
    }

    /// A `current` symlink pointing at a non-version name must not be
    /// mistaken for a version.
    #[test]
    fn non_version_symlink_target_yields_no_version() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("odd-app");
        fs::create_dir_all(app_dir.join("builds").join("latest")).unwrap();
        std::os::unix::fs::symlink(
            app_dir.join("builds").join("latest"),
            app_dir.join("current"),
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert!(apps[0].framework_version.is_none());
    }

    /// The repo slug for update checks (#54) is read from the grimoire
    /// config's issue-tracker block in grimoire-build-info.json.
    #[test]
    fn reads_repo_slug_from_build_info_issue_tracker() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("discord-bot");
        fs::create_dir_all(&app_dir).unwrap();
        let build_info = serde_json::json!({
            "schema_version": 1,
            "app_version": "0.8.0",
            "grimoire_config": {
                "name": "Discord-bot",
                "issue-tracker": {
                    "trackers": [
                        {"name": "default", "provider": "github", "repo": "rhohn94/discord-bot"}
                    ]
                }
            }
        });
        fs::write(
            app_dir.join("grimoire-build-info.json"),
            build_info.to_string(),
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].repo.as_deref(), Some("rhohn94/discord-bot"));
    }

    /// Flat-layout app dir with a LaunchAgent plist (goon-cave shape, #53):
    /// discovered via start.sh, and the plist Label is surfaced on the entry.
    #[test]
    fn discovers_launchd_label_from_app_dir_plist() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("goon-cave");
        fs::create_dir_all(&app_dir).unwrap();
        fs::write(app_dir.join("start.sh"), "#!/bin/sh\n").unwrap();
        fs::write(
            app_dir.join("com.gooncave.server.plist"),
            r#"<plist><dict><key>Label</key><string>com.gooncave.server</string></dict></plist>"#,
        )
        .unwrap();

        let apps = scan_once(&tmp.path().to_path_buf());
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "goon-cave");
        assert_eq!(apps[0].launchd_label.as_deref(), Some("com.gooncave.server"));
    }

    #[test]
    fn parses_port_from_url() {
        assert_eq!(
            parse_port_from_url("http://localhost:8080"),
            Some(8080)
        );
        assert_eq!(
            parse_port_from_url("http://localhost:8080/api"),
            Some(8080)
        );
        assert_eq!(parse_port_from_url("http://localhost"), None);
    }

    #[test]
    fn removed_app_absent_on_rescan() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();

        make_app(&root, "app-a", None, None);
        make_app(&root, "app-b", None, None);

        let first = scan_once(&root);
        assert_eq!(first.len(), 2);

        fs::remove_dir_all(root.join("app-b")).unwrap();

        let second = scan_once(&root);
        assert_eq!(second.len(), 1);
        assert_eq!(second[0].name, "app-a");
        assert!(!second.iter().any(|e| e.name == "app-b"));
    }

    #[tokio::test]
    async fn force_scan_wakes_before_interval() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();
        make_app(&root, "app-x", None, None);

        // Use a very long interval so the test would stall without a force trigger.
        let (mut rx, force_tx) = start(vec![root], Duration::from_secs(3600));

        // Wait for the first scan to arrive (scanner sends before sleeping).
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while !rx.has_changed().unwrap_or(false) {
            assert!(
                std::time::Instant::now() < deadline,
                "timed out waiting for initial scan"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        rx.borrow_and_update(); // consume the first scan

        // Trigger a force scan; the scanner should deliver a second update
        // well within 5 seconds despite the 3600s interval.
        force_tx.send(()).unwrap();
        let timeout = tokio::time::sleep(Duration::from_secs(5));
        tokio::pin!(timeout);
        loop {
            tokio::select! {
                _ = &mut timeout => panic!("force scan did not arrive within 5 seconds"),
                _ = rx.changed() => {
                    let apps = rx.borrow_and_update().clone();
                    assert_eq!(apps.len(), 1);
                    assert_eq!(apps[0].0.name, "app-x");
                    break;
                }
            }
        }
    }

    #[test]
    fn scan_all_combines_multiple_roots() {
        let tmp1 = TempDir::new().unwrap();
        let tmp2 = TempDir::new().unwrap();
        let root1 = tmp1.path().to_path_buf();
        let root2 = tmp2.path().to_path_buf();

        make_app(&root1, "app-alpha", None, None);
        make_app(&root2, "app-beta", None, None);

        let apps = scan_all(&[root1.clone(), root2.clone()]);
        assert_eq!(apps.len(), 2);

        let alpha = apps.iter().find(|e| e.name == "app-alpha").unwrap();
        let beta = apps.iter().find(|e| e.name == "app-beta").unwrap();
        assert_eq!(alpha.root, root1);
        assert_eq!(beta.root, root2);
    }

    #[test]
    fn scan_all_single_root_matches_scan_once() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();

        make_app(&root, "app-solo", Some("1.0"), Some("node index.js"));

        let from_once = scan_once(&root);
        let from_all = scan_all(&[root.clone()]);
        assert_eq!(from_once.len(), from_all.len());
        assert_eq!(from_once[0].name, from_all[0].name);
        assert_eq!(from_once[0].root, root);
        assert_eq!(from_all[0].root, root);
    }

    #[test]
    fn stale_removal_per_root_independent() {
        let tmp1 = TempDir::new().unwrap();
        let tmp2 = TempDir::new().unwrap();
        let root1 = tmp1.path().to_path_buf();
        let root2 = tmp2.path().to_path_buf();

        make_app(&root1, "app-keep", None, None);
        make_app(&root1, "app-gone", None, None);
        make_app(&root2, "app-stable", None, None);

        let first = scan_all(&[root1.clone(), root2.clone()]);
        assert_eq!(first.len(), 3);

        // Remove app-gone from root1 only.
        fs::remove_dir_all(root1.join("app-gone")).unwrap();

        let second = scan_all(&[root1.clone(), root2.clone()]);
        assert_eq!(second.len(), 2);
        assert!(second.iter().any(|e| e.name == "app-keep"));
        assert!(second.iter().any(|e| e.name == "app-stable"));
        assert!(!second.iter().any(|e| e.name == "app-gone"));
    }

    /// Verify that a force-scan fired while the in-flight flag is set is
    /// dropped rather than queued. A relay task drops the trigger and emits
    /// a tracing::debug! log; we confirm by counting how many scans arrive
    /// during a window where the flag is manually held high.
    #[tokio::test]
    async fn force_scan_dropped_when_in_flight() {
        use std::sync::atomic::AtomicBool;

        // Build the same relay logic used in `start()` but driven by a test-
        // controlled in_flight flag so we can hold it true for the entire test.
        let in_flight = Arc::new(AtomicBool::new(false));
        let in_flight_relay = Arc::clone(&in_flight);

        let (inner_force_tx, mut inner_force_rx) = watch::channel::<()>(());
        let (caller_force_tx, mut caller_force_rx) = watch::channel::<()>(());

        // Counter incremented each time a force trigger reaches the inner channel.
        let pass_count = Arc::new(AtomicU32::new(0));
        let pass_count_task = Arc::clone(&pass_count);

        // Spawn the relay task (same logic as in `start()`).
        tokio::spawn(async move {
            loop {
                if caller_force_rx.changed().await.is_err() {
                    break;
                }
                if in_flight_relay.load(Ordering::SeqCst) {
                    debug!("force-scan dropped: scan already in-flight");
                } else {
                    let _ = inner_force_tx.send(());
                    pass_count_task.fetch_add(1, Ordering::SeqCst);
                }
            }
        });

        // Drain the initial sentinel value so changed() only fires on real sends.
        let _ = inner_force_rx.borrow_and_update();

        // ── Case 1: in_flight = false → trigger should pass through ──────────
        in_flight.store(false, Ordering::SeqCst);
        caller_force_tx.send(()).unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            pass_count.load(Ordering::SeqCst),
            1,
            "trigger should pass when not in-flight"
        );

        // ── Case 2: in_flight = true → trigger should be dropped ─────────────
        in_flight.store(true, Ordering::SeqCst);
        caller_force_tx.send(()).unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            pass_count.load(Ordering::SeqCst),
            1, // still 1; the second send was dropped
            "trigger should be dropped when in-flight"
        );

        // ── Case 3: flag cleared → next trigger passes ────────────────────────
        in_flight.store(false, Ordering::SeqCst);
        caller_force_tx.send(()).unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            pass_count.load(Ordering::SeqCst),
            2,
            "trigger should pass again after in-flight cleared"
        );
    }
}
