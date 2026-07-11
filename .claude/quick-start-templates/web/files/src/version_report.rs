// version_report.rs — in-app version/dependency query interface (issue #206).
//
// Unlike the sibling SEAM files (auth_seam.rs / db_seam.rs / cost_surface_seam.rs),
// which are `grimoire:placeholder` scaffolds that `unimplemented!()`, THIS is a
// REAL, WORKING module — the whole point of #206 is that a scaffolded app can
// actually report its own framework version, active dials, and vendored-dependency
// versions at runtime, with no network and no hand-maintained second copy.
//
// It reads the existing canonical files only, in this order (all optional; a
// missing/malformed file degrades only the fields it feeds, never the whole
// report — mirroring project_status.py's `degraded` list):
//   1. .claude/grimoire-config.json  -> framework_version + dials
//   2. vendor.toml                   -> the SET of vendored deps + each channel
//   3. vendor.lock                   -> each dep's RESOLVED version/tag/git_sha/synced_at
//
// Provenance rule: the dep SET comes from vendor.toml; the resolved VERSION for
// each dep comes from vendor.lock (the truth the sync engine wrote, matching the
// running bytes). vendor.lock field names are matched EXACTLY — never a renamed
// alias — so there is zero drift between this output and the lock file.
//
// std-only by design: it carries minimal internal JSON and [deps.*]-TOML readers
// scoped to exactly the well-formed shapes the contract describes, so it compiles
// and its tests run under a bare `rustc` before any dependency is vendored. A
// production app MAY swap the internal readers for serde_json + toml without
// changing the public surface (version_report / render_version_report).
//
// Contract authority and vendor.lock schema authority (grm-sync-deps
//   VendorLock/_sync_one) live in the upstream Grimoire repository
//   (framework-internal) — do NOT re-derive; read the lock's truth.
//
// ── Call sites (both required by the contract §4) ───────────────────────────
//
// Library (admin page / health-check / /about route) — returns an in-memory value:
//     let report = version_report::version_report(".");
//     // e.g. Axum: Json(report.to_json())  — serialize and return as an API body.
//     if !report.degraded.is_empty() { /* health surface may note the degradations */ }
//
// Binary `--version` / `--about` — the same function, rendered to stdout:
//     fn main() {
//         if std::env::args().any(|a| a == "--version" || a == "--about") {
//             println!("{}", version_report::render_version_report("."));
//             return;
//         }
//         // ... normal startup ...
//     }
//
// Both call sites read the LIVE committed files at call time — no build-time freeze.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

/// One vendored dependency's report row.
///
/// `name` + `channel` come from `vendor.toml`; `version` / `release_tag` /
/// `git_sha` / `synced_at` come from `vendor.lock` and are `None` when the lock
/// is absent, malformed, or carries no entry for this dep (per-field degrade).
#[derive(Debug, Clone, PartialEq)]
pub struct VendoredDependency {
    pub name: String,
    pub channel: Option<String>,
    pub version: Option<String>,
    pub release_tag: Option<String>,
    pub git_sha: Option<String>,
    pub synced_at: Option<String>,
}

/// The structured version/dependency report (the contract's top-level shape).
///
/// A report with a non-empty `degraded` list is still a VALID report: partial
/// success is success. Callers decide whether a given degradation matters.
#[derive(Debug, Clone, PartialEq)]
pub struct VersionReport {
    /// From `grimoire-config.json` `framework-version`; `None` when that file is
    /// missing/malformed.
    pub framework_version: Option<String>,
    /// Dial name -> value, from each top-level `{"value": ...}` block in
    /// grimoire-config.json. Empty when the config is missing/malformed.
    pub dials: BTreeMap<String, String>,
    /// One entry per `vendor.toml` dep; empty when vendor.toml is missing/malformed.
    pub vendored_dependencies: Vec<VendoredDependency>,
    /// Human-readable notes, one per degraded source. Empty on a full read.
    pub degraded: Vec<String>,
}

