use crate::{
    detector,
    log_capture::{log_channel, LogReceiver, LogSender},
    models::{AppEntry, AppStatus, PortInfo},
};
use std::{collections::HashMap, path::PathBuf, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Child,
};
use tracing::{info, warn};

/// Grace period for SIGTERM before escalating to SIGKILL during shutdown_all.
const SHUTDOWN_GRACE_MS: u64 = 1500;

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

    /// Start an app. Returns (AppStatus, PortInfo, Option<LogReceiver>) after the 1s settle wait.
    /// The LogReceiver is `Some` when the process was launched and its output is piped;
    /// it is `None` when no launch method was found.
    pub async fn start(&mut self, entry: &AppEntry) -> (AppStatus, PortInfo, Option<LogReceiver>) {
        info!("starting {}", entry.name);

        let (tx, rx) = log_channel();

        let child = if let Some(cmd) = &entry.server_command {
            tokio::process::Command::new("sh")
                .args(["-c", cmd.as_str()])
                .current_dir(&entry.dir)
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .kill_on_drop(true)
                .spawn()
                .ok()
        } else {
            self.spawn_binary_piped(entry)
        };

        let log_rx = if let Some(mut child) = child {
            // Spawn a reader task for stdout.
            if let Some(stdout) = child.stdout.take() {
                spawn_line_reader(BufReader::new(stdout), tx.clone());
            }
            // Spawn a reader task for stderr.
            if let Some(stderr) = child.stderr.take() {
                spawn_line_reader(BufReader::new(stderr), tx);
            }
            self.children.insert(entry.dir.clone(), child);
            Some(rx)
        } else {
            warn!("no launch method found for {}", entry.name);
            None
        };

        tokio::time::sleep(Duration::from_secs(1)).await;
        let (status, port_info) = detector::detect(entry);
        (status, port_info, log_rx)
    }

    /// Restart an app: stop it (removing the child handle), then start it fresh.
    /// Returns (AppStatus, PortInfo, Option<LogReceiver>) after both operations settle.
    pub async fn restart(&mut self, entry: &AppEntry, last_known_pid: Option<u32>) -> (AppStatus, PortInfo, Option<LogReceiver>) {
        info!("restarting {}", entry.name);
        self.stop(entry, last_known_pid).await;
        self.start(entry).await
    }

    /// Stop an app. Returns updated (AppStatus, PortInfo) after the 500 ms settle wait.
    pub async fn stop(&mut self, entry: &AppEntry, last_known_pid: Option<u32>) -> (AppStatus, PortInfo) {
        info!("stopping {}", entry.name);
        if let Some(mut child) = self.children.remove(&entry.dir) {
            let _ = child.kill().await;
            let _ = child.wait().await;
        } else if let Some(pid) = last_known_pid {
            sigterm_then_sigkill(pid).await;
        }

        tokio::time::sleep(Duration::from_millis(500)).await;
        detector::detect(entry)
    }

    /// Gracefully shut down all tracked children on Warden exit.
    ///
    /// Sends SIGTERM to every tracked child, waits up to `SHUTDOWN_GRACE_MS`
    /// milliseconds polling for them to exit, then SIGKILLs any survivors.
    /// Clears `children` when done so the launcher is left in a clean state.
    pub async fn shutdown_all(&mut self) {
        if self.children.is_empty() {
            return;
        }
        info!("shutdown_all: terminating {} child process(es)", self.children.len());

        // Collect pids and send SIGTERM to every tracked child.
        let pids: Vec<u32> = self
            .children
            .values_mut()
            .filter_map(|c| c.id())
            .collect();

        for pid in &pids {
            let _ = std::process::Command::new("kill")
                .args(["-TERM", &pid.to_string()])
                .output();
        }

        // Poll until all pids have exited or the grace period expires.
        let poll_interval = Duration::from_millis(100);
        let mut elapsed = Duration::ZERO;
        let grace = Duration::from_millis(SHUTDOWN_GRACE_MS);

        while elapsed < grace {
            tokio::time::sleep(poll_interval).await;
            elapsed += poll_interval;

            let any_alive = pids.iter().any(|pid| {
                std::process::Command::new("kill")
                    .args(["-0", &pid.to_string()])
                    .output()
                    .map(|o| o.status.success())
                    .unwrap_or(false)
            });

            if !any_alive {
                break;
            }
        }

        // SIGKILL any survivors still alive after the grace period.
        for pid in &pids {
            let alive = std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false);
            if alive {
                warn!("shutdown_all: pid {} did not exit within grace period, sending SIGKILL", pid);
                let _ = std::process::Command::new("kill")
                    .args(["-KILL", &pid.to_string()])
                    .output();
            }
        }

        // Wait on each child handle to reap them and avoid zombies.
        for (_, mut child) in self.children.drain() {
            let _ = child.wait().await;
        }

        info!("shutdown_all: all children terminated");
    }

    fn spawn_binary_piped(&self, entry: &AppEntry) -> Option<Child> {
        let current = entry.dir.join("current");
        let binary = current.join(&entry.name);
        if binary.exists() {
            return tokio::process::Command::new(&binary)
                .current_dir(&entry.dir)
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .kill_on_drop(true)
                .spawn()
                .ok();
        }
        let binary = current.join("bin").join(&entry.name);
        if binary.exists() {
            return tokio::process::Command::new(&binary)
                .current_dir(&entry.dir)
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .kill_on_drop(true)
                .spawn()
                .ok();
        }
        None
    }
}

