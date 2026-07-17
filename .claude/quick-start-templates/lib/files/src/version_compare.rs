//! A tiny, real, dependency-free version-token comparator for Grimoire-style
//! `X.Y` / `X.Y.Z` version tokens (with or without a leading `v`) — the exact
//! shape `docs/version-history.md` headings and `.claude/grimoire-config.json`'s
//! `framework-version` use. Not a full SemVer implementation (no pre-release
//! or build-metadata handling) — scoped to exactly what the framework's own
//! version tokens need. This is the crate's real, exemplar public API.

use std::cmp::Ordering;

/// Parse a version token like `"v3.94"` or `"3.94.1"` into `(major, minor,
/// patch)`, defaulting an absent patch component to `0`.
///
/// ```
/// use lib_app::version_compare::parse;
///
/// assert_eq!(parse("v3.94"), Some((3, 94, 0)));
/// assert_eq!(parse("3.94.1"), Some((3, 94, 1)));
/// assert_eq!(parse("not-a-version"), None);
/// ```
pub fn parse(token: &str) -> Option<(u64, u64, u64)> {
    let stripped = token.strip_prefix('v').unwrap_or(token);
    let mut parts = stripped.split('.');
    let major: u64 = parts.next()?.parse().ok()?;
    let minor: u64 = parts.next()?.parse().ok()?;
    let patch: u64 = match parts.next() {
        Some(p) => p.parse().ok()?,
        None => 0,
    };
    if parts.next().is_some() {
        return None; // more than 3 dotted components — not a token we understand
    }
    Some((major, minor, patch))
}

/// Compare two version tokens. Returns `None` if either fails to parse.
///
/// ```
/// use lib_app::version_compare::compare;
/// use std::cmp::Ordering;
///
/// assert_eq!(compare("v3.94", "v3.93"), Some(Ordering::Greater));
/// assert_eq!(compare("v3.94.0", "v3.94"), Some(Ordering::Equal));
/// assert_eq!(compare("bogus", "v3.94"), None);
/// ```
pub fn compare(a: &str, b: &str) -> Option<Ordering> {
    let a = parse(a)?;
    let b = parse(b)?;
    Some(a.cmp(&b))
}

/// Format `(major, minor, patch)` back into a `vX.Y.Z` token — the inverse of
/// [`parse`] (round-trips for any value `parse` can produce).
///
/// ```
/// use lib_app::version_compare::{format_token, parse};
///
/// assert_eq!(format_token((3, 94, 0)), "v3.94.0");
/// assert_eq!(parse(&format_token((1, 2, 3))), Some((1, 2, 3)));
/// ```
pub fn format_token(version: (u64, u64, u64)) -> String {
    format!("v{}.{}.{}", version.0, version.1, version.2)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_bare_and_v_prefixed() {
        assert_eq!(parse("3.94"), Some((3, 94, 0)));
        assert_eq!(parse("v3.94"), Some((3, 94, 0)));
    }

    #[test]
    fn parses_patch_component() {
        assert_eq!(parse("v1.2.3"), Some((1, 2, 3)));
    }

    #[test]
    fn rejects_malformed_tokens() {
        assert_eq!(parse(""), None);
        assert_eq!(parse("v3"), None);
        assert_eq!(parse("v3.94.1.2"), None);
        assert_eq!(parse("v3.x"), None);
    }

    #[test]
    fn compare_orders_correctly() {
        assert_eq!(compare("v3.94", "v3.93"), Some(Ordering::Greater));
        assert_eq!(compare("v3.93", "v3.94"), Some(Ordering::Less));
        assert_eq!(compare("v3.94.0", "v3.94"), Some(Ordering::Equal));
    }

    #[test]
    fn compare_none_on_malformed() {
        assert_eq!(compare("nope", "v3.94"), None);
    }

    #[test]
    fn format_token_round_trips_through_parse() {
        let original = (3, 94, 1);
        assert_eq!(parse(&format_token(original)), Some(original));
    }
}