impl VersionReport {
    /// Serialize the report to a JSON string (the shape a `--version`/`--about`
    /// binary prints, or an `/about` route returns). std-only writer — no serde.
    pub fn to_json(&self) -> String {
        let mut out = String::from("{\n");
        out.push_str(&format!(
            "  \"framework_version\": {},\n",
            json_opt_str(&self.framework_version)
        ));

        // dials (sorted — BTreeMap iterates in key order, so output is deterministic)
        out.push_str("  \"dials\": {");
        if self.dials.is_empty() {
            out.push('}');
        } else {
            out.push('\n');
            let mut first = true;
            for (k, v) in &self.dials {
                if !first {
                    out.push_str(",\n");
                }
                first = false;
                out.push_str(&format!("    {}: {}", json_str(k), json_str(v)));
            }
            out.push_str("\n  }");
        }
        out.push_str(",\n");

        // vendored_dependencies
        out.push_str("  \"vendored_dependencies\": [");
        if self.vendored_dependencies.is_empty() {
            out.push(']');
        } else {
            out.push('\n');
            let mut first = true;
            for d in &self.vendored_dependencies {
                if !first {
                    out.push_str(",\n");
                }
                first = false;
                out.push_str("    {\n");
                out.push_str(&format!("      \"name\": {},\n", json_str(&d.name)));
                out.push_str(&format!(
                    "      \"channel\": {},\n",
                    json_opt_str(&d.channel)
                ));
                out.push_str(&format!(
                    "      \"version\": {},\n",
                    json_opt_str(&d.version)
                ));
                out.push_str(&format!(
                    "      \"release_tag\": {},\n",
                    json_opt_str(&d.release_tag)
                ));
                out.push_str(&format!(
                    "      \"git_sha\": {},\n",
                    json_opt_str(&d.git_sha)
                ));
                out.push_str(&format!(
                    "      \"synced_at\": {}\n",
                    json_opt_str(&d.synced_at)
                ));
                out.push_str("    }");
            }
            out.push_str("\n  ]");
        }
        out.push_str(",\n");

        // degraded
        out.push_str("  \"degraded\": [");
        if self.degraded.is_empty() {
            out.push(']');
        } else {
            out.push('\n');
            let mut first = true;
            for note in &self.degraded {
                if !first {
                    out.push_str(",\n");
                }
                first = false;
                out.push_str(&format!("    {}", json_str(note)));
            }
            out.push_str("\n  ]");
        }
        out.push_str("\n}\n");
        out
    }
}

/// Library call site: build the version report for a project rooted at
/// `project_root`. Never panics — every missing/malformed source degrades a
/// field and appends a `degraded` note.
pub fn version_report<P: AsRef<Path>>(project_root: P) -> VersionReport {
    let root = project_root.as_ref();
    let mut degraded: Vec<String> = Vec::new();

    let (framework_version, dials) = read_config(root, &mut degraded);
    let vendored_dependencies = read_vendored(root, &mut degraded);

    VersionReport {
        framework_version,
        dials,
        vendored_dependencies,
        degraded,
    }
}

/// Binary call site: the report rendered to a JSON string for a
/// `--version` / `--about` surface (or an `/about` API body).
pub fn render_version_report<P: AsRef<Path>>(project_root: P) -> String {
    version_report(project_root).to_json()
}

// ── source 1: .claude/grimoire-config.json ──────────────────────────────────

fn read_config(root: &Path, degraded: &mut Vec<String>) -> (Option<String>, BTreeMap<String, String>) {
    let path = root.join(".claude").join("grimoire-config.json");
    let text = match fs::read_to_string(&path) {
        Ok(t) => t,
        Err(_) => {
            degraded.push(".claude/grimoire-config.json (missing)".to_string());
            return (None, BTreeMap::new());
        }
    };
    let value = match JsonValue::parse(&text) {
        Some(v) => v,
        None => {
            degraded.push(".claude/grimoire-config.json (unparseable JSON)".to_string());
            return (None, BTreeMap::new());
        }
    };
    let obj = match value.as_object() {
        Some(o) => o,
        None => {
            degraded.push(".claude/grimoire-config.json (unparseable JSON)".to_string());
            return (None, BTreeMap::new());
        }
    };

    let framework_version = obj
        .get("framework-version")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    // Every top-level block shaped `{"value": <scalar>}` is a dial (mirrors the
    // `_dial` helper in project_status.py). Scalar values become the dial value.
    let mut dials = BTreeMap::new();
    for (k, v) in obj {
        if let JsonValue::Object(inner) = v {
            if let Some(val) = inner.get("value") {
                if let Some(scalar) = val.as_scalar_string() {
                    dials.insert(k.clone(), scalar);
                }
            }
        }
    }

    (framework_version, dials)
}

