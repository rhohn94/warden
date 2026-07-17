// build_info.rs — reads the `grimoire-build-info.json` stamp
// (docs/web-app-deployment-protocol.md §8) that `just package` /
// scripts/package.sh writes into the bundle root alongside the binary. Wires
// `--version` to REAL build provenance instead of only the crate's
// compile-time Cargo.toml version.
//
// Resolution order (first match wins):
//   1. next to the running executable (installed/packaged layout —
//      scripts/package.sh stages the binary and grimoire-build-info.json into
//      the SAME dist/<bundle>/ dir).
//   2. the current working directory (dev convenience — nothing packages a
//      dev `cargo run` binary, but a repo root that already ran `just
//      package` once may still have a stale copy at its root).
// Absence is NOT an error — a `cargo run`/`cargo build` dev binary has never
// been packaged, so `--version` reports the crate version alone plus an
// honest note (never fabricates provenance).
//
// std-only by design (no serde/no dependency), scoped to exactly the
// top-level string fields this interface needs — mirrors the web quick-start
// template's version_report.rs JSON-reader philosophy. This is the SAME
// module shipped in the cli/gui quick-start templates (self-contained
// per-template copy — no shared crate across profiles, matching the
// seam-file convention already used by the web quick-start template).

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct BuildInfo {
    pub framework_version: Option<String>,
    pub build_timestamp: Option<String>,
    pub source_ref: Option<String>,
}

/// Find a `grimoire-build-info.json` next to the running executable, else in
/// the current working directory. `None` when neither location has one.
pub fn locate() -> Option<PathBuf> {
    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            let candidate = dir.join("grimoire-build-info.json");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    let cwd_candidate = Path::new("grimoire-build-info.json");
    if cwd_candidate.is_file() {
        return Some(cwd_candidate.to_path_buf());
    }
    None
}

/// Load + parse the build-info stamp via [`locate`]. `None` when absent.
pub fn load() -> Option<BuildInfo> {
    let path = locate()?;
    let text = fs::read_to_string(path).ok()?;
    parse(&text)
}

/// Parse a `grimoire-build-info.json` document's text into a [`BuildInfo`].
/// Pure (no filesystem access) so it is directly unit-testable.
pub fn parse(text: &str) -> Option<BuildInfo> {
    Some(BuildInfo {
        framework_version: extract_string_field(text, "framework-version"),
        build_timestamp: extract_string_field(text, "build-timestamp"),
        source_ref: extract_string_field(text, "source-ref"),
    })
}

/// Extract one top-level `"key": "value"` string field. Returns `None` for an
/// absent key, a `null` value, or a non-string value — never panics on
/// malformed input.
fn extract_string_field(text: &str, key: &str) -> Option<String> {
    let needle = format!("\"{key}\"");
    let key_pos = text.find(&needle)?;
    let after_key = &text[key_pos + needle.len()..];
    let colon_pos = after_key.find(':')?;
    let after_colon = after_key[colon_pos + 1..].trim_start();
    if after_colon.starts_with("null") || !after_colon.starts_with('"') {
        return None;
    }
    let rest = &after_colon[1..];
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => {
                if let Some(esc) = chars.next() {
                    match esc {
                        'n' => out.push('\n'),
                        't' => out.push('\t'),
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        other => out.push(other),
                    }
                }
            }
            other => out.push(other),
        }
    }
    None // unterminated string — malformed input, degrade to None
}

/// Render the `--version`/`-V` line: crate version, plus build provenance
/// when a packaged `grimoire-build-info.json` is present, else an honest
/// dev-build note (never fabricates provenance it doesn't have).
pub fn version_line(crate_version: &str) -> String {
    match load() {
        Some(info) => {
            let framework = info.framework_version.unwrap_or_else(|| "unknown".to_string());
            let ts = info.build_timestamp.unwrap_or_else(|| "unknown".to_string());
            let sref = info.source_ref.unwrap_or_else(|| "unknown".to_string());
            format!("v{crate_version} (framework {framework}, built {ts}, source {sref})")
        }
        None => format!(
            "v{crate_version} (dev build — no grimoire-build-info.json found; run `just package` for full provenance)"
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
  "framework-version": "v3.94",
  "grimoire-config": {"work-paradigm": {"value": "Noir"}},
  "build-timestamp": "2026-07-13T12:00:00Z",
  "source-ref": "v3.94@abc123"
}
"#;

    #[test]
    fn parses_all_fields() {
        let info = parse(SAMPLE).expect("parses");
        assert_eq!(info.framework_version.as_deref(), Some("v3.94"));
        assert_eq!(info.build_timestamp.as_deref(), Some("2026-07-13T12:00:00Z"));
        assert_eq!(info.source_ref.as_deref(), Some("v3.94@abc123"));
    }

    #[test]
    fn null_field_degrades_to_none() {
        let text = r#"{"framework-version": null, "build-timestamp": "t", "source-ref": "r"}"#;
        let info = parse(text).expect("parses");
        assert!(info.framework_version.is_none());
        assert_eq!(info.build_timestamp.as_deref(), Some("t"));
    }

    #[test]
    fn missing_field_is_none_not_panic() {
        let info = parse("{}").expect("parses even an empty object");
        assert!(info.framework_version.is_none());
        assert!(info.build_timestamp.is_none());
        assert!(info.source_ref.is_none());
    }

    #[test]
    fn version_line_with_full_info() {
        let info = parse(SAMPLE).unwrap();
        let line = render_for_test("1.0.0", Some(info));
        assert!(line.contains("v1.0.0"));
        assert!(line.contains("v3.94"));
        assert!(line.contains("abc123"));
    }

    #[test]
    fn version_line_without_build_info_is_honest_dev_note() {
        let line = render_for_test("1.0.0", None);
        assert!(line.contains("v1.0.0"));
        assert!(line.contains("dev build"));
        assert!(line.contains("no grimoire-build-info.json found"));
    }

    /// Test-only helper mirroring `version_line`'s formatting without touching
    /// the filesystem (keeps `load()`'s file-location behavior out of unit
    /// tests, matching the crate-root TempRoot convention used elsewhere).
    fn render_for_test(crate_version: &str, info: Option<BuildInfo>) -> String {
        match info {
            Some(info) => {
                let framework = info.framework_version.unwrap_or_else(|| "unknown".to_string());
                let ts = info.build_timestamp.unwrap_or_else(|| "unknown".to_string());
                let sref = info.source_ref.unwrap_or_else(|| "unknown".to_string());
                format!("v{crate_version} (framework {framework}, built {ts}, source {sref})")
            }
            None => format!(
                "v{crate_version} (dev build — no grimoire-build-info.json found; run `just package` for full provenance)"
            ),
        }
    }
}
