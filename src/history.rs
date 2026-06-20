// HistoryStore: per-app start/stop event ring buffer with JSON persistence.
// Events are recorded on status transitions and stored in
// ~/.config/warden/history.json (max 100 entries per app).

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};

const MAX_EVENTS_PER_APP: usize = 100;

/// A single lifecycle event for a managed app.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum HistoryEvent {
    Started { at: DateTime<Utc>, pid: u32 },
    Stopped { at: DateTime<Utc>, duration_secs: u64 },
    Crashed { at: DateTime<Utc>, duration_secs: u64 },
}

/// Persistent ring-buffer of `HistoryEvent`s, keyed by app name.
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct HistoryStore {
    pub entries: HashMap<String, VecDeque<HistoryEvent>>,
}

impl HistoryStore {
    /// Create a new, empty `HistoryStore`.
    pub fn new() -> Self {
        Self::default()
    }

    /// Load from `~/.config/warden/history.json`.
    /// Returns `Self::new()` on missing file or parse error.
    pub fn load() -> Self {
        match Self::history_path() {
            Some(path) => Self::load_from(&path),
            None => Self::new(),
        }
    }

    /// Load from the given path (useful for hermetic tests).
    /// Returns `Self::new()` on missing file or parse error.
    pub fn load_from(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(contents) => serde_json::from_str(&contents).unwrap_or_else(|e| {
                tracing::warn!("could not parse history file {}: {}", path.display(), e);
                Self::new()
            }),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Self::new(),
            Err(e) => {
                tracing::warn!("could not read history file {}: {}", path.display(), e);
                Self::new()
            }
        }
    }

    /// Save to `~/.config/warden/history.json` atomically.
    /// Creates parent directories as needed; logs a warning on error but does not panic.
    pub fn save(&self) {
        let Some(path) = Self::history_path() else {
            tracing::warn!("history: cannot determine config directory, skipping save");
            return;
        };
        self.save_to(&path);
    }

    /// Save to `path` atomically: serialize to a sibling `.tmp` file in the same
    /// directory, then rename over the target so the live path is never written
    /// directly.  A failed temp write leaves any pre-existing `history.json`
    /// intact.  On rename failure the temp file is removed and a warning is
    /// logged; the method never panics.
    pub fn save_to(&self, path: &Path) {
        if let Some(parent) = path.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                tracing::warn!("history: could not create config dir {}: {}", parent.display(), e);
                return;
            }
        }

        let json = match serde_json::to_string_pretty(self) {
            Ok(j) => j,
            Err(e) => {
                tracing::warn!("history: serialization error: {}", e);
                return;
            }
        };

        // Build the temp path next to the target (same filesystem → atomic rename).
        let tmp_path = path.with_extension("json.tmp");

        if let Err(e) = std::fs::write(&tmp_path, &json) {
            tracing::warn!("history: could not write temp file {}: {}", tmp_path.display(), e);
            return;
        }

        if let Err(e) = std::fs::rename(&tmp_path, path) {
            tracing::warn!(
                "history: could not rename {} → {}: {}",
                tmp_path.display(),
                path.display(),
                e
            );
            // Clean up the temp file; ignore secondary errors.
            let _ = std::fs::remove_file(&tmp_path);
        }
    }

    /// Record a `Started` event for `app_name`.  Caps the ring buffer at 100 entries.
    pub fn record_started(&mut self, app_name: &str, pid: u32) {
        let buf = self.entries.entry(app_name.to_string()).or_default();
        buf.push_back(HistoryEvent::Started { at: Utc::now(), pid });
        while buf.len() > MAX_EVENTS_PER_APP {
            buf.pop_front();
        }
    }

    /// Record a `Stopped` event for `app_name`.
    /// `duration_secs` is derived from the last `Started` event; 0 if none exists.
    /// Caps the ring buffer at 100 entries.
    pub fn record_stopped(&mut self, app_name: &str) {
        let duration_secs = self
            .last_started_at(app_name)
            .map(|started| {
                let elapsed = Utc::now().signed_duration_since(started);
                elapsed.num_seconds().max(0) as u64
            })
            .unwrap_or(0);

        let buf = self.entries.entry(app_name.to_string()).or_default();
        buf.push_back(HistoryEvent::Stopped { at: Utc::now(), duration_secs });
        while buf.len() > MAX_EVENTS_PER_APP {
            buf.pop_front();
        }
    }

    /// Record a `Crashed` event for `app_name`.
    /// `duration_secs` is derived from the last `Started` event; 0 if none exists.
    /// Caps the ring buffer at 100 entries.
    pub fn record_crashed(&mut self, app_name: &str) {
        let duration_secs = self
            .last_started_at(app_name)
            .map(|started| {
                let elapsed = Utc::now().signed_duration_since(started);
                elapsed.num_seconds().max(0) as u64
            })
            .unwrap_or(0);

        let buf = self.entries.entry(app_name.to_string()).or_default();
        buf.push_back(HistoryEvent::Crashed { at: Utc::now(), duration_secs });
        while buf.len() > MAX_EVENTS_PER_APP {
            buf.pop_front();
        }
    }

    /// Returns the last `n` events for `app_name` in reverse-chronological order
    /// (most recent first).
    pub fn recent(&self, app_name: &str, n: usize) -> Vec<&HistoryEvent> {
        match self.entries.get(app_name) {
            Some(buf) => buf.iter().rev().take(n).collect(),
            None => Vec::new(),
        }
    }

    /// Returns the timestamp of the most recent `Started` event for `app_name`,
    /// or `None` if no `Started` event exists.
    pub fn last_started_at(&self, app_name: &str) -> Option<DateTime<Utc>> {
        self.entries.get(app_name)?.iter().rev().find_map(|e| {
            if let HistoryEvent::Started { at, .. } = e {
                Some(*at)
            } else {
                None
            }
        })
    }

    fn history_path() -> Option<PathBuf> {
        dirs::config_dir().map(|d| d.join("warden").join("history.json"))
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_record_started_then_stopped_correct_duration() {
        let mut store = HistoryStore::new();
        // Manually insert a Started event 10 seconds in the past.
        let started_at = Utc::now() - chrono::Duration::seconds(10);
        store
            .entries
            .entry("app".to_string())
            .or_default()
            .push_back(HistoryEvent::Started { at: started_at, pid: 42 });

        store.record_stopped("app");

        let events: Vec<&HistoryEvent> = store.recent("app", 2).into_iter().collect();
        // Most recent first: Stopped, then Started.
        assert_eq!(events.len(), 2);
        if let HistoryEvent::Stopped { duration_secs, .. } = events[0] {
            // Allow 9–20 s window to accommodate any scheduling jitter.
            assert!(
                *duration_secs >= 9 && *duration_secs <= 20,
                "duration_secs={} expected ~10",
                duration_secs
            );
        } else {
            panic!("most recent event should be Stopped");
        }
    }

    #[test]
    fn test_ring_buffer_capped_at_100() {
        let mut store = HistoryStore::new();
        for i in 0..101u32 {
            store.record_started("app", i);
        }
        let buf = store.entries.get("app").unwrap();
        assert_eq!(buf.len(), 100, "ring buffer must be capped at 100");
        // The oldest event (pid=0) should have been evicted; the newest pid is 100.
        if let HistoryEvent::Started { pid, .. } = buf.back().unwrap() {
            assert_eq!(*pid, 100);
        }
        if let HistoryEvent::Started { pid, .. } = buf.front().unwrap() {
            assert_eq!(*pid, 1, "pid=0 should have been evicted");
        }
    }

    #[test]
    fn test_recent_returns_reverse_chronological_order() {
        let mut store = HistoryStore::new();
        let base = Utc::now();
        let buf = store.entries.entry("app".to_string()).or_default();
        // Push 5 Started events with increasing timestamps.
        for i in 0..5u32 {
            buf.push_back(HistoryEvent::Started {
                at: base + chrono::Duration::seconds(i as i64),
                pid: i,
            });
        }
        let recent = store.recent("app", 5);
        assert_eq!(recent.len(), 5);
        // Most recent first → pids should be 4, 3, 2, 1, 0.
        let pids: Vec<u32> = recent
            .iter()
            .map(|e| {
                if let HistoryEvent::Started { pid, .. } = e {
                    *pid
                } else {
                    panic!("unexpected event type")
                }
            })
            .collect();
        assert_eq!(pids, vec![4, 3, 2, 1, 0]);
    }

    #[test]
    fn test_last_started_at_returns_most_recent_start() {
        let mut store = HistoryStore::new();
        let t1 = Utc::now() - chrono::Duration::seconds(30);
        let t2 = Utc::now() - chrono::Duration::seconds(10);
        let buf = store.entries.entry("app".to_string()).or_default();
        buf.push_back(HistoryEvent::Started { at: t1, pid: 1 });
        buf.push_back(HistoryEvent::Stopped {
            at: t1 + chrono::Duration::seconds(5),
            duration_secs: 5,
        });
        buf.push_back(HistoryEvent::Started { at: t2, pid: 2 });

        let result = store.last_started_at("app");
        assert!(result.is_some());
        // Should be t2, not t1.
        let diff = (result.unwrap() - t2).num_milliseconds().abs();
        assert!(diff < 5, "expected t2, diff={}ms", diff);
    }

    #[test]
    fn test_last_started_at_returns_none_when_empty() {
        let store = HistoryStore::new();
        assert!(store.last_started_at("missing-app").is_none());
    }

    #[test]
    fn test_recent_returns_empty_for_unknown_app() {
        let store = HistoryStore::new();
        assert!(store.recent("unknown", 10).is_empty());
    }

    #[test]
    fn test_record_crashed_emits_crashed_event_with_duration() {
        let mut store = HistoryStore::new();
        // Insert a Started event 5 seconds in the past.
        let started_at = Utc::now() - chrono::Duration::seconds(5);
        store
            .entries
            .entry("app".to_string())
            .or_default()
            .push_back(HistoryEvent::Started { at: started_at, pid: 77 });

        store.record_crashed("app");

        let events: Vec<&HistoryEvent> = store.recent("app", 2).into_iter().collect();
        assert_eq!(events.len(), 2, "should have Started + Crashed");
        if let HistoryEvent::Crashed { duration_secs, .. } = events[0] {
            assert!(
                *duration_secs >= 4 && *duration_secs <= 20,
                "duration_secs={} expected ~5",
                duration_secs
            );
        } else {
            panic!("most recent event should be Crashed, got {:?}", events[0]);
        }
    }

    #[test]
    fn test_record_crashed_with_no_prior_start_uses_zero_duration() {
        let mut store = HistoryStore::new();
        store.record_crashed("app");
        let events = store.recent("app", 1);
        assert_eq!(events.len(), 1);
        if let HistoryEvent::Crashed { duration_secs, .. } = events[0] {
            assert_eq!(*duration_secs, 0, "no prior start → duration must be 0");
        } else {
            panic!("expected Crashed event");
        }
    }

    // ── save_to / load_from round-trip tests ─────────────────────────────────

    /// Helper: build a store with one started event for "app".
    fn make_store_with_event() -> HistoryStore {
        let mut store = HistoryStore::new();
        store.record_started("app", 42);
        store
    }

    #[test]
    fn test_save_to_load_from_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.json");

        let original = make_store_with_event();
        original.save_to(&path);

        let loaded = HistoryStore::load_from(&path);
        assert_eq!(
            loaded.entries.len(),
            original.entries.len(),
            "loaded store should have same number of entries"
        );
        assert!(loaded.entries.contains_key("app"), "entry for 'app' must survive round-trip");
        assert_eq!(loaded.entries["app"].len(), 1);
    }

    #[test]
    fn test_save_to_over_existing_file_succeeds_and_parses() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.json");

        // Write an initial store.
        let mut first = HistoryStore::new();
        first.record_started("alpha", 1);
        first.save_to(&path);
        assert!(path.exists(), "first save must create the file");

        // Overwrite with a different store.
        let mut second = HistoryStore::new();
        second.record_started("beta", 2);
        second.save_to(&path);

        let loaded = HistoryStore::load_from(&path);
        assert!(loaded.entries.contains_key("beta"), "overwritten store must have 'beta'");
        assert!(!loaded.entries.contains_key("alpha"), "stale 'alpha' entry must not appear");
    }

    #[test]
    fn test_temp_write_failure_leaves_original_intact() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.json");

        // Write a valid original store.
        let mut good = HistoryStore::new();
        good.record_started("original", 99);
        good.save_to(&path);

        // Simulate a failed temp write by making the DIRECTORY read-only so
        // writing any new file fails.  Then call save_to with a path in that dir.
        let ro_dir = dir.path().join("readonly");
        std::fs::create_dir(&ro_dir).unwrap();
        let ro_path = ro_dir.join("history.json");

        // First, write a valid file there.
        let mut existing = HistoryStore::new();
        existing.record_started("safe", 7);
        existing.save_to(&ro_path);

        // Make the directory read-only to prevent new temp files.
        let mut perms = std::fs::metadata(&ro_dir).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        {
            use std::os::unix::fs::PermissionsExt;
            perms.set_mode(0o555);
        }
        std::fs::set_permissions(&ro_dir, perms.clone()).unwrap();

        // Attempt a save that must fail at the temp-write step.
        let mut bad = HistoryStore::new();
        bad.record_started("should-not-appear", 0);
        bad.save_to(&ro_path); // must warn, not panic

        // Restore permissions so tempdir cleanup works.
        #[allow(clippy::permissions_set_readonly_false)]
        {
            use std::os::unix::fs::PermissionsExt;
            perms.set_mode(0o755);
        }
        std::fs::set_permissions(&ro_dir, perms).unwrap();

        // The original file must still parse correctly.
        let recovered = HistoryStore::load_from(&ro_path);
        assert!(
            recovered.entries.contains_key("safe"),
            "original 'safe' entry must survive a failed overwrite"
        );
        assert!(
            !recovered.entries.contains_key("should-not-appear"),
            "failed write must not corrupt the existing file"
        );
    }
}
