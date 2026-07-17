// config_loader.rs — shared fail-closed APP_ENV config-loader module (#439).
//
// Unlike the sibling SEAM files (auth_seam.rs / db_seam.rs / cost_surface_seam.rs
// / updater_seam.rs), which are `grimoire:placeholder` scaffolds that
// `unimplemented!()`, THIS is a REAL, WORKING module — mirrors version_report.rs's
// precedent (#206): a scaffolded app can load real, layered config TODAY, with
// no vendored dependency and no crate to add first.
//
// Implements the layer order + fail-closed boot check specified in
// docs/grimoire/design/config-loader-design.md (extends
// deploy-environment-design.md §1-§2, framework-internal, upstream Grimoire
// repository — do NOT re-derive the contract here, read it there):
//
//     defaults.toml -> base.toml -> {APP_ENV}.toml -> local.toml -> process env
//     (lowest priority)                                          (highest priority)
//
// plus the `APP_ENV` fail-closed boot self-check: `APP_ENV` is read ONCE, at
// boot, explicitly (never inferred from hostname/build-flags/ambient signals);
// unset or unrecognized -> refuse to start with a clear error naming the bad
// value and the valid set.
//
// std-only by design (same rationale as version_report.rs): it carries a
// minimal internal TOML-subset reader scoped to flat `key = value` layers (no
// nested tables — each layer file IS one flat env profile), so it compiles and
// its tests run under a bare `rustc` before any config crate (figment,
// config-rs, ...) is vendored. Swap the internal reader for one of those later
// if you need nested tables or richer sources — the public surface
// (`AppEnv`, `Config::load`, the typed accessors) does not have to change.
//
// ── Call site (boot, before anything else) ───────────────────────────────────
//
//     fn main() {
//         let cfg = match config_loader::Config::load(std::path::Path::new(".")) {
//             Ok(cfg) => cfg,
//             Err(e) => {
//                 eprintln!("fatal: config load failed: {e}");
//                 std::process::exit(1);      // fail CLOSED — never boot on bad config
//             }
//         };
//         println!("booting in {} mode", cfg.app_env());
//         let port = cfg.get_i64("port").unwrap_or(8080);
//         let db_url = cfg.require_string("database_url").unwrap_or_else(|e| {
//             eprintln!("fatal: {e}");
//             std::process::exit(1);
//         });
//         // ... serve ...
//     }
//
// Layer files, all under `config/` (relative to the project root passed to
// `Config::load`). Every layer is OPTIONAL — a project may have none of them
// and `Config::load` still succeeds, AS LONG AS `APP_ENV` itself is valid;
// the fail-closed check is on `APP_ENV`, never on whether the (optional) layer
// files happen to exist yet (matches the framework's absence-as-default
// posture for the `environments` config block, deploy-environment-design.md §1):
//
//   config/defaults.toml    — lowest priority, checked into git
//   config/base.toml        — shared across all environments, checked in
//   config/{app_env}.toml   — e.g. config/dev.toml, config/production.toml
//   config/local.toml       — gitignored (config/.gitignore); developer-local
//                              overrides, never committed
//   process environment     — highest priority; a var overrides a config key
//                              `foo_bar` when `FOO_BAR` is set (UPPER_SNAKE_CASE
//                              of the key)
//
// Contract authority: docs/grimoire/design/config-loader-design.md
// (framework-internal, upstream Grimoire repository).

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::Path;

// ── AppEnv: the fail-closed boot check ───────────────────────────────────────

