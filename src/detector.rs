use crate::models::{AppEntry, AppStatus, PortInfo};
use std::process::Command;
use tracing::debug;

/// Exit code threshold: pgrep returns 1 for clean no-match; >= 2 signals an error.
const PGREP_NO_MATCH_EXIT: i32 = 1;

/// Outcome of a single probe — distinguishes a confirmed absence from an inconclusive result.
#[derive(Debug, PartialEq)]
pub enum DetectOutcome {
    /// A process / listener was found; carries its PID.
    Found(u32),
    /// The probe ran cleanly and found nothing.
    NoMatch,
    /// The probe could not run reliably (permission error, spawn failure, etc.).
    Inconclusive,
}

/// Pure classifier for lsof output.
///
/// Rules (lsof -ti :<port>):
/// - exit 0 with a parseable PID in stdout → Found
/// - exit 1 with empty stdout AND empty stderr → NoMatch (clean no listener)
/// - any non-empty stderr, or a non-0/1 exit, or a spawn error → Inconclusive
pub fn classify_lsof(exit_code: Option<i32>, stdout: &str, stderr: &str) -> DetectOutcome {
    match exit_code {
        Some(0) => {
            // lsof succeeded; try to parse a PID from the first whitespace token.
            if let Some(pid) = stdout.split_whitespace().next().and_then(|t| t.parse::<u32>().ok()) {
                DetectOutcome::Found(pid)
            } else {
                // Exit 0 but no parseable PID — treat as inconclusive.
                DetectOutcome::Inconclusive
            }
        }
        Some(1) if stdout.is_empty() && stderr.is_empty() => DetectOutcome::NoMatch,
        None => DetectOutcome::Inconclusive,
        _ => {
            // Non-empty stderr, exit >= 2, or exit 1 with unexpected output → error path.
            DetectOutcome::Inconclusive
        }
    }
}

/// Pure classifier for pgrep output.
///
/// Rules (pgrep -f <pattern>):
/// - exit 0 with a parseable PID → Found
/// - exit 1 → clean no-match (per POSIX/BSD pgrep semantics)
/// - exit >= 2, or spawn failure (None) → Inconclusive
pub fn classify_pgrep(exit_code: Option<i32>, stdout: &str) -> DetectOutcome {
    match exit_code {
        Some(0) => {
            if let Some(pid) = stdout.split_whitespace().next().and_then(|t| t.parse::<u32>().ok()) {
                DetectOutcome::Found(pid)
            } else {
                DetectOutcome::Inconclusive
            }
        }
        Some(code) if code == PGREP_NO_MATCH_EXIT => DetectOutcome::NoMatch,
        _ => DetectOutcome::Inconclusive,
    }
}

/// Pure zombie predicate: returns true when the `ps -o stat=` string begins with 'Z'.
pub fn is_zombie(stat: &str) -> bool {
    stat.trim_start().starts_with('Z')
}

/// Returns true when the given PID is a zombie/defunct process.
///
/// Uses `ps -o stat= -p <pid>`.  A spawn failure is treated as non-zombie
/// (safe fallback: we may still filter it out via other means).
fn pid_is_zombie(pid: u32) -> bool {
    let out = Command::new("ps")
        .args(["-o", "stat=", "-p", &pid.to_string()])
        .output();
    match out {
        Ok(o) => {
            let stat = String::from_utf8_lossy(&o.stdout);
            is_zombie(&stat)
        }
        Err(_) => false,
    }
}

/// Probe a TCP port via `lsof -ti :<port>` and return a `DetectOutcome`.
fn lsof_outcome(port: u16) -> DetectOutcome {
    match Command::new("lsof").args(["-ti", &format!(":{port}")]).output() {
        Ok(out) => {
            let exit_code = out.status.code();
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            classify_lsof(exit_code, &stdout, &stderr)
        }
        Err(_) => DetectOutcome::Inconclusive,
    }
}

