//! Changelog data layer: parses `docs/version-history.md` and exposes typed entries.
//!
//! `parse_changelog` splits the file on `## v` headings and collects bullet lines.
//! `changelog_entries` returns entries in source order (latest first).

/// One version section from the changelog.
#[allow(dead_code)]
pub struct ChangelogEntry {
    pub version: String,
    pub bullets: Vec<String>,
}

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const CHANGELOG_MD: &str = include_str!("../docs/version-history.md");

/// Parse a changelog markdown string into a list of `ChangelogEntry` values.
///
/// Sections start on lines beginning with `## v`; bullet lines begin with `- `.
/// Entries are returned in source order (latest first, matching file layout).
pub fn parse_changelog(md: &str) -> Vec<ChangelogEntry> {
    let mut entries: Vec<ChangelogEntry> = Vec::new();
    let mut current_version: Option<String> = None;
    let mut current_bullets: Vec<String> = Vec::new();

    for line in md.lines() {
        if line.starts_with("## v") {
            // Flush previous entry.
            if let Some(version) = current_version.take() {
                entries.push(ChangelogEntry {
                    version,
                    bullets: std::mem::take(&mut current_bullets),
                });
            }
            // Extract version string after "## ".
            current_version = Some(line[3..].trim().to_string());
        } else if let Some(bullet) = line.strip_prefix("- ") {
            if current_version.is_some() {
                current_bullets.push(bullet.to_string());
            }
        }
    }

    // Flush the final entry.
    if let Some(version) = current_version.take() {
        entries.push(ChangelogEntry {
            version,
            bullets: current_bullets,
        });
    }

    entries
}

/// Return changelog entries parsed from the bundled `docs/version-history.md`.
pub fn changelog_entries() -> Vec<ChangelogEntry> {
    parse_changelog(CHANGELOG_MD)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_produces_entries_for_all_versions() {
        let entries = parse_changelog(CHANGELOG_MD);
        assert!(entries.len() >= 11, "expected at least 11 versions");
        // Assert the newest entry matches the current crate version rather than a
        // hardcoded string, so the test never goes stale on a release and also
        // enforces that every version bump ships a matching changelog entry.
        assert_eq!(
            entries[0].version,
            format!("v{}", VERSION),
            "newest changelog entry must match the crate version"
        );
        assert!(!entries[0].bullets.is_empty(), "first entry should have bullets");
    }
}
