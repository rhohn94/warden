use crate::models::AppEntry;
use serde_json::Value;
use std::path::PathBuf;
use std::time::Duration;
use tokio::sync::watch;

/// Starts a background tokio task that scans `root` every `interval` seconds
/// and sends the discovered apps through the returned `watch::Receiver`.
pub fn start(root: PathBuf, interval: Duration) -> watch::Receiver<Vec<AppEntry>> {
    let (tx, rx) = watch::channel(Vec::new());
    tokio::spawn(async move {
        loop {
            let apps = scan_once(&root);
            let _ = tx.send(apps);
            tokio::time::sleep(interval).await;
        }
    });
    rx
}

/// Scans `root` synchronously and returns discovered `AppEntry` records.
pub fn scan_once(root: &PathBuf) -> Vec<AppEntry> {
    let read = match std::fs::read_dir(root) {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };

    let mut apps = Vec::new();
    for entry in read.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let config_path = dir.join("grimoire-config.json");
        if !config_path.exists() {
            continue;
        }
        let app = parse_app_dir(&dir);
        apps.push(app);
    }
    apps
}

fn parse_app_dir(dir: &PathBuf) -> AppEntry {
    let dir_name = dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_string();

    let (name, framework_version) = read_grimoire_config(dir, &dir_name);
    let server_command = read_server_command(dir);

    AppEntry {
        name,
        dir: dir.clone(),
        framework_version,
        server_command,
    }
}

fn read_grimoire_config(dir: &PathBuf, dir_name: &str) -> (String, Option<String>) {
    let path = dir.join("grimoire-config.json");
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(_) => return (dir_name.to_string(), None),
    };
    let val: Value = match serde_json::from_str(&text) {
        Ok(v) => v,
        Err(_) => return (dir_name.to_string(), None),
    };

    let name = val
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(dir_name)
        .to_string();

    let framework_version = val
        .get("framework-version")
        .and_then(Value::as_str)
        .map(str::to_string);

    (name, framework_version)
}

fn read_server_command(dir: &PathBuf) -> Option<String> {
    let path = dir.join("recipes.json");
    let text = std::fs::read_to_string(&path).ok()?;
    let val: Value = serde_json::from_str(&text).ok()?;
    val.pointer("/targets/server/command")
        .and_then(Value::as_str)
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn make_app(root: &std::path::Path, name: &str, framework_version: Option<&str>, server_cmd: Option<&str>) {
        let app_dir = root.join(name);
        fs::create_dir_all(&app_dir).unwrap();

        let mut config = serde_json::json!({"name": name});
        if let Some(v) = framework_version {
            config["framework-version"] = serde_json::Value::String(v.to_string());
        }
        fs::write(app_dir.join("grimoire-config.json"), config.to_string()).unwrap();

        if let Some(cmd) = server_cmd {
            let recipes = serde_json::json!({"targets": {"server": {"command": cmd}}});
            fs::write(app_dir.join("recipes.json"), recipes.to_string()).unwrap();
        }
    }

    #[test]
    fn discovers_apps_with_grimoire_config() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();

        make_app(&root, "my-app", Some("3.36"), Some("node server.js"));
        fs::create_dir_all(root.join("not-an-app")).unwrap(); // no grimoire-config.json

        let apps = scan_once(&root);
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0].name, "my-app");
        assert_eq!(apps[0].framework_version.as_deref(), Some("3.36"));
        assert_eq!(apps[0].server_command.as_deref(), Some("node server.js"));
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
}