/// Search for a process by name/pattern via `pgrep -f <pattern>`, skipping zombies.
///
/// Returns `DetectOutcome::Found(pid)` only when the first matched PID is not a zombie.
fn pgrep_outcome(pattern: &str) -> DetectOutcome {
    match Command::new("pgrep").args(["-f", pattern]).output() {
        Ok(out) => {
            let exit_code = out.status.code();
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let outcome = classify_pgrep(exit_code, &stdout);
            // Filter zombie matches before reporting Found.
            if let DetectOutcome::Found(pid) = outcome {
                if pid_is_zombie(pid) {
                    return DetectOutcome::NoMatch;
                }
                return DetectOutcome::Found(pid);
            }
            outcome
        }
        Err(_) => DetectOutcome::Inconclusive,
    }
}

/// Detects the port and running status of a discovered app.
pub fn detect(entry: &AppEntry) -> (AppStatus, PortInfo) {
    let port_info = detect_port(entry);
    let status = detect_status(entry, port_info.port);
    debug!("{}: {:?} port={:?}", entry.name, status, port_info.port);
    (status, port_info)
}

/// Port detection chain: .port file → PORT file → known_port → parse server command → None.
pub fn detect_port(entry: &AppEntry) -> PortInfo {
    if let Some(port) = read_port_file(entry, ".port") {
        return PortInfo { port: Some(port) };
    }
    if let Some(port) = read_port_file(entry, "PORT") {
        return PortInfo { port: Some(port) };
    }
    if let Some(port) = entry.known_port {
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
///
/// When a probe returns `Inconclusive` (error, not a clean no-match), that step is
/// skipped rather than forcing `Stopped`.  A confirmed `NoMatch` on a known port
/// yields `Stopped`.  If every probe is inconclusive, returns `AppStatus::Unknown`.
pub fn detect_status(entry: &AppEntry, port: Option<u16>) -> AppStatus {
    if let Some(port) = port {
        return match lsof_outcome(port) {
            DetectOutcome::Found(pid) => AppStatus::Running { pid },
            DetectOutcome::NoMatch => AppStatus::Stopped,
            // Inconclusive: don't commit to Stopped — fall through the pgrep chain below.
            DetectOutcome::Inconclusive => {
                debug!("lsof inconclusive for port {port}; falling through to pgrep");
                pgrep_fallback(entry)
            }
        };
    }

    pgrep_fallback(entry)
}

/// pgrep fallback: try binary name then directory name; return Unknown if both inconclusive.
fn pgrep_fallback(entry: &AppEntry) -> AppStatus {
    let binary_name = binary_name_from_current(&entry.dir);
    if let Some(ref name) = binary_name {
        match pgrep_outcome(name) {
            DetectOutcome::Found(pid) => return AppStatus::Running { pid },
            DetectOutcome::NoMatch => {}
            DetectOutcome::Inconclusive => {}
        }
    }

    let dir_name = entry
        .dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    if !dir_name.is_empty() {
        match pgrep_outcome(dir_name) {
            DetectOutcome::Found(pid) => return AppStatus::Running { pid },
            DetectOutcome::NoMatch => {}
            DetectOutcome::Inconclusive => {}
        }
    }

    AppStatus::Unknown
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
            root: PathBuf::new(),
            framework_version: None,
            server_command: None,
            known_port: None,
        }
    }

    // -----------------------------------------------------------------------
    // classify_lsof — pure unit tests
    // -----------------------------------------------------------------------

    #[test]
    fn lsof_exit0_with_pid_is_found() {
        assert_eq!(
            classify_lsof(Some(0), "1234\n", ""),
            DetectOutcome::Found(1234)
        );
    }

    #[test]
    fn lsof_exit0_no_parseable_pid_is_inconclusive() {
        assert_eq!(
            classify_lsof(Some(0), "not-a-number\n", ""),
            DetectOutcome::Inconclusive
        );
    }

    #[test]
    fn lsof_exit1_empty_stdout_stderr_is_nomatch() {
        assert_eq!(classify_lsof(Some(1), "", ""), DetectOutcome::NoMatch);
    }

    #[test]
    fn lsof_exit1_nonempty_stderr_is_inconclusive() {
        assert_eq!(
            classify_lsof(Some(1), "", "lsof: WARNING: can't stat() fuse.gvfsd"),
            DetectOutcome::Inconclusive
        );
    }

    #[test]
    fn lsof_exit2_is_inconclusive() {
        assert_eq!(classify_lsof(Some(2), "", ""), DetectOutcome::Inconclusive);
    }

    #[test]
    fn lsof_spawn_failure_is_inconclusive() {
        assert_eq!(classify_lsof(None, "", ""), DetectOutcome::Inconclusive);
    }

    // -----------------------------------------------------------------------
    // classify_pgrep — pure unit tests
    // -----------------------------------------------------------------------

    #[test]
    fn pgrep_exit0_with_pid_is_found() {
        assert_eq!(
            classify_pgrep(Some(0), "5678\n"),
            DetectOutcome::Found(5678)
        );
    }

    #[test]
    fn pgrep_exit0_no_parseable_pid_is_inconclusive() {
        assert_eq!(
            classify_pgrep(Some(0), "not-a-pid\n"),
            DetectOutcome::Inconclusive
        );
    }

    #[test]
    fn pgrep_exit1_is_nomatch() {
        assert_eq!(classify_pgrep(Some(1), ""), DetectOutcome::NoMatch);
    }

    #[test]
    fn pgrep_exit2_is_inconclusive() {
        assert_eq!(classify_pgrep(Some(2), ""), DetectOutcome::Inconclusive);
    }

    #[test]
    fn pgrep_spawn_failure_is_inconclusive() {
        assert_eq!(classify_pgrep(None, ""), DetectOutcome::Inconclusive);
    }

    // -----------------------------------------------------------------------
    // is_zombie — pure unit tests (table-driven)
    // -----------------------------------------------------------------------

    #[test]
    fn zombie_stat_strings() {
        let zombie_cases = ["Z", "Z+", "Zs", "  Z", "Z\n"];
        for s in &zombie_cases {
            assert!(is_zombie(s), "expected zombie for stat={s:?}");
        }
        let alive_cases = ["S", "R", "S+", "Rs", "Ss+", "I", "D", "T", "s", "z"];
        for s in &alive_cases {
            assert!(!is_zombie(s), "expected non-zombie for stat={s:?}");
        }
    }

    // -----------------------------------------------------------------------
    // Integration-level tests (existing + new)
    // -----------------------------------------------------------------------

    #[test]
    fn returns_stopped_when_high_port_has_no_listener() {
        let tmp = TempDir::new().unwrap();
        let app_dir = tmp.path().join("myapp");
        std::fs::create_dir_all(&app_dir).unwrap();
        // Ask the OS for a free port, then release it before calling detect.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        std::fs::write(app_dir.join(".port"), port.to_string()).unwrap();

        let entry = AppEntry {
            name: "myapp".to_string(),
            dir: app_dir,
            root: PathBuf::new(),
            framework_version: None,
            server_command: None,
            known_port: None,
        };
        let (status, port_info) = detect(&entry);
        assert_eq!(port_info.port, Some(port));
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
            root: PathBuf::new(),
            framework_version: None,
            server_command: Some("node server.js --port 4321".to_string()),
            known_port: None,
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
            root: PathBuf::new(),
            framework_version: None,
            server_command: Some("PORT=8080 node server.js".to_string()),
            known_port: None,
        };
        let port_info = detect_port(&entry);
        assert_eq!(port_info.port, Some(8080));
    }

    /// When lsof returns a clean no-match (exit 1, empty stderr), a known port → Stopped.
    #[test]
    fn clean_nomatch_on_known_port_yields_stopped() {
        assert_eq!(classify_lsof(Some(1), "", ""), DetectOutcome::NoMatch);
    }

    /// An lsof error (non-empty stderr) must NOT be treated as a clean no-match.
    #[test]
    fn lsof_error_is_not_nomatch() {
        let outcome = classify_lsof(Some(1), "", "permission denied");
        assert_ne!(outcome, DetectOutcome::NoMatch);
        assert_eq!(outcome, DetectOutcome::Inconclusive);
    }

    /// pgrep exit 1 is a clean no-match, not an error.
    #[test]
    fn pgrep_exit1_is_not_error() {
        assert_eq!(classify_pgrep(Some(1), ""), DetectOutcome::NoMatch);
    }

    /// pgrep exit >= 2 is treated as an error (Inconclusive), not a no-match.
    #[test]
    fn pgrep_exit_ge2_is_error_not_nomatch() {
        assert_ne!(classify_pgrep(Some(2), ""), DetectOutcome::NoMatch);
        assert_eq!(classify_pgrep(Some(2), ""), DetectOutcome::Inconclusive);
    }
}