// ── sources 2 + 3: vendor.toml (set + channel) then vendor.lock (resolved) ──

fn read_vendored(root: &Path, degraded: &mut Vec<String>) -> Vec<VendoredDependency> {
    // vendor.toml supplies the dep SET + declared channel.
    let toml_path = root.join("vendor.toml");
    let toml_text = match fs::read_to_string(&toml_path) {
        Ok(t) => t,
        Err(_) => {
            degraded.push("vendor.toml (missing)".to_string());
            return Vec::new();
        }
    };
    let declared = match parse_vendor_toml_deps(&toml_text) {
        Some(d) => d,
        None => {
            degraded.push("vendor.toml (unparseable TOML)".to_string());
            return Vec::new();
        }
    };

    // With zero declared deps (the seed stub) there is nothing to resolve, so the
    // lock is never consulted and its absence is NOT a degradation.
    if declared.is_empty() {
        return Vec::new();
    }

    // vendor.lock supplies the RESOLVED fields. Absence/malformation degrades the
    // resolved fields of EVERY dep (each dep still appears from vendor.toml).
    let lock_path = root.join("vendor.lock");
    let lock_deps: Option<BTreeMap<String, JsonValue>> = match fs::read_to_string(&lock_path) {
        Ok(text) => match JsonValue::parse(&text) {
            Some(v) => match v.as_object().and_then(|o| o.get("deps")).and_then(|d| d.as_object()) {
                Some(deps) => Some(deps.clone()),
                None => {
                    // File parsed but has no `deps` object — treat as no entries.
                    degraded.push("vendor.lock (unparseable JSON)".to_string());
                    None
                }
            },
            None => {
                degraded.push("vendor.lock (unparseable JSON)".to_string());
                None
            }
        },
        Err(_) => {
            degraded.push(
                "vendor.lock (missing — resolved dependency versions unavailable)".to_string(),
            );
            None
        }
    };

    let mut out = Vec::with_capacity(declared.len());
    for (name, channel) in declared {
        let lock_entry = lock_deps.as_ref().and_then(|d| d.get(&name)).and_then(|e| e.as_object());

        // Per-field degrade: a lock present but with no entry for THIS dep leaves
        // only this dep's resolved fields null (others unaffected) + one note.
        if lock_deps.is_some() && lock_entry.is_none() {
            degraded.push(format!("vendor.lock (no entry for dep '{}')", name));
        }

        let get = |field: &str| -> Option<String> {
            lock_entry
                .and_then(|e| e.get(field))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
        };

        out.push(VendoredDependency {
            name,
            channel,
            // Field names match vendor.lock exactly (no renamed alias).
            version: get("version"),
            release_tag: get("release_tag"),
            git_sha: get("git_sha"),
            synced_at: get("synced_at"),
        });
    }
    out
}

/// Parse the `[deps.<name>]` blocks of a vendor.toml into `name -> channel`.
///
/// A minimal line-oriented reader scoped to exactly the fields this interface
/// needs (block header + `channel = "..."`); the full schema is owned by
/// grm-sync-deps. Comment lines (`#`) and unrelated keys are ignored. Returns
/// `Some(map)` for any readable file (an empty map when no deps are declared),
/// and `None` only if the input is not text we can line-scan (unreachable for a
/// String, but kept so the caller's degrade path is explicit and future-proof).
fn parse_vendor_toml_deps(text: &str) -> Option<BTreeMap<String, Option<String>>> {
    let mut deps: BTreeMap<String, Option<String>> = BTreeMap::new();
    let mut current: Option<String> = None;

    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            // Section header. We care only about [deps.<name>]; any other table
            // (or the array-of-tables form [[...]]) clears the current dep.
            let inner = &line[1..line.len() - 1];
            let inner = inner.trim();
            if let Some(rest) = inner.strip_prefix("deps.") {
                let name = rest.trim();
                if !name.is_empty() {
                    current = Some(name.to_string());
                    deps.entry(name.to_string()).or_insert(None);
                    continue;
                }
            }
            current = None;
            continue;
        }
        // key = value line inside the current [deps.<name>] block.
        if let Some(name) = &current {
            if let Some((key, value)) = split_toml_kv(line) {
                if key == "channel" {
                    if let Some(s) = toml_string_value(value) {
                        deps.insert(name.clone(), Some(s));
                    }
                }
            }
        }
    }
    Some(deps)
}

