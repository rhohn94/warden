//! launchd LaunchAgent awareness (#53).
//!
//! Several deployed apps ship a LaunchAgent plist at the app-dir root
//! (`com.gooncave.server.plist`, `com.discordbot.dashboard.plist`). When such
//! an agent is loaded with `KeepAlive`, warden's SIGTERM stop fights launchd:
//! launchd restarts the process and the app flips back to Running. This module
//! detects those plists and, when the agent is actually loaded, manages the
//! app through `launchctl` instead of raw signals.

use std::path::{Path, PathBuf};
use std::process::Command;
use tracing::{info, warn};

/// A LaunchAgent discovered at an app-dir root.
#[derive(Debug, Clone, PartialEq)]
pub struct LaunchdAgent {
    pub label: String,
    pub plist_path: PathBuf,
}

/// Scan the app dir root (non-recursive) for a `*.plist` LaunchAgent and parse
/// its `Label`. Template files (`*.plist.template`) are skipped — they are
/// deploy inputs, not installed agents.
pub fn find_agent(dir: &Path) -> Option<LaunchdAgent> {
    let read = std::fs::read_dir(dir).ok()?;
    for entry in read.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !name.ends_with(".plist") {
            continue; // also excludes *.plist.template
        }
        let Ok(xml) = std::fs::read_to_string(&path) else {
            continue;
        };
        if let Some(label) = parse_label(&xml) {
            return Some(LaunchdAgent {
                label,
                plist_path: path,
            });
        }
        warn!("plist without a parseable Label: {}", path.display());
    }
    None
}

/// Extract the `Label` string from launchd plist XML.
///
/// Pure text extraction (no plist dependency): finds `<key>Label</key>` and
/// returns the contents of the next `<string>…</string>` element.
pub fn parse_label(xml: &str) -> Option<String> {
    let key_pos = xml.find("<key>Label</key>")?;
    let rest = &xml[key_pos..];
    let open = rest.find("<string>")? + "<string>".len();
    let close = rest[open..].find("</string>")? + open;
    let label = rest[open..close].trim();
    if label.is_empty() {
        None
    } else {
        Some(label.to_string())
    }
}

/// The launchd domain target for the current GUI session, e.g. `gui/501`.
fn gui_domain() -> Option<String> {
    let out = Command::new("id").arg("-u").output().ok()?;
    if !out.status.success() {
        return None;
    }
    let uid = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if uid.is_empty() {
        return None;
    }
    Some(format!("gui/{uid}"))
}

/// True when the agent with `label` is currently loaded in the GUI domain.
/// `launchctl print gui/<uid>/<label>` exits 0 only for loaded services.
pub fn is_loaded(label: &str) -> bool {
    let Some(domain) = gui_domain() else {
        return false;
    };
    Command::new("launchctl")
        .args(["print", &format!("{domain}/{label}")])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Start (or restart) a loaded agent via `launchctl kickstart`.
/// Returns true when launchctl reported success.
pub fn kickstart(label: &str) -> bool {
    let Some(domain) = gui_domain() else {
        return false;
    };
    let target = format!("{domain}/{label}");
    match Command::new("launchctl")
        .args(["kickstart", &target])
        .output()
    {
        Ok(o) if o.status.success() => {
            info!("launchctl kickstart {target} succeeded");
            true
        }
        Ok(o) => {
            warn!(
                "launchctl kickstart {target} failed: {}",
                String::from_utf8_lossy(&o.stderr).trim()
            );
            false
        }
        Err(e) => {
            warn!("launchctl kickstart {target} spawn error: {e}");
            false
        }
    }
}

/// Stop a loaded agent via `launchctl bootout` (unload). This is the only
/// stop that sticks for `KeepAlive` agents — killing the pid makes launchd
/// respawn it. The plist file stays on disk, so the agent can be re-loaded
/// later (login, `launchctl bootstrap`, or the app's own justfile).
pub fn bootout(label: &str) -> bool {
    let Some(domain) = gui_domain() else {
        return false;
    };
    let target = format!("{domain}/{label}");
    match Command::new("launchctl").args(["bootout", &target]).output() {
        Ok(o) if o.status.success() => {
            info!("launchctl bootout {target} succeeded");
            true
        }
        Ok(o) => {
            warn!(
                "launchctl bootout {target} failed: {}",
                String::from_utf8_lossy(&o.stderr).trim()
            );
            false
        }
        Err(e) => {
            warn!("launchctl bootout {target} spawn error: {e}");
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    const GOON_CAVE_PLIST: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gooncave.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/x/deployed-apps/goon-cave/goon-cave</string>
    </array>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>"#;

    #[test]
    fn parses_label_from_real_plist_shape() {
        assert_eq!(
            parse_label(GOON_CAVE_PLIST).as_deref(),
            Some("com.gooncave.server")
        );
    }

    #[test]
    fn label_key_matched_exactly_not_program_arguments() {
        // The Label key appears after other <string> elements; extraction must
        // anchor on <key>Label</key>, not the first <string> in the file.
        let xml = r#"<plist><dict>
            <key>ProgramArguments</key>
            <array><string>/bin/echo</string></array>
            <key>Label</key>
            <string>com.example.later</string>
        </dict></plist>"#;
        assert_eq!(parse_label(xml).as_deref(), Some("com.example.later"));
    }

    #[test]
    fn missing_label_returns_none() {
        assert_eq!(parse_label("<plist><dict></dict></plist>"), None);
        assert_eq!(parse_label("<key>Label</key>"), None);
        assert_eq!(parse_label("<key>Label</key><string></string>"), None);
    }

    #[test]
    fn finds_agent_in_app_dir() {
        let tmp = TempDir::new().unwrap();
        std::fs::write(tmp.path().join("com.gooncave.server.plist"), GOON_CAVE_PLIST).unwrap();

        let agent = find_agent(tmp.path()).expect("agent should be found");
        assert_eq!(agent.label, "com.gooncave.server");
        assert_eq!(
            agent.plist_path,
            tmp.path().join("com.gooncave.server.plist")
        );
    }

    #[test]
    fn skips_plist_templates() {
        let tmp = TempDir::new().unwrap();
        std::fs::write(
            tmp.path().join("com.familiar.server.plist.template"),
            GOON_CAVE_PLIST,
        )
        .unwrap();
        assert_eq!(find_agent(tmp.path()), None);
    }

    #[test]
    fn no_plist_returns_none() {
        let tmp = TempDir::new().unwrap();
        std::fs::write(tmp.path().join("start.sh"), "#!/bin/sh\n").unwrap();
        assert_eq!(find_agent(tmp.path()), None);
    }
}
