// config.rs — real, minimal config loading (12-factor style): defaults, then
// an optional `config.env` file (KEY=VALUE per line, `#` comments) in the
// current working directory, then real process environment variables
// (highest precedence). No magic numbers: every default lives in
// `Config::default()`. Replace/extend fields as the service grows.

use std::collections::HashMap;
use std::env;
use std::fs;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// tracing/logging_init.rs level filter (trace/debug/info/warn/error).
    /// See docs/coding-standards.md §Logging.
    pub log_level: String,
    /// Default bind port (matches the web quick-start template's default).
    pub port: u16,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            log_level: "info".to_string(),
            port: 3000,
        }
    }
}

impl Config {
    /// Load config: defaults -> `config.env` (CWD, or `$CONFIG_ENV_PATH`) ->
    /// real process env vars (`LOG_LEVEL`, `PORT`).
    pub fn load() -> Config {
        let mut cfg = Config::default();
        let file_path = env::var("CONFIG_ENV_PATH").unwrap_or_else(|_| "config.env".to_string());
        if let Ok(text) = fs::read_to_string(&file_path) {
            apply_env_file(&mut cfg, &text);
        }
        if let Ok(v) = env::var("LOG_LEVEL") {
            cfg.log_level = v;
        }
        if let Ok(v) = env::var("PORT") {
            if let Ok(p) = v.parse() {
                cfg.port = p;
            }
        }
        cfg
    }
}

fn apply_env_file(cfg: &mut Config, text: &str) {
    let map = parse_env_file(text);
    if let Some(v) = map.get("LOG_LEVEL") {
        cfg.log_level = v.clone();
    }
    if let Some(v) = map.get("PORT") {
        if let Ok(p) = v.parse() {
            cfg.port = p;
        }
    }
}

/// Parse simple `KEY=VALUE` lines; blank lines and `#` comments are ignored.
/// Not a TOML/YAML parser — intentionally simple and honest about its scope.
pub fn parse_env_file(text: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            map.insert(k.trim().to_string(), v.trim().to_string());
        }
    }
    map
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_port_and_log_level() {
        let cfg = Config::default();
        assert_eq!(cfg.log_level, "info");
        assert_eq!(cfg.port, 3000);
    }

    #[test]
    fn env_file_overrides_defaults() {
        let mut cfg = Config::default();
        apply_env_file(&mut cfg, "LOG_LEVEL=debug\nPORT=8080\n");
        assert_eq!(cfg.log_level, "debug");
        assert_eq!(cfg.port, 8080);
    }

    #[test]
    fn malformed_port_is_ignored_not_a_panic() {
        let mut cfg = Config::default();
        apply_env_file(&mut cfg, "PORT=not-a-number\n");
        assert_eq!(cfg.port, 3000, "malformed PORT must not change the default");
    }

    #[test]
    fn parse_env_file_skips_blank_and_comment_lines() {
        let map = parse_env_file("A=1\n\n# comment\nB = 2 \n");
        assert_eq!(map.get("A").map(String::as_str), Some("1"));
        assert_eq!(map.get("B").map(String::as_str), Some("2"));
        assert_eq!(map.len(), 2);
    }
}
