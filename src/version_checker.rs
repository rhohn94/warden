// VersionChecker: background task that periodically checks each app's
// deployed version against the latest git tag on its remote.

use crate::models::{AppEntry, VersionCheckResult};
use std::{
    cmp::Ordering,
    collections::HashMap,
    sync::{Arc, RwLock},
};

/// Fallback GitHub owner when an app carries no explicit repo slug — every
/// fleet repo lives under this account (#54).
const DEFAULT_GITHUB_OWNER: &str = "rhohn94";

/// Runs a periodic background sweep that checks each monitored app's
/// deployed version against the latest git tag visible on its remote.
/// Results are stored in a shared map keyed by app name.
pub struct VersionChecker {
    results: Arc<RwLock<HashMap<String, VersionCheckResult>>>,
}

impl VersionChecker {
    /// Create a new `VersionChecker` with an empty results map.
    pub fn new() -> Self {
        VersionChecker {
            results: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Return the shared results map so the UI can read it without copying.
    pub fn results(&self) -> Arc<RwLock<HashMap<String, VersionCheckResult>>> {
        Arc::clone(&self.results)
    }

    /// Spawn a background tokio task that sweeps `entries` every `interval_secs`.
    ///
    /// If `interval_secs` is 0 the method is a no-op (version checking disabled).
    /// The task runs inside the provided `runtime` handle so it integrates with
    /// the existing single tokio runtime in `main`.
    pub fn start(
        &self,
        entries: Vec<AppEntry>,
        interval_secs: u64,
        runtime: tokio::runtime::Handle,
    ) {
        if interval_secs == 0 {
            return;
        }

        let results = Arc::clone(&self.results);
        runtime.spawn(async move {
            loop {
                for entry in &entries {
                    let result = check_entry(entry).await;
                    if let Ok(mut map) = results.write() {
                        map.insert(entry.name.clone(), result);
                    }
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(interval_secs)).await;
            }
        });
    }
}

/// Query the app's remote for its latest tag and compare it to the app's
/// recorded deployed version.
///
/// Deploy directories under ~/Projects/deployed-apps are rsynced trees, NOT
/// git checkouts — `git -C <dir> ls-remote origin` always failed there, so
/// every instance permanently reported `Unknown` (#54). The remote URL is now
/// derived without a local git dir: an explicit `owner/name` slug from the
/// grimoire config wins, else the app dir name under the default owner. A real
/// git checkout (project dir) still uses its own `origin`.
async fn check_entry(entry: &AppEntry) -> VersionCheckResult {
    let mut cmd = tokio::process::Command::new("git");
    if entry.dir.join(".git").exists() {
        cmd.args([
            "-C",
            entry.dir.to_string_lossy().as_ref(),
            "ls-remote",
            "--tags",
            "--sort=-version:refname",
            "origin",
        ]);
    } else {
        cmd.args(["ls-remote", "--tags", "--sort=-version:refname", &remote_url(entry)]);
    }

    let output = match cmd.output().await {
        Ok(o) => o,
        Err(_) => return VersionCheckResult::Unknown,
    };

    if !output.status.success() {
        return VersionCheckResult::Unknown;
    }

    let stdout = match std::str::from_utf8(&output.stdout) {
        Ok(s) => s,
        Err(_) => return VersionCheckResult::Unknown,
    };

    let latest_tag = match latest_tag_from_ls_remote(stdout) {
        Some(t) => t,
        None => return VersionCheckResult::Unknown,
    };

    verdict(entry.framework_version.as_deref(), &latest_tag)
}

/// The remote URL to query when the app dir is not a git checkout.
fn remote_url(entry: &AppEntry) -> String {
    if let Some(repo) = &entry.repo {
        return format!("https://github.com/{repo}.git");
    }
    let name = entry
        .dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(&entry.name)
        .to_lowercase();
    format!("https://github.com/{DEFAULT_GITHUB_OWNER}/{name}.git")
}

/// Extract the newest tag from `git ls-remote --tags --sort=-version:refname`
/// output. Each line is `<sha>\trefs/tags/<tag>`; peeled refs (`^{}`) are
/// skipped; the first remaining tag is the latest.
fn latest_tag_from_ls_remote(stdout: &str) -> Option<String> {
    stdout
        .lines()
        .filter_map(|line| {
            let tab = line.find('\t')?;
            let refname = &line[tab + 1..];
            // Skip peeled refs (refs/tags/<tag>^{})
            if refname.ends_with("^{}") {
                return None;
            }
            refname.strip_prefix("refs/tags/").map(str::to_owned)
        })
        .next()
}

/// Compare the deployed version against the latest remote tag.
///
/// Versions are normalized (leading `v` stripped) and compared as semver, so
/// a deployed `1.87.1` matches tag `v1.87.1` instead of producing a false
/// `UpdateAvailable` on the prefix mismatch (#54).
fn verdict(current: Option<&str>, latest_tag: &str) -> VersionCheckResult {
    let Some(current) = current else {
        // Version unknown locally — we genuinely cannot say whether the
        // instance is stale, and claiming an update is available would be a
        // false signal.
        return VersionCheckResult::Unknown;
    };
    match semver_cmp(current, latest_tag) {
        Some(Ordering::Less) => VersionCheckResult::UpdateAvailable {
            latest: latest_tag.to_string(),
        },
        Some(_) => VersionCheckResult::UpToDate,
        // Not semver on one side (e.g. a framework marker like "3.36" vs a
        // date tag): fall back to normalized string equality.
        None => {
            if normalize_version(current) == normalize_version(latest_tag) {
                VersionCheckResult::UpToDate
            } else {
                VersionCheckResult::UpdateAvailable {
                    latest: latest_tag.to_string(),
                }
            }
        }
    }
}

/// Strip a leading `v`/`V` prefix (`v1.2.3` → `1.2.3`).
fn normalize_version(v: &str) -> &str {
    let v = v.trim();
    v.strip_prefix(['v', 'V']).unwrap_or(v)
}

/// A parsed semver-ish version: numeric core plus optional prerelease.
struct SemVer<'a> {
    core: Vec<u64>,
    prerelease: Option<&'a str>,
}

/// Parse `1.2.3`, `1.2`, `1.2.3-rc.1`, `1.2.3+build` (build metadata ignored).
/// Returns `None` when any core component is non-numeric.
fn parse_semver(v: &str) -> Option<SemVer<'_>> {
    let v = normalize_version(v);
    let v = v.split('+').next().unwrap_or(v); // drop build metadata
    let (core_str, prerelease) = match v.split_once('-') {
        Some((c, p)) => (c, Some(p)),
        None => (v, None),
    };
    if core_str.is_empty() {
        return None;
    }
    let core = core_str
        .split('.')
        .map(|part| part.parse::<u64>().ok())
        .collect::<Option<Vec<u64>>>()?;
    Some(SemVer { core, prerelease })
}