/// The four canonical named environments (deploy-environment-design.md §1).
pub const KNOWN_APP_ENVS: [&str; 4] = ["local", "dev", "beta", "production"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum AppEnv {
    Local,
    Dev,
    Beta,
    Production,
}

impl AppEnv {
    pub fn as_str(self) -> &'static str {
        match self {
            AppEnv::Local => "local",
            AppEnv::Dev => "dev",
            AppEnv::Beta => "beta",
            AppEnv::Production => "production",
        }
    }

    /// Fail-closed parse of an already-known-to-be-set `APP_ENV` string: `Err`
    /// on anything outside the canonical set. Never guesses, never defaults.
    pub fn parse(raw: &str) -> Result<AppEnv, ConfigError> {
        match raw {
            "local" => Ok(AppEnv::Local),
            "dev" => Ok(AppEnv::Dev),
            "beta" => Ok(AppEnv::Beta),
            "production" => Ok(AppEnv::Production),
            other => Err(ConfigError(format!(
                "APP_ENV={other:?} is not a recognized environment (valid: {})",
                KNOWN_APP_ENVS.join(", ")
            ))),
        }
    }

    /// The fail-closed boot check itself, decoupled from `std::env` so it is
    /// unit-testable without mutating real process state (process-env
    /// mutation races across parallel test threads). `raw` is what
    /// `std::env::var("APP_ENV").ok()` yields: `None` on unset, `Some(v)`
    /// otherwise. Unset and empty are refused with the same clarity as an
    /// unrecognized value — deploy-environment-design.md §2's "explicit read
    /// only ... fail-closed boot self-check ... never inferred" is this
    /// function.
    fn from_value(raw: Option<&str>) -> Result<AppEnv, ConfigError> {
        match raw {
            None => Err(ConfigError(format!(
                "APP_ENV is not set (valid: {}) — refusing to boot with an \
                 ambiguous environment",
                KNOWN_APP_ENVS.join(", ")
            ))),
            Some(v) if v.trim().is_empty() => Err(ConfigError(format!(
                "APP_ENV is empty (valid: {}) — refusing to boot with an \
                 ambiguous environment",
                KNOWN_APP_ENVS.join(", ")
            ))),
            Some(v) => AppEnv::parse(v.trim()),
        }
    }

    /// Reads the REAL process `APP_ENV` var and applies the fail-closed
    /// check. This IS the boot self-check — call it directly, or via
    /// `Config::load` (which calls it first, before touching any file),
    /// before any other startup work.
    pub fn from_env() -> Result<AppEnv, ConfigError> {
        AppEnv::from_value(std::env::var("APP_ENV").ok().as_deref())
    }
}

impl fmt::Display for AppEnv {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A config-loader failure: a bad/missing `APP_ENV`, an unreadable layer
/// file, or a missing required key. Always carries a human-readable message
/// naming what was wrong and (where applicable) the valid set — "loud
/// failure, never a silent no-op" (deploy-environment-design.md §3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

// ── typed values + the merged config ─────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
enum ConfigValue {
    Str(String),
    Int(i64),
    Bool(bool),
}

/// The fully layered, merged config for one boot: `app_env()` plus typed
/// access (`get_string` / `get_i64` / `get_bool`, and the `require_*`
/// fail-loud variants) over the merged key/value set.
#[derive(Debug, Clone)]
pub struct Config {
    app_env: AppEnv,
    values: BTreeMap<String, ConfigValue>,
}

impl Config {
    /// The active environment this config was loaded for (i.e. the result of
    /// the fail-closed `APP_ENV` boot check that ran before any layer file
    /// was even opened).
    pub fn app_env(&self) -> AppEnv {
        self.app_env
    }

    /// Loads the full layer stack rooted at `root`, after first running the
    /// fail-closed `APP_ENV` boot check (`AppEnv::from_env`). `Err` here
    /// means "refuse to boot" — the caller MUST NOT proceed to serve traffic.
    pub fn load(root: &Path) -> Result<Config, ConfigError> {
        let app_env = AppEnv::from_env()?; // <- the fail-closed check, first, always.
        let overrides: BTreeMap<String, String> = std::env::vars().collect();
        Config::load_for_env(root, app_env, &overrides)
    }

    /// Same as `load`, but with the active environment and the override map
    /// injected rather than read from real process state — this is what
    /// makes the layer-order + precedence behavior unit-testable without
    /// mutating (and racing on) `std::env` across parallel test threads.
    fn load_for_env(
        root: &Path,
        app_env: AppEnv,
        overrides: &BTreeMap<String, String>,
    ) -> Result<Config, ConfigError> {
        let mut values: BTreeMap<String, ConfigValue> = BTreeMap::new();
        let config_dir = root.join("config");
        let layers = [
            config_dir.join("defaults.toml"),
            config_dir.join("base.toml"),
            config_dir.join(format!("{}.toml", app_env.as_str())),
            config_dir.join("local.toml"),
        ];
        for layer_path in &layers {
            if let Some(text) = read_optional(layer_path)? {
                for (k, v) in parse_layer(&text) {
                    values.insert(k, v);
                }
            }
        }
        apply_env_overrides(&mut values, overrides);
        Ok(Config { app_env, values })
    }

