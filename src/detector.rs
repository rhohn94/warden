use crate::models::{AppEntry, AppStatus, PortInfo};
use std::process::Command;

/// Detects the port and running status of a discovered app.
pub fn detect(entry: &AppEntry) -> (AppStatus, PortInfo) {
    let port_info = detect_port(entry);
    let status = detect_status(entry, port_info.port);
    (status, port_info)
}

/// Port detection chain: .port file → PORT file → parse server command → None.
pub fn detect_port(entry: &AppEntry) -> PortInfo {
    if let Some(port) = read_port_file(entry, ".port") {
        return PortInfo { port: Some(port) };
    }
    if let Some(port) = read_port_file(entry, "PORT") {
        return PortInfo { port: Some(port) };
    }
    if let Some(cmd) = &entry.server_command {
        if let Some(port) = parse_port_from_command(cmd) {
            return PortInfo { port: Some(port) };
        }
    }
    PortInfo { port: None }
}

fn read_port_file(entry: &AppEntry, filename: &str) -> Option<u16> {
    let path = entry.dir.join(filename);
    std::fs::read_to_string(&path).ok()?.trim().parse().ok()
}

fn parse_port_from_command(cmd: &str) -> Option<u16> {
    // Match --port <N>
    if let Some(idx) = cmd.find("--port") {
        let rest = cmd[idx + 6..].trim_start();
        let token = rest.split_whitespace().next()?;
        if let Ok(p) = token.parse::<u16>() {
            return Some(p);
        }
    }
    // Match PORT=<N> (env var prefix)
    for part in cmd.split_whitespace() {
        if let Some(val) = part.strip_prefix("PORT=") {
            if let Ok(p) = val.parse::<u16>() {
                return Some(p);
            }
        }
    }
    None
}

/// Process detection chain: lsof (port) → pgrep binary → pgrep dir name → Stopped/Unknown.
pub fn detect_status(entry: &AppEntry, port: Option<u16>) -> AppStatus {
    if let Some(port) = port {
        return match lsof_pid(port) {
            Some(pid) => AppStatus::Running { pid },
            None => AppStatus::Stopped,
        };
    }

    let binary_name = binary_name_from_current(&entry.dir);
    if let Some(ref name) = binary_name {
        if let Some(pid) = pgrep(name) {
            return AppStatus::Running { pid };
        }
    }

    let dir_name = entry
        .dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    if !dir_name.is_empty() {
        if let Some(pid) = pgrep(dir_name) {
            return AppStatus::Running { pid };
        }
    }

    AppStatus::Unknown
}

fn lsof_pid(port: u16) -> Option<u32> {
    let out = Command::new("lsof")
        .args(["-ti", &format!(":{port}")])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    std::str::from_utf8(&out.stdout)
        .ok()?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

fn pgrep(pattern: &str) -> Option<u32> {
    let out = Command::new("pgrep")
        .args(["-f", pattern])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    std::str::from_utf8(&out.stdout)
        .ok()?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

fn binary_name_from_current(dir: &std::path::Path) -> Option<String> {
    let current = dir.join("current");
    if !current.exists() {
        return None;
    }
    let dir_name = dir.file_name()?.to_str()?.to_string();
    if current.join(&dir_name).exists() || current.join("bin").join(&dir_name).exists() {
        return Some(dir_name);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn fake_entry(dir: PathBuf) -> AppEntry {
        AppEntry {
            name: dir.file_name().and_then(|n| n.to_str()).unwrap_or("test").to_string(),
            dir,
            framework_version: None,
            server_command: None,
        }
    }

    #[test]
    fn returns_stopped_when_high_port_has_no_listener() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("myapp");
        std::fs::create_dir_all(&app_dir).unwrap();
        // Write a .port file with a port very unlikely to be in use
        std::fs::write(app_dir.join(".port"), "59997").unwrap();

        let entry = AppEntry {
            name: "myapp".to_string(),
            dir: app_dir,
            framework_version: None,
            server_command: None,
        };
        let (status, port_info) = detect(&entry);
        assert_eq!(port_info.port, Some(59997));
        // Nothing is listening on 59997 in CI, so expect Stopped
        assert!(
            matches!(status, AppStatus::Stopped),
            "Expected Stopped, got {status:?}"
        );
    }

    #[test]
    fn detects_running_process_via_pgrep_dir_name() {
        // Spawn a shell script whose path contains a unique marker.
        // pgrep -f <marker> matches the sh process because the script path is in its argv.
        let tmp = TempDir::new().unwrap();
        let marker = format!("warden_test_{}", std::process::id());

        // Script file path contains the marker — appears in the sh process's argv.
        let script = tmp.path().join(format!("{marker}.sh"));
        std::fs::write(&script, "#!/bin/sh\nsleep 100\n").unwrap();

        // App dir name IS the marker, so pgrep(dir_name) searches for the marker.
        let app_dir = tmp.path().join(&marker);
        std::fs::create_dir_all(&app_dir).unwrap();

        let mut child = std::process::Command::new("sh")
            .arg(&script)
            .spawn()
            .expect("failed to spawn sh");

        // Give the process a moment to appear in pgrep output.
        std::thread::sleep(std::time::Duration::from_millis(150));

        let entry = fake_entry(app_dir);
        let status = detect_status(&entry, None);

        child.kill().ok();
        child.wait().ok();

        assert!(
            matches!(status, AppStatus::Running { .. }),
            "Expected Running, got {status:?}"
        );
    }

    #[test]
    fn parses_port_from_server_command_flag() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("port-test");
        std::fs::create_dir_all(&app_dir).unwrap();

        let entry = AppEntry {
            name: "port-test".to_string(),
            dir: app_dir,
            framework_version: None,
            server_command: Some("node server.js --port 4321".to_string()),
        };
        let port_info = detect_port(&entry);
        assert_eq!(port_info.port, Some(4321));
    }

    #[test]
    fn parses_port_from_env_var_prefix() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("port-env-test");
        std::fs::create_dir_all(&app_dir).unwrap();

        let entry = AppEntry {
            name: "port-env-test".to_string(),
            dir: app_dir,
            framework_version: None,
            server_command: Some("PORT=8080 node server.js".to_string()),
        };
        let port_info = detect_port(&entry);
        assert_eq!(port_info.port, Some(8080));
    }
}
