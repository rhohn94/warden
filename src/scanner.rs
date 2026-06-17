use crate::models::AppEntry;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::sync::watch;
use tracing::{debug, info, warn};

/// Starts a background tokio task that scans all `roots` every `interval` and
/// sends the combined discovered apps through the returned `watch::Receiver`.
/// Also returns a `watch::Sender<()>` — send any value to trigger an
/// immediate scan without waiting for the interval.
pub fn start(
    roots: Vec<PathBuf>,
    interval: Duration,
) -> (watch::Receiver<Vec<AppEntry>>, watch::Sender<()>) {
    let (tx, rx) = watch::channel(Vec::new());
    let (force_tx, mut force_rx) = watch::channel(());
    tokio::spawn(async move {
        loop {
            let apps = scan_all(&roots);
            info!(
                "scan complete: {} app(s) across {} root(s)",
                apps.len(),
                roots.len()
            );
            let _ = tx.send(apps);
            tokio::select! {
                _ = tokio::time::sleep(interval) => {}
                _ = force_rx.changed() => {}
            }
        }
    });
    (rx, force_tx)
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
    if let Some(entry) = parse_from_build_info(dir) {
        return entry;
    }
    if let Some(entry) = parse_from_grimoire_config(dir) {
        return entry;
    }
    // Fallback: use directory name, no metadata.
    // `root` is populated by `scan_once` after this call.
    let name = dir_name(dir);
    AppEntry {
        name,
        dir: dir.to_path_buf(),
        root: PathBuf::new(),
        framework_version: None,
        server_command: read_server_command(dir),
        known_port: None,
    }
}

/// Parse from Grimoire deployment build info (`grimoire-build-info.json`).
fn parse_from_build_info(dir: &Path) -> Option<AppEntry> {
    let text = std::fs::read_to_string(dir.join("grimoire-build-info.json")).ok()?;
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
    })
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
        // well within 1 second despite the 3600s interval.
        force_tx.send(()).unwrap();
        let timeout = tokio::time::sleep(Duration::from_secs(1));
        tokio::pin!(timeout);
        loop {
            tokio::select! {
                _ = &mut timeout => panic!("force scan did not arrive within 1 second"),
                _ = rx.changed() => {
                    let apps = rx.borrow_and_update().clone();
                    assert_eq!(apps.len(), 1);
                    assert_eq!(apps[0].name, "app-x");
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
}