    pub fn get_string(&self, key: &str) -> Option<String> {
        match self.values.get(key)? {
            ConfigValue::Str(s) => Some(s.clone()),
            ConfigValue::Int(i) => Some(i.to_string()),
            ConfigValue::Bool(b) => Some(b.to_string()),
        }
    }

    pub fn get_i64(&self, key: &str) -> Option<i64> {
        match self.values.get(key)? {
            ConfigValue::Int(i) => Some(*i),
            ConfigValue::Str(s) => s.parse().ok(),
            ConfigValue::Bool(_) => None,
        }
    }

    pub fn get_bool(&self, key: &str) -> Option<bool> {
        match self.values.get(key)? {
            ConfigValue::Bool(b) => Some(*b),
            ConfigValue::Str(s) => match s.as_str() {
                "true" => Some(true),
                "false" => Some(false),
                _ => None,
            },
            ConfigValue::Int(_) => None,
        }
    }

    /// Fail-loud variant: `Err` (never a silent default) when the key is
    /// absent or not string-shaped — for config a project truly cannot boot
    /// without (a database URL, a signing secret's *path*, ...).
    pub fn require_string(&self, key: &str) -> Result<String, ConfigError> {
        self.get_string(key)
            .ok_or_else(|| ConfigError(format!("required config key {key:?} is not set")))
    }

    pub fn require_i64(&self, key: &str) -> Result<i64, ConfigError> {
        self.get_i64(key)
            .ok_or_else(|| ConfigError(format!("required config key {key:?} is not a valid integer")))
    }

    pub fn require_bool(&self, key: &str) -> Result<bool, ConfigError> {
        self.get_bool(key)
            .ok_or_else(|| ConfigError(format!("required config key {key:?} is not a valid boolean")))
    }
}

fn read_optional(path: &Path) -> Result<Option<String>, ConfigError> {
    match fs::read_to_string(path) {
        Ok(s) => Ok(Some(s)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(ConfigError(format!("{}: {e}", path.display()))),
    }
}

/// Highest-priority layer: process environment variables. A var overrides
/// config key `foo_bar` when `FOO_BAR` (the UPPER_SNAKE_CASE of the key) is
/// set in `env`. Only keys ALREADY present from a lower layer are eligible —
/// this loader does not invent new config keys out of arbitrary ambient
/// process env, only overrides declared ones (keeps the merged key set
/// predictable from the layer files alone).
fn apply_env_overrides(values: &mut BTreeMap<String, ConfigValue>, env: &BTreeMap<String, String>) {
    let keys: Vec<String> = values.keys().cloned().collect();
    for key in keys {
        let env_key = key.to_uppercase();
        if let Some(raw) = env.get(&env_key) {
            values.insert(key, infer_value(raw));
        }
    }
}

fn infer_value(raw: &str) -> ConfigValue {
    match raw {
        "true" => ConfigValue::Bool(true),
        "false" => ConfigValue::Bool(false),
        _ => match raw.parse::<i64>() {
            Ok(i) => ConfigValue::Int(i),
            Err(_) => ConfigValue::Str(raw.to_string()),
        },
    }
}

// ── minimal std-only TOML-subset reader ──────────────────────────────────────
//
// Scoped to exactly the shape a flat env-profile layer needs: `key = value`
// lines, `#`-comments (whole-line or trailing), blank lines, and string /
// integer / boolean scalar values. No nested tables — each layer file IS one
// flat profile, so `[section]` headers are not part of the contract; a line
// that starts with `[` is skipped rather than erroring, so an app that
// migrates to a richer TOML crate later does not need to strip section
// headers from files it already committed as plain scalars. Malformed lines
// are skipped (not erroring the whole layer) — the same forgiving-parse
// posture as version_report.rs's `vendor.toml`/`vendor.lock` readers, so one
// bad line degrades a single key, never the whole config layer.

fn parse_layer(text: &str) -> BTreeMap<String, ConfigValue> {
    let mut out = BTreeMap::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with('[') {
            continue;
        }
        if let Some((key, value)) = split_kv(line) {
            if let Some(v) = parse_value(value) {
                out.insert(key.to_string(), v);
            }
        }
    }
    out
}

/// Split a `key = value` line (value may carry a trailing `# comment`).
fn split_kv(line: &str) -> Option<(&str, &str)> {
    let eq = line.find('=')?;
    let key = line[..eq].trim();
    let value = line[eq + 1..].trim();
    if key.is_empty() {
        return None;
    }
    Some((key, value))
}

/// Parse a scalar TOML-ish value: a quoted string, `true`/`false`, or an
/// integer. A trailing `# comment` outside of a quoted string is stripped
/// first. Returns `None` for anything else (unsupported shape — the caller
/// skips the key rather than erroring the layer).
fn parse_value(value: &str) -> Option<ConfigValue> {
    let stripped = strip_trailing_comment(value);
    let v = stripped.trim();
    if v.len() >= 2 && v.starts_with('"') && v.ends_with('"') {
        return Some(ConfigValue::Str(v[1..v.len() - 1].to_string()));
    }
    match v {
        "true" => return Some(ConfigValue::Bool(true)),
        "false" => return Some(ConfigValue::Bool(false)),
        _ => {}
    }
    if let Ok(i) = v.parse::<i64>() {
        return Some(ConfigValue::Int(i));
    }
    None
}

/// Strip a trailing `# ...` comment that is NOT inside a quoted string.
fn strip_trailing_comment(value: &str) -> &str {
    let mut in_quotes = false;
    for (i, ch) in value.char_indices() {
        match ch {
            '"' => in_quotes = !in_quotes,
            '#' if !in_quotes => return &value[..i],
            _ => {}
        }
    }
    value
}

// ── tests ─────────────────────────────────────────────────────────────────
//
// Run with: rustc --edition 2021 --test config_loader.rs -o /tmp/config_loader_test
// && /tmp/config_loader_test
//
// No env-var mutation in any test (see `from_value` / `load_for_env` doc
// comments above) — every test drives the module through explicit
// `Option<&str>` / `BTreeMap` inputs, so the whole suite is safe to run with
// the default parallel test harness.

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    // ---- AppEnv::from_value: the fail-closed boot check ----

