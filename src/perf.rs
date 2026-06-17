// Perf: helpers for writing performance telemetry to ~/.config/warden/perf.log.

use std::io::Write;

/// Append one scan-cycle record to `~/.config/warden/perf.log`.
///
/// Each line has the format:
/// `<ISO-8601> scan_cycle <ms>ms drops=<n>[ slowest=[<app>]]`
pub fn write_perf_log(cycle_ms: u128, drop_count: u32, slowest_app: &str) {
    let timestamp = chrono::Local::now().to_rfc3339();
    let line = if slowest_app.is_empty() {
        format!(
            "{} scan_cycle {}ms drops={}\n",
            timestamp, cycle_ms, drop_count
        )
    } else {
        format!(
            "{} scan_cycle {}ms drops={} slowest=[{}]\n",
            timestamp, cycle_ms, drop_count, slowest_app
        )
    };
    if let Some(mut path) = dirs::config_dir() {
        path.push("warden");
        path.push("perf.log");
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
        {
            let _ = f.write_all(line.as_bytes());
        }
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn test_write_perf_log_no_slowest() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("perf.log");
        // Write directly using a temp path to avoid touching ~.
        let timestamp = chrono::Local::now().to_rfc3339();
        let line = format!("{} scan_cycle 123ms drops=0\n", timestamp);
        {
            let mut f = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .unwrap();
            f.write_all(line.as_bytes()).unwrap();
        }
        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.contains("scan_cycle 123ms drops=0"));
        assert!(!contents.contains("slowest="));
    }

    #[test]
    fn test_write_perf_log_with_slowest() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("perf.log");
        let timestamp = chrono::Local::now().to_rfc3339();
        let line = format!(
            "{} scan_cycle 456ms drops=2 slowest=[my-app]\n",
            timestamp
        );
        {
            let mut f = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .unwrap();
            f.write_all(line.as_bytes()).unwrap();
        }
        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.contains("scan_cycle 456ms drops=2 slowest=[my-app]"));
    }

    #[test]
    fn test_write_perf_log_appends() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("perf.log");
        for i in 0u32..3 {
            let timestamp = chrono::Local::now().to_rfc3339();
            let line = format!("{} scan_cycle {}ms drops=0\n", timestamp, i * 10);
            let mut f = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .unwrap();
            f.write_all(line.as_bytes()).unwrap();
        }
        let contents = fs::read_to_string(&path).unwrap();
        assert_eq!(contents.lines().count(), 3);
    }

    #[test]
    fn test_log_line_format_no_slowest() {
        let cycle_ms: u128 = 77;
        let drop_count: u32 = 0;
        let slowest_app = "";
        let line = if slowest_app.is_empty() {
            format!("TS scan_cycle {}ms drops={}\n", cycle_ms, drop_count)
        } else {
            format!(
                "TS scan_cycle {}ms drops={} slowest=[{}]\n",
                cycle_ms, drop_count, slowest_app
            )
        };
        assert!(line.contains("scan_cycle 77ms drops=0"));
        assert!(!line.contains("slowest="));
    }

    #[test]
    fn test_log_line_format_with_slowest() {
        let cycle_ms: u128 = 200;
        let drop_count: u32 = 3;
        let slowest_app = "big-app";
        let line = format!(
            "TS scan_cycle {}ms drops={} slowest=[{}]\n",
            cycle_ms, drop_count, slowest_app
        );
        assert!(line.contains("scan_cycle 200ms drops=3 slowest=[big-app]"));
    }
}
