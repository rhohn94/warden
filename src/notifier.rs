// Notifier: tracks previous app statuses and fires desktop notifications
// on Running <-> Stopped transitions.

use std::collections::HashMap;
use crate::models::AppStatus;

/// Fires macOS desktop notifications when an app transitions between
/// Running and Stopped states.  Tracks previous statuses internally so
/// the caller only needs to pass the current snapshot on each update.
pub struct Notifier {
    /// Previous known status per app name.
    prev: HashMap<String, AppStatus>,
    /// Whether notifications are enabled (from Config.notifications_enabled).
    enabled: bool,
}

impl Notifier {
    /// Create a new `Notifier`.  Pass `enabled = true` to show OS
    /// notifications; `false` still tracks transitions but stays silent.
    pub fn new(enabled: bool) -> Self {
        Notifier {
            prev: HashMap::new(),
            enabled,
        }
    }

    /// Call after each scanner/detector update with the fresh app list.
    ///
    /// `entries` is a slice of `(name, current_status)` pairs.
    ///
    /// * First time a name is seen → stored in `prev`; no notification
    ///   (startup populate so we don't flood on launch).
    /// * Subsequent calls → fire a notification when Running↔Stopped
    ///   transitions occur (if `enabled`).
    /// * `AppStatus::Unknown` is treated as Stopped for comparison; an
    ///   Unknown→Unknown or Unknown→Unknown sequence is considered unchanged
    ///   and produces no notification.
    ///
    /// Returns the list of `(name, new_status)` pairs whose status changed.
    pub fn check_transitions(&mut self, entries: &[(String, AppStatus)]) -> Vec<(String, AppStatus)> {
        let mut fired: Vec<(String, AppStatus)> = Vec::new();

        for (name, status) in entries {
            match self.prev.get(name) {
                None => {
                    // First time seen — populate only, no notification.
                    self.prev.insert(name.clone(), status.clone());
                }
                Some(prev_status) => {
                    if transition_changed(prev_status, status) {
                        if self.enabled {
                            let label = status_label(status);
                            let _ = notify_rust::Notification::new()
                                .summary("Warden")
                                .body(&format!("{} is now {}", name, label))
                                .show();
                        }
                        fired.push((name.clone(), status.clone()));
                    }
                    self.prev.insert(name.clone(), status.clone());
                }
            }
        }

        fired
    }
}

/// Returns `true` when the status change is a meaningful Running↔Stopped
/// transition worth notifying about.
///
/// `AppStatus::Unknown` is treated as equivalent to Stopped for this
/// comparison so that Unknown→Stopped (or the reverse) is silent, but
/// Unknown→Running and Running→Unknown still fire.
fn transition_changed(prev: &AppStatus, current: &AppStatus) -> bool {
    // Normalise Unknown to Stopped so we only fire on real Running changes.
    let prev_running = matches!(prev, AppStatus::Running { .. });
    let curr_running = matches!(current, AppStatus::Running { .. });
    prev_running != curr_running
}

/// Human-readable label for a status, used in notification bodies.
fn status_label(status: &AppStatus) -> &'static str {
    match status {
        AppStatus::Running { .. } => "Running",
        AppStatus::Stopped => "Stopped",
        AppStatus::Unknown => "Stopped",
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn running() -> AppStatus {
        AppStatus::Running { pid: 1234 }
    }

    fn stopped() -> AppStatus {
        AppStatus::Stopped
    }

    fn unknown() -> AppStatus {
        AppStatus::Unknown
    }

    /// Helper: create (name, status) pairs from a slice.
    fn pairs(data: &[(&str, AppStatus)]) -> Vec<(String, AppStatus)> {
        data.iter().map(|(n, s)| (n.to_string(), s.clone())).collect()
    }

    #[test]
    fn test_first_call_no_notifications() {
        let mut n = Notifier::new(false);
        let entries = pairs(&[
            ("app-a", running()),
            ("app-b", stopped()),
        ]);
        let fired = n.check_transitions(&entries);
        assert!(fired.is_empty(), "startup populate must not fire any transition");
    }

    #[test]
    fn test_second_call_no_change_no_notification() {
        let mut n = Notifier::new(false);
        let entries = pairs(&[("app-a", running())]);
        n.check_transitions(&entries); // populate
        let fired = n.check_transitions(&entries); // same status
        assert!(fired.is_empty(), "unchanged status must not fire");
    }

    #[test]
    fn test_running_to_stopped_fires_transition() {
        let mut n = Notifier::new(false);
        let first = pairs(&[("app-a", running())]);
        n.check_transitions(&first); // populate

        let second = pairs(&[("app-a", stopped())]);
        let fired = n.check_transitions(&second);

        assert_eq!(fired.len(), 1, "exactly one transition expected");
        assert_eq!(fired[0].0, "app-a");
        assert!(matches!(fired[0].1, AppStatus::Stopped));
    }

    #[test]
    fn test_stopped_to_running_fires_transition() {
        let mut n = Notifier::new(false);
        let first = pairs(&[("app-a", stopped())]);
        n.check_transitions(&first); // populate

        let second = pairs(&[("app-a", running())]);
        let fired = n.check_transitions(&second);

        assert_eq!(fired.len(), 1, "exactly one transition expected");
        assert_eq!(fired[0].0, "app-a");
        assert!(matches!(fired[0].1, AppStatus::Running { .. }));
    }

    #[test]
    fn test_unknown_status_does_not_crash() {
        let mut n = Notifier::new(false);
        let first = pairs(&[("app-a", unknown())]);
        n.check_transitions(&first); // populate — must not panic

        let second = pairs(&[("app-a", unknown())]);
        let fired = n.check_transitions(&second);
        assert!(fired.is_empty(), "unknown→unknown must not fire");
    }

    #[test]
    fn test_unknown_to_running_fires_transition() {
        let mut n = Notifier::new(false);
        let first = pairs(&[("app-a", unknown())]);
        n.check_transitions(&first); // populate

        let second = pairs(&[("app-a", running())]);
        let fired = n.check_transitions(&second);
        assert_eq!(fired.len(), 1, "unknown→running should fire (app came up)");
    }

    #[test]
    fn test_multiple_apps_only_changed_fires() {
        let mut n = Notifier::new(false);
        let first = pairs(&[("app-a", running()), ("app-b", running())]);
        n.check_transitions(&first); // populate

        let second = pairs(&[("app-a", stopped()), ("app-b", running())]);
        let fired = n.check_transitions(&second);
        assert_eq!(fired.len(), 1);
        assert_eq!(fired[0].0, "app-a");
    }

    #[test]
    fn test_transitions_tracked_even_when_disabled() {
        // When disabled, notifications are suppressed but transitions are still returned.
        let mut n = Notifier::new(false);
        let first = pairs(&[("app-a", running())]);
        n.check_transitions(&first);
        let second = pairs(&[("app-a", stopped())]);
        let fired = n.check_transitions(&second);
        assert_eq!(fired.len(), 1, "transition must be tracked even when notifications are off");
    }
}