/// Spawns an async task that reads lines from `reader` and forwards them to `tx`.
/// On a full channel the line is dropped (log tail tolerates loss).
/// On a closed receiver the task exits cleanly.
fn spawn_line_reader<R>(reader: BufReader<R>, tx: LogSender)
where
    R: tokio::io::AsyncRead + Unpin + Send + 'static,
{
    use tokio::sync::mpsc::error::TrySendError;
    tokio::spawn(async move {
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            match tx.try_send(line) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => {} // Drop the line; never block the child reader.
                Err(TrySendError::Closed(_)) => break, // Receiver gone; exit.
            }
        }
    });
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
            root: PathBuf::new(),
            framework_version: None,
            server_command: Some(cmd.to_string()),
            known_port: None,
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

    #[tokio::test]
    async fn restart_kills_old_child_and_starts_new_process() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("restart-test-app");
        std::fs::create_dir_all(&app_dir).unwrap();

        let entry = app_entry(app_dir.clone(), "sleep 120");
        let mut launcher = Launcher::new();

        // Start the process initially.
        launcher.start(&entry).await;
        assert!(
            launcher.children.contains_key(&app_dir),
            "Child handle should be tracked after initial start"
        );

        // Capture the pid of the first child (if available).
        let first_pid = launcher
            .children
            .get_mut(&app_dir)
            .and_then(|c| c.id());

        // Restart: should stop old child and spawn a new one.
        launcher.restart(&entry, first_pid).await;

        // A new child handle must be tracked after restart.
        assert!(
            launcher.children.contains_key(&app_dir),
            "Child handle should be tracked after restart"
        );

        // If the original child had a pid, the new child should be different
        // (or at least a new handle was registered, which is sufficient).
        let new_pid = launcher
            .children
            .get_mut(&app_dir)
            .and_then(|c| c.id());

        // Both pids may be None if the OS reclaimed them; the key invariant is
        // that a child is tracked at all, validated above. When both are Some we
        // confirm they differ.
        if let (Some(old), Some(new)) = (first_pid, new_pid) {
            assert_ne!(old, new, "Restarted process should have a different pid");
        }

        // Clean up.
        launcher.stop(&entry, None).await;
        assert!(
            !launcher.children.contains_key(&app_dir),
            "Child handle should be removed after final stop"
        );
    }

    #[tokio::test]
    async fn shutdown_all_terminates_children_and_empties_map() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("shutdown-test-app");
        std::fs::create_dir_all(&app_dir).unwrap();

        let entry = app_entry(app_dir.clone(), "sleep 120");
        let mut launcher = Launcher::new();

        // Start a long-lived child.
        launcher.start(&entry).await;
        assert!(
            launcher.children.contains_key(&app_dir),
            "Child handle should be tracked after start"
        );

        // Capture the pid before shutdown_all drains the map.
        let pid = launcher
            .children
            .get_mut(&app_dir)
            .and_then(|c| c.id());

        // Shut down all children.
        launcher.shutdown_all().await;

        // The children map must be empty after shutdown_all.
        assert!(
            launcher.children.is_empty(),
            "children map must be empty after shutdown_all"
        );

        // The process must no longer be alive.
        if let Some(pid) = pid {
            let alive = std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false);
            assert!(!alive, "child process (pid {}) should be dead after shutdown_all", pid);
        }
    }
}