    #[test]
    fn unset_app_env_fails_closed() {
        let err = AppEnv::from_value(None).unwrap_err();
        assert!(err.0.contains("not set"), "{}", err.0);
        assert!(err.0.contains("local"), "{}", err.0);
        assert!(err.0.contains("production"), "{}", err.0);
    }

    #[test]
    fn empty_app_env_fails_closed() {
        let err = AppEnv::from_value(Some("")).unwrap_err();
        assert!(err.0.contains("empty"), "{}", err.0);
        let err2 = AppEnv::from_value(Some("   ")).unwrap_err();
        assert!(err2.0.contains("empty"), "{}", err2.0);
    }

    #[test]
    fn unrecognized_app_env_fails_closed() {
        let err = AppEnv::from_value(Some("staging")).unwrap_err();
        assert!(err.0.contains("staging"), "{}", err.0);
        assert!(err.0.contains("local"), "{}", err.0);
        assert!(err.0.contains("dev"), "{}", err.0);
        assert!(err.0.contains("beta"), "{}", err.0);
        assert!(err.0.contains("production"), "{}", err.0);
    }

    #[test]
    fn known_app_envs_parse_ok() {
        assert_eq!(AppEnv::from_value(Some("local")).unwrap(), AppEnv::Local);
        assert_eq!(AppEnv::from_value(Some("dev")).unwrap(), AppEnv::Dev);
        assert_eq!(AppEnv::from_value(Some("beta")).unwrap(), AppEnv::Beta);
        assert_eq!(AppEnv::from_value(Some("production")).unwrap(), AppEnv::Production);
        // whitespace is trimmed, not treated as part of the value.
        assert_eq!(AppEnv::from_value(Some("  dev  ")).unwrap(), AppEnv::Dev);
    }

    #[test]
    fn app_env_display_round_trips_as_str() {
        for name in KNOWN_APP_ENVS {
            let env = AppEnv::parse(name).unwrap();
            assert_eq!(env.as_str(), name);
            assert_eq!(format!("{env}"), name);
        }
    }

    // ---- parse_layer: the minimal TOML-subset reader ----