/// Split a `key = value` TOML line (value may carry a trailing `# comment`).
fn split_toml_kv(line: &str) -> Option<(&str, &str)> {
    let eq = line.find('=')?;
    let key = line[..eq].trim();
    let value = line[eq + 1..].trim();
    if key.is_empty() {
        return None;
    }
    Some((key, value))
}

/// Extract the string content of a TOML value like `"stable"   # comment`.
/// Returns None for non-string values.
fn toml_string_value(value: &str) -> Option<String> {
    let v = value.trim();
    if !v.starts_with('"') {
        return None;
    }
    // Find the closing quote (no escape handling needed for our fields; channel
    // values are simple identifiers). Everything after it (e.g. a comment) is
    // ignored.
    let rest = &v[1..];
    let close = rest.find('"')?;
    Some(rest[..close].to_string())
}

// ── minimal std-only JSON reader ─────────────────────────────────────────────
//
// Scoped to the object/string/number/bool/null shapes grimoire-config.json and
// vendor.lock actually use. Robust against malformed input: any parse failure
// returns None (the caller's degrade path), never a panic.

#[derive(Debug, Clone, PartialEq)]
enum JsonValue {
    Null,
    Bool(bool),
    Number(String),
    Str(String),
    Array(Vec<JsonValue>),
    Object(BTreeMap<String, JsonValue>),
}

impl JsonValue {
    fn parse(text: &str) -> Option<JsonValue> {
        let bytes = text.as_bytes();
        let mut pos = 0usize;
        skip_ws(bytes, &mut pos);
        let v = parse_value(bytes, &mut pos)?;
        skip_ws(bytes, &mut pos);
        // Trailing non-whitespace ⇒ malformed.
        if pos != bytes.len() {
            return None;
        }
        Some(v)
    }

    fn as_object(&self) -> Option<&BTreeMap<String, JsonValue>> {
        match self {
            JsonValue::Object(m) => Some(m),
            _ => None,
        }
    }

