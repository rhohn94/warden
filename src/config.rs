// Config: persistent user settings loaded from ~/.config/warden/config.toml
// and saved back when values are explicitly set via CLI.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Persistent user settings for Warden, backed by ~/.config/warden/config.toml.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// Directory to watch for apps (matches --apps-dir CLI flag)
    pub apps_dir: Option<String>,
    /// Scanner refresh interval in seconds (matches --refresh CLI flag)
    pub refresh_secs: Option<u64>,
    /// Send desktop notifications on app status changes (default true)
    pub notifications_enabled: Option<bool>,
    /// Maximum log lines to retain per app in the tail buffer (default 500)
    pub log_tail_lines: Option<usize>,
    /// Interval in seconds between version-update checks; 0 disables (default 3600)
    pub version_check_interval_secs: Option<u64>,
    /// Performance telemetry settings
    pub perf: Option<PerfConfig>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            apps_dir: None,
            refresh_secs: Some(5),
            notifications_enabled: Some(true),
            log_tail_lines: Some(500),
            version_check_interval_secs: Some(3600),
            perf: Some(PerfConfig::default()),
        }
    }
}

/// Performance telemetry settings.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PerfConfig {
    /// Frame time (ms) above which a warning is logged (default 50)
    pub frame_warn_ms: Option<u64>,
}

impl Default for PerfConfig {
    fn default() -> Self {
        Self {
            frame_warn_ms: Some(50),
        }
    }
}

impl Config {
    /// Load config from ~/.config/warden/config.toml.
    /// Returns default config if the file does not exist.
    pub fn load() -> Self {
        match Self::config_path() {
            Some(path) => Self::load_from(&path),
            None => Self::default(),
        }
    }

    /// Load config from the given path (useful for hermetic tests).
    /// Returns default config if the file does not exist.
    pub fn load_from(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(contents) => toml::from_str(&contents).unwrap_or_default(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Self::default(),
            Err(e) => {
                tracing::warn!("could not read config file {}: {}", path.display(), e);
                Self::default()
            }
        }
    }

    /// Save config to ~/.config/warden/config.toml.
    /// Creates parent directories as needed.
    pub fn save(&self) -> Result<(), Box<dyn std::error::Error>> {
        let path = Self::config_path().ok_or("cannot determine config directory")?;
        self.save_to(&path)
    }

    /// Save config to the given path (useful for hermetic tests).
    /// Creates parent directories as needed.
    pub fn save_to(&self, path: &Path) -> Result<(), Box<dyn std::error::Error>> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let contents = toml::to_string_pretty(self)?;
        std::fs::write(path, contents)?;
        Ok(())
    }

    /// Return the config file path: ~/.config/warden/config.toml.
    pub fn config_path() -> Option<PathBuf> {
        dirs::config_dir().map(|d| d.join("warden").join("config.toml"))
    }

    /// Return the frame-time warning threshold in milliseconds (default 50).
    pub fn frame_warn_ms(&self) -> u64 {
        self.perf
            .as_ref()
            .and_then(|p| p.frame_warn_ms)
            .unwrap_or(50)
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    #[test]
    fn test_load_from_valid_toml() {
        let mut f = NamedTempFile::new().unwrap();
        writeln!(f, r#"apps_dir = "/tmp/test""#).unwrap();
        writeln!(f, "refresh_secs = 10").unwrap();
        let cfg = Config::load_from(f.path());
        assert_eq!(cfg.apps_dir.as_deref(), Some("/tmp/test"));
        assert_eq!(cfg.refresh_secs, Some(10));
    }

    #[test]
    fn test_load_from_missing_file_returns_default() {
        let cfg = Config::load_from(Path::new("/nonexistent/path/config.toml"));
        let def = Config::default();
        assert_eq!(cfg.apps_dir, def.apps_dir);
        assert_eq!(cfg.refresh_secs, def.refresh_secs);
        assert_eq!(cfg.notifications_enabled, def.notifications_enabled);
        assert_eq!(cfg.log_tail_lines, def.log_tail_lines);
        assert_eq!(cfg.version_check_interval_secs, def.version_check_interval_secs);
    }

    #[test]
    fn test_save_to_and_reload() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        let cfg = Config {
            apps_dir: Some("/tmp/myapps".to_string()),
            refresh_secs: Some(15),
            notifications_enabled: Some(false),
            log_tail_lines: Some(200),
            version_check_interval_secs: Some(7200),
            perf: None,
        };
        cfg.save_to(&path).unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(raw.contains("apps_dir"));
        assert!(raw.contains("refresh_secs"));
        assert!(raw.contains("notifications_enabled"));
        assert!(raw.contains("log_tail_lines"));
        assert!(raw.contains("version_check_interval_secs"));

        let loaded = Config::load_from(&path);
        assert_eq!(loaded.apps_dir.as_deref(), Some("/tmp/myapps"));
        assert_eq!(loaded.refresh_secs, Some(15));
        assert_eq!(loaded.notifications_enabled, Some(false));
        assert_eq!(loaded.log_tail_lines, Some(200));
        assert_eq!(loaded.version_check_interval_secs, Some(7200));
    }

    #[test]
    fn test_save_creates_parent_dirs() {
        let dir = tempfile::tempdir().unwrap();
        let nested_path = dir.path().join("a").join("b").join("config.toml");
        let cfg = Config::default();
        cfg.save_to(&nested_path).unwrap();
        assert!(nested_path.exists());
    }

    #[test]
    fn test_default_values() {
        let cfg = Config::default();
        assert!(cfg.apps_dir.is_none());
        assert_eq!(cfg.refresh_secs, Some(5));
        assert_eq!(cfg.notifications_enabled, Some(true));
        assert_eq!(cfg.log_tail_lines, Some(500));
        assert_eq!(cfg.version_check_interval_secs, Some(3600));
    }

    #[test]
    fn test_config_path_is_some() {
        // On any platform with a home dir, this should return Some.
        // We just verify it doesn't panic and ends with the expected suffix.
        if let Some(p) = Config::config_path() {
            assert!(p.ends_with("warden/config.toml"));
        }
    }

    #[test]
    fn test_frame_warn_ms_default() {
        let cfg = Config::default();
        assert_eq!(cfg.frame_warn_ms(), 50);
    }

    #[test]
    fn test_frame_warn_ms_custom() {
        let cfg = Config {
            apps_dir: None,
            refresh_secs: None,
            notifications_enabled: None,
            log_tail_lines: None,
            version_check_interval_secs: None,
            perf: Some(PerfConfig { frame_warn_ms: Some(100) }),
        };
        assert_eq!(cfg.frame_warn_ms(), 100);
    }

    #[test]
    fn test_frame_warn_ms_none_perf_falls_back_to_default() {
        let cfg = Config {
            apps_dir: None,
            refresh_secs: None,
            notifications_enabled: None,
            log_tail_lines: None,
            version_check_interval_secs: None,
            perf: None,
        };
        assert_eq!(cfg.frame_warn_ms(), 50);
    }

    #[test]
    fn test_perf_config_default() {
        let perf = PerfConfig::default();
        assert_eq!(perf.frame_warn_ms, Some(50));
    }
}
