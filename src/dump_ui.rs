//! Headless JSON snapshot of the current app state — used by `--dump-ui`.
//!
//! Scans `apps_dir`, runs detection for each entry, and serialises the result
//! to stdout as stable (sorted, timestamp-free) JSON. No window is created.

use crate::{detector, models::AppStatus, scanner};
use serde::Serialize;
use std::path::PathBuf;

/// Wire-format for a single app entry in the `--dump-ui` snapshot.
#[derive(Serialize)]
struct AppSnapshot {
    dir: PathBuf,
    root: PathBuf,
    name: String,
    framework_version: Option<String>,
    known_port: Option<u16>,
    server_command: Option<String>,
    status: String,
    pid: Option<u32>,
    detected_port: Option<u16>,
}

/// Top-level `--dump-ui` JSON object.
#[derive(Serialize)]
struct UiDump {
    apps_dirs: Vec<PathBuf>,
    entries: Vec<AppSnapshot>,
}

/// Scan all `apps_dirs`, detect status/port for every entry, and print stable JSON to stdout.
pub fn scan_and_dump(apps_dirs: Vec<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let mut entries = scanner::scan_all(&apps_dirs);

    // Sort by dir for a stable, diff-friendly ordering.
    entries.sort_by(|a, b| a.dir.cmp(&b.dir));

    let mut snapshots = Vec::with_capacity(entries.len());
    for entry in &entries {
        let (status, port_info) = detector::detect(entry);

        let (status_str, pid) = match &status {
            AppStatus::Running { pid } => ("Running".to_string(), Some(*pid)),
            AppStatus::Stopped => ("Stopped".to_string(), None),
            AppStatus::Unknown => ("Unknown".to_string(), None),
        };

        snapshots.push(AppSnapshot {
            dir: entry.dir.clone(),
            root: entry.root.clone(),
            name: entry.name.clone(),
            framework_version: entry.framework_version.clone(),
            known_port: entry.known_port,
            server_command: entry.server_command.clone(),
            status: status_str,
            pid,
            detected_port: port_info.port,
        });
    }

    let dump = UiDump {
        apps_dirs,
        entries: snapshots,
    };

    let json = serde_json::to_string_pretty(&dump)?;
    println!("{}", json);
    Ok(())
}