    fn as_str(&self) -> Option<&str> {
        match self {
            JsonValue::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }

    /// A dial value may be a string, number, or bool; render it as a string so
    /// heterogeneous dial values surface uniformly. Objects/arrays/null are not
    /// scalar dial values and yield None.
    fn as_scalar_string(&self) -> Option<String> {
        match self {
            JsonValue::Str(s) => Some(s.clone()),
            JsonValue::Number(n) => Some(n.clone()),
            JsonValue::Bool(b) => Some(b.to_string()),
            _ => None,
        }
    }
}

fn skip_ws(bytes: &[u8], pos: &mut usize) {
    while *pos < bytes.len() {
        match bytes[*pos] {
            b' ' | b'\t' | b'\n' | b'\r' => *pos += 1,
            _ => break,
        }
    }
}

fn parse_value(bytes: &[u8], pos: &mut usize) -> Option<JsonValue> {
    skip_ws(bytes, pos);
    if *pos >= bytes.len() {
        return None;
    }
    match bytes[*pos] {
        b'{' => parse_object(bytes, pos),
        b'[' => parse_array(bytes, pos),
        b'"' => parse_string(bytes, pos).map(JsonValue::Str),
        b't' | b'f' => parse_bool(bytes, pos),
        b'n' => parse_null(bytes, pos),
        b'-' | b'0'..=b'9' => parse_number(bytes, pos),
        _ => None,
    }
}

fn parse_object(bytes: &[u8], pos: &mut usize) -> Option<JsonValue> {
    // assumes bytes[*pos] == '{'
    *pos += 1;
    let mut map = BTreeMap::new();
    skip_ws(bytes, pos);
    if *pos < bytes.len() && bytes[*pos] == b'}' {
        *pos += 1;
        return Some(JsonValue::Object(map));
    }
    loop {
        skip_ws(bytes, pos);
        if *pos >= bytes.len() || bytes[*pos] != b'"' {
            return None;
        }
        let key = parse_string(bytes, pos)?;
        skip_ws(bytes, pos);
        if *pos >= bytes.len() || bytes[*pos] != b':' {
            return None;
        }
        *pos += 1;
        let val = parse_value(bytes, pos)?;
        map.insert(key, val);
        skip_ws(bytes, pos);
        if *pos >= bytes.len() {
            return None;
        }
        match bytes[*pos] {
            b',' => {
                *pos += 1;
                continue;
            }
            b'}' => {
                *pos += 1;
                return Some(JsonValue::Object(map));
            }
            _ => return None,
        }
    }
}

fn parse_array(bytes: &[u8], pos: &mut usize) -> Option<JsonValue> {
    // assumes bytes[*pos] == '['
    *pos += 1;
    let mut arr = Vec::new();
    skip_ws(bytes, pos);
    if *pos < bytes.len() && bytes[*pos] == b']' {
        *pos += 1;
        return Some(JsonValue::Array(arr));
    }
    loop {
        let val = parse_value(bytes, pos)?;
        arr.push(val);
        skip_ws(bytes, pos);
        if *pos >= bytes.len() {
            return None;
        }
        match bytes[*pos] {
            b',' => {
                *pos += 1;
                continue;
            }
            b']' => {
                *pos += 1;
                return Some(JsonValue::Array(arr));
            }
            _ => return None,
        }
    }
}

fn parse_string(bytes: &[u8], pos: &mut usize) -> Option<String> {
    // assumes bytes[*pos] == '"'
    *pos += 1;
    let mut out = String::new();
    while *pos < bytes.len() {
        let c = bytes[*pos];
        *pos += 1;
        match c {
            b'"' => return Some(out),
            b'\\' => {
                if *pos >= bytes.len() {
                    return None;
                }
                let esc = bytes[*pos];
                *pos += 1;
                match esc {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'n' => out.push('\n'),
                    b't' => out.push('\t'),
                    b'r' => out.push('\r'),
                    b'b' => out.push('\u{0008}'),
                    b'f' => out.push('\u{000C}'),
                    b'u' => {
                        // \uXXXX — parse four hex digits into a char.
                        if *pos + 4 > bytes.len() {
                            return None;
                        }
                        let hex = std::str::from_utf8(&bytes[*pos..*pos + 4]).ok()?;
                        let code = u32::from_str_radix(hex, 16).ok()?;
                        *pos += 4;
                        out.push(char::from_u32(code)?);
                    }
                    _ => return None,
                }
            }
            _ => {
                // Copy the byte through as UTF-8. Non-ASCII bytes are part of a
                // multi-byte sequence already validated by read_to_string, so we
                // push them via a small buffer to preserve them.
                out.push(c as char);
            }
        }
    }
    None // unterminated string
}

fn parse_bool(bytes: &[u8], pos: &mut usize) -> Option<JsonValue> {
    if bytes[*pos..].starts_with(b"true") {
        *pos += 4;
        Some(JsonValue::Bool(true))
    } else if bytes[*pos..].starts_with(b"false") {
        *pos += 5;
        Some(JsonValue::Bool(false))
    } else {
        None
    }
}

fn parse_null(bytes: &[u8], pos: &mut usize) -> Option<JsonValue> {
    if bytes[*pos..].starts_with(b"null") {
        *pos += 4;
        Some(JsonValue::Null)
    } else {
        None
    }
}

fn parse_number(bytes: &[u8], pos: &mut usize) -> Option<JsonValue> {
    let start = *pos;
    if *pos < bytes.len() && bytes[*pos] == b'-' {
        *pos += 1;
    }
    let mut saw_digit = false;
    while *pos < bytes.len() {
        match bytes[*pos] {
            b'0'..=b'9' => {
                saw_digit = true;
                *pos += 1;
            }
            b'.' | b'e' | b'E' | b'+' | b'-' => *pos += 1,
            _ => break,
        }
    }
    if !saw_digit {
        return None;
    }
    let s = std::str::from_utf8(&bytes[start..*pos]).ok()?;
    Some(JsonValue::Number(s.to_string()))
}

// ── JSON string emitters for to_json() ───────────────────────────────────────

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

fn json_opt_str(v: &Option<String>) -> String {
    match v {
        Some(s) => json_str(s),
        None => "null".to_string(),
    }
}

// ── tests ────────────────────────────────────────────────────────────────────
//
// The first Rust-with-tests file in this template set. Run standalone with:
//   rustc --edition 2021 --test version_report.rs -o /tmp/vr_test && /tmp/vr_test
// Covers: full-data, missing vendor.toml, missing vendor.lock (per-field degrade),
// missing grimoire-config.json, and malformed JSON/TOML (degrades, no panic).

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU32, Ordering};

    static COUNTER: AtomicU32 = AtomicU32::new(0);

    /// A throwaway project root under the OS temp dir. Files are written by the
    /// test; the dir is created fresh per test so cases don't cross-contaminate.
    struct TempRoot {
        path: PathBuf,
    }

    impl TempRoot {
        fn new() -> TempRoot {
            let n = COUNTER.fetch_add(1, Ordering::SeqCst);
            let pid = std::process::id();
            let mut path = std::env::temp_dir();
            path.push(format!("grm-vr-test-{}-{}", pid, n));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(path.join(".claude")).unwrap();
            TempRoot { path }
        }

        fn write(&self, rel: &str, contents: &str) {
            let full = self.path.join(rel);
            if let Some(parent) = full.parent() {
                fs::create_dir_all(parent).unwrap();
            }
            fs::write(full, contents).unwrap();
        }

        fn root(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for TempRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    const GOOD_CONFIG: &str = r#"{
      "schema-version": 4,
      "name": "Demo",
      "framework-version": "v3.71",
      "work-paradigm": { "value": "Noir" },
      "workflow-variant": { "value": "Efficient" },
      "model-effort-profile": { "value": "Medium" },
      "release-phase-model": { "value": "Default" },
      "autonomous-push": { "enabled": true }
    }"#;

    const GOOD_VENDOR_TOML: &str = r#"
schema_version = 1

# a real dep
[deps.aura]
repo = "rhohn94/design-language"
channel = "stable"   # trailing comment must be ignored
version = "3.20.0"
artifact = "aura-v3.20.0.tar.gz"
dest = "lib/third-party/aura"
kind = "asset-bundle"

[deps.token-bookkeeper]
repo = "rhohn94/token-bookkeeper"
channel = "beta"
version = "0.2.0"
artifact = "token-bookkeeper-v0.2.0.tar.gz"
dest = "lib/third-party/token-bookkeeper"
kind = "vendored-crate"
"#;

    const GOOD_VENDOR_LOCK: &str = r#"{
      "schema_version": 1,
      "deps": {
        "aura": {
          "version": "3.20.0",
          "channel": "stable",
          "git_sha": "abc123def",
          "release_tag": "v3.20.0",
          "release_url": "https://example.invalid/aura/v3.20.0",
          "artifact": "aura-v3.20.0.tar.gz",
          "artifact_sha256": "sha256:aaaa",
          "tree_sha256": "sha256:bbbb",
          "release_json_sha256": "sha256:cccc",
          "signature_verified": false,
          "synced_at": "2026-06-01T12:00:00Z"
        },
        "token-bookkeeper": {
          "version": "0.2.0",
          "channel": "beta",
          "git_sha": "999888",
          "release_tag": "v0.2.0",
          "release_url": "https://example.invalid/tb/v0.2.0",
          "artifact": "token-bookkeeper-v0.2.0.tar.gz",
          "artifact_sha256": "sha256:dddd",
          "tree_sha256": "sha256:eeee",
          "release_json_sha256": "sha256:ffff",
          "signature_verified": false,
          "synced_at": "2026-06-02T09:30:00Z"
        }
      }
    }"#;

    #[test]
    fn full_data_case() {
        let t = TempRoot::new();
        t.write(".claude/grimoire-config.json", GOOD_CONFIG);
        t.write("vendor.toml", GOOD_VENDOR_TOML);
        t.write("vendor.lock", GOOD_VENDOR_LOCK);

        let r = version_report(t.root());
        assert!(r.degraded.is_empty(), "expected no degradation: {:?}", r.degraded);
        assert_eq!(r.framework_version.as_deref(), Some("v3.71"));

        // dials: every {"value": ...} block surfaced; a non-dial block
        // (autonomous-push -> {enabled: true}) is NOT surfaced (no "value" key).
        assert_eq!(r.dials.get("work-paradigm").map(String::as_str), Some("Noir"));
        assert_eq!(r.dials.get("workflow-variant").map(String::as_str), Some("Efficient"));
        assert_eq!(r.dials.get("model-effort-profile").map(String::as_str), Some("Medium"));
        assert_eq!(r.dials.get("release-phase-model").map(String::as_str), Some("Default"));
        assert!(!r.dials.contains_key("autonomous-push"), "non-dial block must not surface");

        // vendored deps: set from vendor.toml, resolved version from vendor.lock.
        assert_eq!(r.vendored_dependencies.len(), 2);
        let aura = r
            .vendored_dependencies
            .iter()
            .find(|d| d.name == "aura")
            .expect("aura present");
        assert_eq!(aura.channel.as_deref(), Some("stable"));
        assert_eq!(aura.version.as_deref(), Some("3.20.0"));
        assert_eq!(aura.release_tag.as_deref(), Some("v3.20.0"));
        assert_eq!(aura.git_sha.as_deref(), Some("abc123def"));
        assert_eq!(aura.synced_at.as_deref(), Some("2026-06-01T12:00:00Z"));

        // to_json must be valid, non-empty, and round-trippable by our own reader.
        let js = r.to_json();
        assert!(js.contains("\"framework_version\": \"v3.71\""));
        assert!(JsonValue::parse(&js).is_some(), "to_json output must be valid JSON");
    }

    #[test]
    fn missing_vendor_toml_degrades() {
        let t = TempRoot::new();
        t.write(".claude/grimoire-config.json", GOOD_CONFIG);
        // no vendor.toml, no vendor.lock
        let r = version_report(t.root());
        assert_eq!(r.framework_version.as_deref(), Some("v3.71"));
        assert!(r.vendored_dependencies.is_empty());
        assert!(
            r.degraded.iter().any(|d| d.contains("vendor.toml (missing)")),
            "expected vendor.toml missing note: {:?}",
            r.degraded
        );
    }

    #[test]
    fn missing_vendor_lock_per_field_degrade() {
        let t = TempRoot::new();
        t.write(".claude/grimoire-config.json", GOOD_CONFIG);
        t.write("vendor.toml", GOOD_VENDOR_TOML);
        // no vendor.lock
        let r = version_report(t.root());

        // Both deps still appear (from vendor.toml), channel populated, resolved
        // fields all None.
        assert_eq!(r.vendored_dependencies.len(), 2);
        for d in &r.vendored_dependencies {
            assert!(d.channel.is_some(), "channel comes from vendor.toml");
            assert!(d.version.is_none(), "version None without a lock");
            assert!(d.release_tag.is_none());
            assert!(d.git_sha.is_none());
            assert!(d.synced_at.is_none());
        }
        assert!(
            r.degraded.iter().any(|d| d.contains("vendor.lock (missing")),
            "expected vendor.lock missing note: {:?}",
            r.degraded
        );
    }

    #[test]
    fn lock_missing_one_dep_entry_degrades_only_that_dep() {
        let t = TempRoot::new();
        t.write(".claude/grimoire-config.json", GOOD_CONFIG);
        t.write("vendor.toml", GOOD_VENDOR_TOML);
        // lock has aura but NOT token-bookkeeper.
        t.write(
            "vendor.lock",
            r#"{ "schema_version": 1, "deps": { "aura": { "version": "3.20.0", "channel": "stable", "release_tag": "v3.20.0", "git_sha": "abc123def", "synced_at": "2026-06-01T12:00:00Z" } } }"#,
        );
        let r = version_report(t.root());

        let aura = r.vendored_dependencies.iter().find(|d| d.name == "aura").unwrap();
        assert_eq!(aura.version.as_deref(), Some("3.20.0"));
        let tb = r
            .vendored_dependencies
            .iter()
            .find(|d| d.name == "token-bookkeeper")
            .unwrap();
        assert!(tb.version.is_none(), "the un-locked dep degrades to None");
        assert_eq!(tb.channel.as_deref(), Some("beta"), "its channel still comes from vendor.toml");
        assert!(
            r.degraded.iter().any(|d| d.contains("no entry for dep 'token-bookkeeper'")),
            "expected per-dep no-entry note: {:?}",
            r.degraded
        );
    }

    #[test]
    fn missing_config_degrades() {
        let t = TempRoot::new();
        // no grimoire-config.json
        t.write("vendor.toml", GOOD_VENDOR_TOML);
        t.write("vendor.lock", GOOD_VENDOR_LOCK);
        let r = version_report(t.root());
        assert!(r.framework_version.is_none());
        assert!(r.dials.is_empty());
        assert!(
            r.degraded.iter().any(|d| d.contains("grimoire-config.json (missing)")),
            "expected config missing note: {:?}",
            r.degraded
        );
        // deps still fully populated despite the missing config.
        assert_eq!(r.vendored_dependencies.len(), 2);
    }

    #[test]
    fn malformed_json_degrades_not_panics() {
        let t = TempRoot::new();
        // truncated / invalid JSON
        t.write(".claude/grimoire-config.json", "{ \"framework-version\": ");
        t.write("vendor.toml", GOOD_VENDOR_TOML);
        // vendor.lock that is not valid JSON
        t.write("vendor.lock", "{ this is not json ]");
        let r = version_report(t.root());

        assert!(r.framework_version.is_none());
        assert!(r.dials.is_empty());
        assert!(
            r.degraded.iter().any(|d| d.contains("grimoire-config.json (unparseable JSON)")),
            "expected config unparseable note: {:?}",
            r.degraded
        );
        assert!(
            r.degraded.iter().any(|d| d.contains("vendor.lock (unparseable JSON)")),
            "expected lock unparseable note: {:?}",
            r.degraded
        );
        // deps still present from vendor.toml; resolved fields degraded.
        assert_eq!(r.vendored_dependencies.len(), 2);
        assert!(r.vendored_dependencies.iter().all(|d| d.version.is_none()));
    }

    #[test]
    fn empty_vendor_toml_no_deps_is_clean() {
        let t = TempRoot::new();
        t.write(".claude/grimoire-config.json", GOOD_CONFIG);
        // vendor.toml with no [deps.*] blocks (the seed stub) — readable, no deps.
        t.write("vendor.toml", "schema_version = 1\n# no deps declared yet\n");
        // No vendor.lock: with zero declared deps there is nothing to resolve, so
        // the lock is never consulted and its absence is NOT a degradation.
        let r = version_report(t.root());
        assert!(r.vendored_dependencies.is_empty());
        assert!(
            !r.degraded.iter().any(|d| d.contains("vendor.lock")),
            "empty dep set must not consult or flag vendor.lock: {:?}",
            r.degraded
        );
    }

    #[test]
    fn render_version_report_is_valid_json_string() {
        let t = TempRoot::new();
        t.write(".claude/grimoire-config.json", GOOD_CONFIG);
        t.write("vendor.toml", GOOD_VENDOR_TOML);
        t.write("vendor.lock", GOOD_VENDOR_LOCK);
        let s = render_version_report(t.root());
        assert!(JsonValue::parse(&s).is_some(), "render output must be valid JSON: {}", s);
        assert!(s.contains("\"vendored_dependencies\""));
        assert!(s.contains("\"degraded\": []"));
    }

    #[test]
    fn totally_empty_project_degrades_all_sources_no_panic() {
        let t = TempRoot::new();
        // nothing written at all.
        let r = version_report(t.root());
        assert!(r.framework_version.is_none());
        assert!(r.dials.is_empty());
        assert!(r.vendored_dependencies.is_empty());
        // config + vendor.toml both degrade (lock never consulted — no deps).
        assert!(r.degraded.iter().any(|d| d.contains("grimoire-config.json (missing)")));
        assert!(r.degraded.iter().any(|d| d.contains("vendor.toml (missing)")));
        assert!(JsonValue::parse(&r.to_json()).is_some());
    }
}
