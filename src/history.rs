// HistoryStore: per-app start/stop event ring buffer with JSON persistence.
// Events are recorded on status transitions and stored in
// ~/.config/warden/history.json (max 100 entries per app).

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;

const MAX_EVENTS_PER_APP: usize = 100;

/// A single lifecycle event for a managed app.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum HistoryEvent {
    Started { at: DateTime<Utc>, pid: u32 },
    Stopped { at: DateTime<Utc>, duration_secs: u64 },
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

    fn load_from(path: &std::path::Path) -> Self {
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

    /// Save to `~/.config/warden/history.json`.
    /// Creates parent directories as needed; logs a warning on error but does not panic.
    pub fn save(&self) {
        let Some(path) = Self::history_path() else {
            tracing::warn!("history: cannot determine config directory, skipping save");
            return;
        };
        if let Some(parent) = path.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                tracing::warn!("history: could not create config dir {}: {}", parent.display(), e);
                return;
            }
        }
        match serde_json::to_string_pretty(self) {
            Ok(json) => {
                if let Err(e) = std::fs::write(&path, json) {
                    tracing::warn!("history: could not write {}: {}", path.display(), e);
                }
            }
            Err(e) => {
                tracing::warn!("history: serialization error: {}", e);
            }
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
}
