use crate::{
    detector,
    models::{AppEntry, AppStatus, PortInfo},
};
use std::{collections::HashMap, path::PathBuf, time::Duration};
use tokio::process::Child;

/// Starts and stops apps, tracking spawned children for graceful cleanup.
pub struct Launcher {
    children: HashMap<PathBuf, Child>,
}

impl Launcher {
    pub fn new() -> Self {
        Launcher {
            children: HashMap::new(),
        }
    }

    /// Start an app. Returns updated (AppStatus, PortInfo) after the 1s settle wait.
    pub async fn start(&mut self, entry: &AppEntry) -> (AppStatus, PortInfo) {
        let child = if let Some(cmd) = &entry.server_command {
            tokio::process::Command::new("sh")
                .args(["-c", cmd.as_str()])
                .current_dir(&entry.dir)
                .spawn()
                .ok()
        } else {
            self.spawn_binary(entry)
        };

        if let Some(child) = child {
            self.children.insert(entry.dir.clone(), child);
        }

        tokio::time::sleep(Duration::from_secs(1)).await;
        detector::detect(entry)
    }

    /// Stop an app. Returns updated (AppStatus, PortInfo) after the 500 ms settle wait.
    pub async fn stop(&mut self, entry: &AppEntry, last_known_pid: Option<u32>) -> (AppStatus, PortInfo) {
        if let Some(mut child) = self.children.remove(&entry.dir) {
            let _ = child.kill().await;
            let _ = child.wait().await;
        } else if let Some(pid) = last_known_pid {
            sigterm_then_sigkill(pid).await;
        }

        tokio::time::sleep(Duration::from_millis(500)).await;
        detector::detect(entry)
    }

    fn spawn_binary(&self, entry: &AppEntry) -> Option<Child> {
        let current = entry.dir.join("current");
        let binary = current.join(&entry.name);
        if binary.exists() {
            return tokio::process::Command::new(&binary)
                .current_dir(&entry.dir)
                .spawn()
                .ok();
        }
        let binary = current.join("bin").join(&entry.name);
        if binary.exists() {
            return tokio::process::Command::new(&binary)
                .current_dir(&entry.dir)
                .spawn()
                .ok();
        }
        None
    }
}

async fn sigterm_then_sigkill(pid: u32) {
    let pid_str = pid.to_string();
    let _ = std::process::Command::new("kill")
        .args(["-TERM", &pid_str])
        .output();

    // Wait up to 5 s for the process to exit before SIGKILL.
    for _ in 0..10 {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let alive = std::process::Command::new("kill")
            .args(["-0", &pid_str])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if !alive {
            return;
        }
    }

    let _ = std::process::Command::new("kill")
        .args(["-KILL", &pid_str])
        .output();
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::AppEntry;
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn app_entry(dir: PathBuf, cmd: &str) -> AppEntry {
        AppEntry {
            name: dir.file_name().and_then(|n| n.to_str()).unwrap_or("app").to_string(),
            dir,
            framework_version: None,
            server_command: Some(cmd.to_string()),
        }
    }

    #[tokio::test]
    async fn starts_and_stops_a_process_via_server_command() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("test-app");
        std::fs::create_dir_all(&app_dir).unwrap();

        let entry = app_entry(app_dir.clone(), "sleep 120");
        let mut launcher = Launcher::new();

        // Start the process
        launcher.start(&entry).await;

        // Verify the child is tracked
        assert!(
            launcher.children.contains_key(&app_dir),
            "Child handle should be tracked after start"
        );

        // Stop the process
        launcher.stop(&entry, None).await;

        // Child handle should be removed
        assert!(
            !launcher.children.contains_key(&app_dir),
            "Child handle should be removed after stop"
        );
    }
}