/// Semver comparison of two version strings (leading `v` tolerated). `None`
/// when either side does not parse as semver.
fn semver_cmp(a: &str, b: &str) -> Option<Ordering> {
    let a = parse_semver(a)?;
    let b = parse_semver(b)?;

    // Compare numeric cores, treating missing components as 0 (1.2 == 1.2.0).
    let len = a.core.len().max(b.core.len());
    for i in 0..len {
        let x = a.core.get(i).copied().unwrap_or(0);
        let y = b.core.get(i).copied().unwrap_or(0);
        match x.cmp(&y) {
            Ordering::Equal => {}
            other => return Some(other),
        }
    }

    // Equal cores: a prerelease sorts BELOW the release (1.2.3-rc.1 < 1.2.3).
    match (a.prerelease, b.prerelease) {
        (None, None) => Some(Ordering::Equal),
        (Some(_), None) => Some(Ordering::Less),
        (None, Some(_)) => Some(Ordering::Greater),
        (Some(pa), Some(pb)) => Some(prerelease_cmp(pa, pb)),
    }
}

/// Compare dot-separated prerelease identifiers per semver §11: numeric
/// identifiers compare numerically and sort below alphanumeric ones; a longer
/// identifier list wins a shared prefix.
fn prerelease_cmp(a: &str, b: &str) -> Ordering {
    let mut xs = a.split('.');
    let mut ys = b.split('.');
    loop {
        match (xs.next(), ys.next()) {
            (None, None) => return Ordering::Equal,
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
            (Some(x), Some(y)) => {
                let ord = match (x.parse::<u64>(), y.parse::<u64>()) {
                    (Ok(nx), Ok(ny)) => nx.cmp(&ny),
                    (Ok(_), Err(_)) => Ordering::Less,
                    (Err(_), Ok(_)) => Ordering::Greater,
                    (Err(_), Err(_)) => x.cmp(y),
                };
                if ord != Ordering::Equal {
                    return ord;
                }
            }
        }
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_new_produces_empty_results() {
        let checker = VersionChecker::new();
        let arc = checker.results();
        let map = arc.read().unwrap();
        assert!(map.is_empty(), "fresh VersionChecker must have no results");
    }

    #[test]
    fn test_update_available_reads_back_correctly() {
        let checker = VersionChecker::new();
        {
            let arc = checker.results();
            let mut map = arc.write().unwrap();
            map.insert(
                "my-app".to_string(),
                VersionCheckResult::UpdateAvailable {
                    latest: "v0.5.0".to_string(),
                },
            );
        }
        let arc = checker.results();
        let map = arc.read().unwrap();
        match map.get("my-app") {
            Some(VersionCheckResult::UpdateAvailable { latest }) => {
                assert_eq!(latest, "v0.5.0");
            }
            other => panic!("expected UpdateAvailable, got {:?}", other),
        }
    }

    // ── remote URL derivation (#54) ───────────────────────────────────────

    fn entry(dir: &str, repo: Option<&str>, version: Option<&str>) -> AppEntry {
        AppEntry {
            name: PathBuf::from(dir)
                .file_name()
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            dir: PathBuf::from(dir),
            framework_version: version.map(str::to_string),
            repo: repo.map(str::to_string),
            ..Default::default()
        }
    }

    #[test]
    fn remote_url_prefers_explicit_repo_slug() {
        let e = entry("/apps/discord-bot", Some("rhohn94/discord-bot"), None);
        assert_eq!(
            remote_url(&e),
            "https://github.com/rhohn94/discord-bot.git"
        );
    }

    #[test]
    fn remote_url_falls_back_to_dir_name_under_default_owner() {
        let e = entry("/apps/goon-cave", None, None);
        assert_eq!(remote_url(&e), "https://github.com/rhohn94/goon-cave.git");
    }

    // ── ls-remote parsing ─────────────────────────────────────────────────

    #[test]
    fn latest_tag_skips_peeled_refs() {
        let out = "abc1\trefs/tags/v1.3.0^{}\nabc2\trefs/tags/v1.3.0\nabc3\trefs/tags/v1.2.0\n";
        assert_eq!(latest_tag_from_ls_remote(out).as_deref(), Some("v1.3.0"));
    }

    #[test]
    fn latest_tag_none_for_empty_output() {
        assert_eq!(latest_tag_from_ls_remote(""), None);
    }

    // ── version normalization & compare (#54 acceptance 2) ───────────────

    #[test]
    fn v_prefix_mismatch_is_up_to_date() {
        // The original defect: deployed "1.87.1" vs tag "v1.87.1" reported a
        // false UpdateAvailable on the string mismatch.
        assert_eq!(verdict(Some("1.87.1"), "v1.87.1"), VersionCheckResult::UpToDate);
        assert_eq!(verdict(Some("v1.87.1"), "1.87.1"), VersionCheckResult::UpToDate);
    }

    #[test]
    fn older_deployed_version_reports_update() {
        assert_eq!(
            verdict(Some("1.87.1"), "v1.90.0"),
            VersionCheckResult::UpdateAvailable {
                latest: "v1.90.0".to_string()
            }
        );
    }

    #[test]
    fn newer_local_version_is_up_to_date_not_update() {
        // A dev checkout ahead of the latest tag must not flag an update.
        assert_eq!(verdict(Some("2.0.0"), "v1.9.9"), VersionCheckResult::UpToDate);
    }

    #[test]
    fn unknown_local_version_is_unknown_not_update() {
        assert_eq!(verdict(None, "v1.0.0"), VersionCheckResult::Unknown);
    }

    #[test]
    fn prerelease_sorts_below_release() {
        assert_eq!(semver_cmp("1.2.3-rc.1", "1.2.3"), Some(Ordering::Less));
        assert_eq!(semver_cmp("1.2.3", "1.2.3-rc.1"), Some(Ordering::Greater));
        assert_eq!(
            verdict(Some("1.2.3-rc.1"), "v1.2.3"),
            VersionCheckResult::UpdateAvailable {
                latest: "v1.2.3".to_string()
            }
        );
    }

    #[test]
    fn prerelease_identifiers_compare_numerically() {
        assert_eq!(semver_cmp("1.0.0-rc.2", "1.0.0-rc.10"), Some(Ordering::Less));
        assert_eq!(semver_cmp("1.0.0-alpha", "1.0.0-beta"), Some(Ordering::Less));
        assert_eq!(semver_cmp("1.0.0-rc.1", "1.0.0-rc.1"), Some(Ordering::Equal));
    }

    #[test]
    fn missing_components_are_zero() {
        assert_eq!(semver_cmp("1.2", "1.2.0"), Some(Ordering::Equal));
        assert_eq!(semver_cmp("v1.2", "1.2.1"), Some(Ordering::Less));
    }

    #[test]
    fn build_metadata_is_ignored() {
        assert_eq!(semver_cmp("1.2.3+abc", "1.2.3"), Some(Ordering::Equal));
    }

    #[test]
    fn non_semver_falls_back_to_normalized_string_equality() {
        assert_eq!(semver_cmp("nightly", "1.0.0"), None);
        assert_eq!(
            verdict(Some("nightly"), "nightly"),
            VersionCheckResult::UpToDate
        );
        assert_eq!(
            verdict(Some("nightly"), "v2026.07"),
            VersionCheckResult::UpdateAvailable {
                latest: "v2026.07".to_string()
            }
        );
    }
}