    #[test]
    fn parse_layer_reads_string_int_bool() {
        let text = "app_name = \"widget\"\nport = 8080\ndebug = true\nstrict = false\n";
        let parsed = parse_layer(text);
        assert_eq!(parsed.get("app_name"), Some(&ConfigValue::Str("widget".into())));
        assert_eq!(parsed.get("port"), Some(&ConfigValue::Int(8080)));
        assert_eq!(parsed.get("debug"), Some(&ConfigValue::Bool(true)));
        assert_eq!(parsed.get("strict"), Some(&ConfigValue::Bool(false)));
    }

    #[test]
    fn parse_layer_skips_comments_blanks_and_sections() {
        let text = "\n# a full-line comment\nport = 8080   # trailing comment\n\n[some_section]\nignored_under_section = 1\n";
        let parsed = parse_layer(text);
        assert_eq!(parsed.get("port"), Some(&ConfigValue::Int(8080)));
        // Section headers are skipped, not erroring; a line found immediately
        // after one is still parsed as a flat key (no nested-table support).
        assert_eq!(parsed.get("ignored_under_section"), Some(&ConfigValue::Int(1)));
    }

    #[test]
    fn parse_layer_skips_malformed_lines_without_panicking() {
        let text = "not_a_kv_line\n= missing_key\nport = 8080\n";
        let parsed = parse_layer(text);
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed.get("port"), Some(&ConfigValue::Int(8080)));
    }

    #[test]
    fn strip_trailing_comment_respects_quotes() {
        assert_eq!(strip_trailing_comment("\"a#b\"  # real comment"), "\"a#b\"  ");
        assert_eq!(strip_trailing_comment("8080"), "8080");
    }

    // ---- Config::load_for_env: layer order + precedence ----

    struct TempRoot {
        root: PathBuf,
    }

    impl TempRoot {
        fn new() -> Self {
            let mut root = std::env::temp_dir();
            root.push(format!(
                "grimoire-config-loader-test-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir_all(root.join("config")).unwrap();
            TempRoot { root }
        }

        fn write(&self, rel: &str, contents: &str) {
            let p = self.root.join(rel);
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(p, contents).unwrap();
        }

        fn root(&self) -> &Path {
            &self.root
        }
    }

    impl Drop for TempRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn load_with_no_layer_files_succeeds_empty() {
        // Absence of every (optional) layer file is NOT a boot failure — only
        // a bad/missing APP_ENV is (deploy-environment-design.md §1's
        // absence-as-default posture, extended to the loader's own files).
        let t = TempRoot::new();
        let cfg = Config::load_for_env(t.root(), AppEnv::Dev, &BTreeMap::new()).unwrap();
        assert_eq!(cfg.app_env(), AppEnv::Dev);
        assert_eq!(cfg.get_string("port"), None);
    }

    #[test]
    fn layer_precedence_defaults_base_env_local() {
        let t = TempRoot::new();
        t.write("config/defaults.toml", "port = 1000\nname = \"defaults\"\nonly_defaults = true\n");
        t.write("config/base.toml", "port = 2000\nname = \"base\"\n");
        t.write("config/dev.toml", "port = 3000\nname = \"dev\"\n");
        t.write("config/local.toml", "port = 4000\n");
        let cfg = Config::load_for_env(t.root(), AppEnv::Dev, &BTreeMap::new()).unwrap();

        // local.toml wins for `port` (highest file-layer priority).
        assert_eq!(cfg.get_i64("port"), Some(4000));
        // dev.toml wins for `name` (local.toml doesn't declare it).
        assert_eq!(cfg.get_string("name"), Some("dev".to_string()));
        // A key only defaults.toml declares survives every later layer.
        assert_eq!(cfg.get_bool("only_defaults"), Some(true));
    }

    #[test]
    fn env_var_override_wins_over_every_file_layer() {
        let t = TempRoot::new();
        t.write("config/defaults.toml", "port = 1000\n");
        t.write("config/local.toml", "port = 4000\n");
        let mut overrides = BTreeMap::new();
        overrides.insert("PORT".to_string(), "9999".to_string());
        let cfg = Config::load_for_env(t.root(), AppEnv::Dev, &overrides).unwrap();
        assert_eq!(cfg.get_i64("port"), Some(9999));
    }

    #[test]
    fn env_var_override_only_applies_to_already_declared_keys() {
        // An unrelated process env var never injects a NEW config key — only
        // overrides one a lower layer already declared.
        let t = TempRoot::new();
        t.write("config/defaults.toml", "port = 1000\n");
        let mut overrides = BTreeMap::new();
        overrides.insert("PATH".to_string(), "/usr/bin".to_string());
        overrides.insert("HOME".to_string(), "/home/x".to_string());
        let cfg = Config::load_for_env(t.root(), AppEnv::Dev, &overrides).unwrap();
        assert_eq!(cfg.get_i64("port"), Some(1000));
        assert_eq!(cfg.get_string("path"), None);
        assert_eq!(cfg.get_string("home"), None);
    }

    #[test]
    fn env_var_override_infers_bool_and_string_types() {
        let t = TempRoot::new();
        t.write("config/defaults.toml", "debug = false\nname = \"a\"\n");
        let mut overrides = BTreeMap::new();
        overrides.insert("DEBUG".to_string(), "true".to_string());
        overrides.insert("NAME".to_string(), "overridden".to_string());
        let cfg = Config::load_for_env(t.root(), AppEnv::Local, &overrides).unwrap();
        assert_eq!(cfg.get_bool("debug"), Some(true));
        assert_eq!(cfg.get_string("name"), Some("overridden".to_string()));
    }

    #[test]
    fn different_app_envs_select_different_layer_files() {
        let t = TempRoot::new();
        t.write("config/dev.toml", "target = \"dev\"\n");
        t.write("config/production.toml", "target = \"production\"\n");
        let dev_cfg = Config::load_for_env(t.root(), AppEnv::Dev, &BTreeMap::new()).unwrap();
        let prod_cfg = Config::load_for_env(t.root(), AppEnv::Production, &BTreeMap::new()).unwrap();
        assert_eq!(dev_cfg.get_string("target"), Some("dev".to_string()));
        assert_eq!(prod_cfg.get_string("target"), Some("production".to_string()));
    }

    #[test]
    fn unreadable_layer_directory_as_file_errors_loudly_not_silently() {
        // A layer path that exists but cannot be read as a file (here: it's a
        // directory) must surface a loud ConfigError, never silently skip.
        let t = TempRoot::new();
        fs::create_dir_all(t.root().join("config/base.toml")).unwrap();
        let err = Config::load_for_env(t.root(), AppEnv::Dev, &BTreeMap::new()).unwrap_err();
        assert!(err.0.contains("base.toml"), "{}", err.0);
    }

    // ---- typed access ----

    #[test]
    fn typed_getters_cross_convert_where_sensible() {
        let t = TempRoot::new();
        t.write("config/defaults.toml", "count = 42\nflag = true\nlabel = \"x\"\n");
        let cfg = Config::load_for_env(t.root(), AppEnv::Local, &BTreeMap::new()).unwrap();
        // int -> string is a sensible widen.
        assert_eq!(cfg.get_string("count"), Some("42".to_string()));
        // bool -> string is a sensible widen.
        assert_eq!(cfg.get_string("flag"), Some("true".to_string()));
        // string -> int / bool only succeeds when the string itself parses.
        assert_eq!(cfg.get_i64("label"), None);
        assert_eq!(cfg.get_bool("label"), None);
    }

    #[test]
    fn require_variants_fail_loud_on_missing_key() {
        let t = TempRoot::new();
        let cfg = Config::load_for_env(t.root(), AppEnv::Local, &BTreeMap::new()).unwrap();
        let err = cfg.require_string("database_url").unwrap_err();
        assert!(err.0.contains("database_url"), "{}", err.0);
        assert!(err.0.contains("required"), "{}", err.0);
    }

    #[test]
    fn require_variants_succeed_on_present_key() {
        let t = TempRoot::new();
        t.write("config/defaults.toml", "database_url = \"postgres://x\"\nport = 8080\nready = true\n");
        let cfg = Config::load_for_env(t.root(), AppEnv::Local, &BTreeMap::new()).unwrap();
        assert_eq!(cfg.require_string("database_url").unwrap(), "postgres://x");
        assert_eq!(cfg.require_i64("port").unwrap(), 8080);
        assert_eq!(cfg.require_bool("ready").unwrap(), true);
    }
}
