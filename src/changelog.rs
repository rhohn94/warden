//! Changelog data layer: parses `docs/version-history.md` and exposes typed entries.
//!
//! `parse_changelog` splits the file on `## v` headings and collects bullet lines.
//! `changelog_entries` returns entries in source order (latest first).
//! `parse_inline` converts a bullet string into styled spans for rich rendering.

/// One version section from the changelog.
#[allow(dead_code)]
pub struct ChangelogEntry {
    pub version: String,
    pub bullets: Vec<String>,
}

/// A single styled run of inline text within a bullet.
///
/// `bold` and `code` are mutually exclusive in practice but both flags are
/// independent so overlapping markers degrade gracefully rather than panic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InlineSpan {
    pub text: String,
    pub bold: bool,
    pub code: bool,
}

impl InlineSpan {
    fn plain(text: impl Into<String>) -> Self {
        Self { text: text.into(), bold: false, code: false }
    }
    fn bold(text: impl Into<String>) -> Self {
        Self { text: text.into(), bold: true, code: false }
    }
    fn code(text: impl Into<String>) -> Self {
        Self { text: text.into(), bold: false, code: true }
    }
}

/// Parse a bullet string into `InlineSpan` values for rich text rendering.
///
/// Supports `**bold**` and `` `code` `` inline markers.  Unmatched or partial
/// markers are emitted as literal text — this function never panics.
/// An empty input returns a single empty plain span.
pub fn parse_inline(s: &str) -> Vec<InlineSpan> {
    let mut spans: Vec<InlineSpan> = Vec::new();
    let chars: Vec<char> = s.chars().collect();
    let len = chars.len();
    let mut i = 0;
    let mut current = String::new();

    while i < len {
        // Detect `**bold**`
        if i + 1 < len && chars[i] == '*' && chars[i + 1] == '*' {
            let start = i + 2;
            // Search for closing `**`
            let mut j = start;
            let mut found_bold = false;
            while j < len {
                if j + 1 < len && chars[j] == '*' && chars[j + 1] == '*' {
                    if !current.is_empty() {
                        spans.push(InlineSpan::plain(std::mem::take(&mut current)));
                    }
                    let bold_text: String = chars[start..j].iter().collect();
                    if !bold_text.is_empty() {
                        spans.push(InlineSpan::bold(bold_text));
                    }
                    i = j + 2;
                    found_bold = true;
                    break;
                }
                j += 1;
            }
            if !found_bold {
                // No closing `**` — emit the opening asterisks as literal text.
                current.push('*');
                current.push('*');
                i += 2;
            }
        // Detect `` `code` ``
        } else if chars[i] == '`' {
            let start = i + 1;
            if let Some(j) = chars[start..].iter().position(|&c| c == '`') {
                let close = start + j;
                if !current.is_empty() {
                    spans.push(InlineSpan::plain(std::mem::take(&mut current)));
                }
                let code_text: String = chars[start..close].iter().collect();
                if !code_text.is_empty() {
                    spans.push(InlineSpan::code(code_text));
                }
                i = close + 1;
            } else {
                // No closing backtick — emit as literal.
                current.push('`');
                i += 1;
            }
        } else {
            current.push(chars[i]);
            i += 1;
        }
    }

    if !current.is_empty() {
        spans.push(InlineSpan::plain(current));
    }

    // Empty input → return one empty plain span so callers always iterate.
    if spans.is_empty() {
        spans.push(InlineSpan::plain(""));
    }

    spans
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

    // ── parse_inline (#48) ───────────────────────────────────────────────────

    #[test]
    fn parse_inline_plain_text() {
        let spans = parse_inline("hello world");
        assert_eq!(spans, vec![InlineSpan::plain("hello world")]);
    }

    #[test]
    fn parse_inline_bold() {
        let spans = parse_inline("**bold** word");
        assert_eq!(spans, vec![
            InlineSpan::bold("bold"),
            InlineSpan::plain(" word"),
        ]);
    }

    #[test]
    fn parse_inline_code() {
        let spans = parse_inline("run `cargo test` now");
        assert_eq!(spans, vec![
            InlineSpan::plain("run "),
            InlineSpan::code("cargo test"),
            InlineSpan::plain(" now"),
        ]);
    }

    #[test]
    fn parse_inline_mixed_bold_and_code() {
        let spans = parse_inline("**bold** and `code`");
        assert_eq!(spans, vec![
            InlineSpan::bold("bold"),
            InlineSpan::plain(" and "),
            InlineSpan::code("code"),
        ]);
    }

    #[test]
    fn parse_inline_unmatched_bold_marker_is_literal() {
        // A lone `**` with no closing pair must emit as literal asterisks.
        let spans = parse_inline("before ** after");
        // No closing `**` found — everything becomes plain.
        let combined: String = spans.iter().map(|s| s.text.as_str()).collect();
        assert!(combined.contains("**"), "unmatched ** must appear in output");
        assert!(spans.iter().all(|s| !s.bold), "no span should be bold when marker is unmatched");
    }

    #[test]
    fn parse_inline_empty_string() {
        let spans = parse_inline("");
        assert_eq!(spans.len(), 1);
        assert_eq!(spans[0].text, "");
        assert!(!spans[0].bold);
        assert!(!spans[0].code);
    }

    #[test]
    fn parse_inline_unmatched_backtick_is_literal() {
        let spans = parse_inline("open `tick no close");
        let combined: String = spans.iter().map(|s| s.text.as_str()).collect();
        assert!(combined.contains('`'), "unmatched backtick must appear in output");
        assert!(spans.iter().all(|s| !s.code), "no span should be code when backtick is unmatched");
    }

    #[test]
    fn parse_produces_entries_for_all_versions() {
        let entries = parse_changelog(CHANGELOG_MD);
        assert!(entries.len() >= 11, "expected at least 11 versions");
        // Assert the newest entry matches the current crate version rather than a
        // hardcoded string, so the test never goes stale on a release and also
        // enforces that every version bump ships a matching changelog entry.
        // The parsed version retains the heading's date suffix (e.g.
        // "v1.1.0 (2026-06-20)"), so match by prefix.
        assert!(
            entries[0].version.starts_with(&format!("v{}", VERSION)),
            "newest changelog entry ({}) must match crate version v{}",
            entries[0].version,
            VERSION
        );
        assert!(!entries[0].bullets.is_empty(), "first entry should have bullets");
    }
}
