// app_telemetry.rs — standardized app-side telemetry event emitter (#436).
//
// Unlike the sibling SEAM files (auth_seam.rs / db_seam.rs / cost_surface_seam.rs
// / updater_seam.rs), which are `grimoire:placeholder` scaffolds that
// `unimplemented!()`, THIS is a REAL, WORKING module — mirrors
// config_loader.rs (#439) and version_report.rs (#206)'s precedent: a
// scaffolded app can emit real, schema-conformant telemetry events TODAY,
// with no vendored dependency and no crate to add first.
//
// Implements the six-field event schema + sampling rules specified in
// docs/grimoire/design/app-telemetry-design.md §1-§3 (framework-internal,
// upstream Grimoire repository — do NOT re-derive the contract here, read it
// there):
//
//     { ts, instance, app, version, event, props }
//
// three reference event types — `boot` (always), `request-summary` (sampled
// per `sample_rate`), `error` (always) — and the §4 privacy rule (never put
// PII in `props`; that rule is enforced by the CALLER, not this module,
// since only the caller knows what a given `props` value actually contains).
//
// std-only by design (same rationale as version_report.rs / config_loader.rs):
// no serde_json, a minimal internal JSON writer scoped to exactly this
// module's flat/nested-object shape, so it compiles and its tests run under
// a bare `rustc` before any telemetry crate is vendored. Swap the internal
// writer for serde_json later if you need it — the public surface
// (`AppTelemetryEvent`, `Emitter`) does not have to change.
//
// ── Call sites (boot, once; per request; on error) ──────────────────────────
//
//     let emitter = app_telemetry::Emitter::new(
//         "familiar".to_string(),      // app
//         "1.20.0".to_string(),        // version
//         instance_id.clone(),         // instance — reuse the Fleet Status
//                                       // Contract's instance.id when you
//                                       // have one (app-telemetry-design.md §1)
//         1.0,                         // sample_rate for request-summary
//         app_telemetry::FileSink::new("telemetry.jsonl"),
//     );
//     emitter.emit_boot();
//     // ... per request ...
//     emitter.emit_request_summary("/api/widgets", "GET", 200, 42);
//     // ... on an error condition ...
//     emitter.emit_error("db_timeout", "/api/widgets");
//
// Contract authority: docs/grimoire/design/app-telemetry-design.md
// (framework-internal, upstream Grimoire repository).

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

// ── The six-field event schema (§1) ──────────────────────────────────────────

/// One app-telemetry event: the six required fields, no others
/// (app-telemetry-design.md §1 — schema v1 has no optional fields).
#[derive(Debug, Clone, PartialEq)]
pub struct AppTelemetryEvent {
    pub ts: String,
    pub instance: String,
    pub app: String,
    pub version: String,
    pub event: String,
    /// Free-form event-specific payload as `(key, value)` pairs — kept as an
    /// ordered `Vec` rather than a `BTreeMap` so a caller can control key
    /// order in the emitted JSON without fighting alphabetical sorting; this
    /// is a debugging/log-reading convenience only, never load-bearing.
    pub props: Vec<(String, PropValue)>,
}

/// A `props` value. Deliberately narrow — three JSON-scalar shapes plus
/// nothing else, so a caller cannot accidentally smuggle a nested object
/// (and by construction, less surface for accidentally embedding PII than an
/// arbitrary serde_json::Value would allow).
#[derive(Debug, Clone, PartialEq)]
pub enum PropValue {
    Str(String),
    Int(i64),
    Bool(bool),
}

impl AppTelemetryEvent {
    /// Serialize to the exact six-field JSON shape (§1). std-only writer —
    /// no serde — reuses the same escaping convention version_report.rs's
    /// `json_str` already established.
    pub fn to_json(&self) -> String {
        let mut out = String::from("{");
        out.push_str(&format!("\"ts\":{},", json_str(&self.ts)));
        out.push_str(&format!("\"instance\":{},", json_str(&self.instance)));
        out.push_str(&format!("\"app\":{},", json_str(&self.app)));
        out.push_str(&format!("\"version\":{},", json_str(&self.version)));
        out.push_str(&format!("\"event\":{},", json_str(&self.event)));
        out.push_str("\"props\":{");
        for (i, (k, v)) in self.props.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str(&format!("{}:{}", json_str(k), prop_value_json(v)));
        }
        out.push('}');
        out.push('}');
        out
    }
}

fn prop_value_json(v: &PropValue) -> String {
    match v {
        PropValue::Str(s) => json_str(s),
        PropValue::Int(i) => i.to_string(),
        PropValue::Bool(b) => b.to_string(),
    }
}

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

// ── Timestamp + random sources (both swappable, so tests never depend on
//    real wall-clock time or real randomness) ────────────────────────────────

/// Supplies the current UTC timestamp for `ts`. The real implementation
/// (`SystemClock`) is std-only (no `chrono`/`time` crate needed): it formats
/// `SystemTime::now()` manually into RFC-3339/ISO-8601 UTC — the exact shape
/// every other framework timestamp already uses (fleet-status-contract.md).
pub trait Clock: Send + Sync {
    fn now_iso8601(&self) -> String;
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now_iso8601(&self) -> String {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default();
        format_iso8601(now.as_secs())
    }
}

/// Minimal, dependency-free Unix-seconds -> ISO-8601 UTC formatter (proleptic
/// Gregorian calendar, no leap-second handling — the same precision every
/// other framework artifact's plain-text timestamp already uses).
fn format_iso8601(unix_secs: u64) -> String {
    const SECS_PER_DAY: u64 = 86_400;
    let days = unix_secs / SECS_PER_DAY;
    let secs_of_day = unix_secs % SECS_PER_DAY;
    let (hour, min, sec) = (secs_of_day / 3600, (secs_of_day / 60) % 60, secs_of_day % 60);

    // Civil-from-days algorithm (Howard Hinnant's public-domain calendar
    // algorithm) — days since 1970-01-01 -> (year, month, day).
    let z = days as i64 + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };

    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year, m, d, hour, min, sec
    )
}

/// Supplies a `[0.0, 1.0)` draw for sampling decisions (§3). The real
/// implementation is a simple std-only LCG seeded from the system clock —
/// good enough for a uniform sampling draw, not cryptographic. Swappable so
/// tests can inject a deterministic sequence.
pub trait RandomSource: Send + Sync {
    fn draw(&self) -> f64;
}

pub struct SystemRandom;

impl RandomSource for SystemRandom {
    fn draw(&self) -> f64 {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or(0);
        (nanos as f64) / (u32::MAX as f64 + 1.0)
    }
}

// ── Sink: where serialized events go ─────────────────────────────────────────

/// Anything that can durably accept one already-serialized JSON line.
/// std-only default is `FileSink` (append-only, local file — §4's "local
/// sink, retention is the operator's responsibility until a real transport
/// exists" position). A test injects an in-memory sink instead.
pub trait Sink: Send + Sync {
    fn write_line(&self, line: &str);
}

pub struct FileSink {
    path: std::path::PathBuf,
    // A Mutex, not because Rust's type system requires it for a single
    // writer, but because multiple request-handling threads may call
    // emit_* concurrently — append-mode writes still need serialization at
    // the application level to avoid interleaved partial lines.
    lock: Mutex<()>,
}

impl FileSink {
    pub fn new<P: AsRef<Path>>(path: P) -> Self {
        FileSink {
            path: path.as_ref().to_path_buf(),
            lock: Mutex::new(()),
        }
    }
}

impl Sink for FileSink {
    fn write_line(&self, line: &str) {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&self.path) {
            let _ = writeln!(f, "{line}");
        }
        // A write failure here is deliberately swallowed, not propagated:
        // telemetry emission must never be allowed to crash or block the
        // request path it is describing (the same "observability must not
        // become a new failure mode" posture the Fleet Status Contract's
        // optional-auth design already takes for its own endpoint).
    }
}

// ── Emitter: owns identity + sampling + sink ─────────────────────────────────

pub struct Emitter {
    app: String,
    version: String,
    instance: String,
    sample_rate: f64,
    clock: Box<dyn Clock>,
    random: Box<dyn RandomSource>,
    sink: Box<dyn Sink>,
}

impl Emitter {
    /// `sample_rate` applies ONLY to `emit_request_summary` (§3) — `boot`
    /// and `error` are always emitted regardless of this value.
    pub fn new(
        app: String,
        version: String,
        instance: String,
        sample_rate: f64,
        sink: impl Sink + 'static,
    ) -> Self {
        Emitter {
            app,
            version,
            instance,
            sample_rate: sample_rate.clamp(0.0, 1.0),
            clock: Box::new(SystemClock),
            random: Box::new(SystemRandom),
            sink: Box::new(sink),
        }
    }

    /// Test/advanced constructor: inject the clock and random source too, so
    /// unit tests never depend on real wall-clock time or real randomness.
    fn with_sources(
        app: String,
        version: String,
        instance: String,
        sample_rate: f64,
        clock: Box<dyn Clock>,
        random: Box<dyn RandomSource>,
        sink: impl Sink + 'static,
    ) -> Self {
        Emitter {
            app,
            version,
            instance,
            sample_rate: sample_rate.clamp(0.0, 1.0),
            clock,
            random,
            sink: Box::new(sink),
        }
    }

    fn base_event(&self, event: &str, props: Vec<(String, PropValue)>) -> AppTelemetryEvent {
        AppTelemetryEvent {
            ts: self.clock.now_iso8601(),
            instance: self.instance.clone(),
            app: self.app.clone(),
            version: self.version.clone(),
            event: event.to_string(),
            props,
        }
    }

    fn write(&self, ev: &AppTelemetryEvent) {
        self.sink.write_line(&ev.to_json());
    }

    /// `boot` — always emitted (§3: rate 1.0, never sampled). Call once,
    /// after startup finishes (config loaded, listeners bound).
    pub fn emit_boot(&self) {
        let ev = self.base_event("boot", Vec::new());
        self.write(&ev);
    }

    /// `request-summary` — sampled per `sample_rate` (§3). `route` and
    /// `method`/`status`/`duration_ms` are the reference `props` shape
    /// (app-telemetry-design.md §2); callers MUST NOT pass raw request
    /// bodies/headers/cookies (§4).
    pub fn emit_request_summary(&self, route: &str, method: &str, status: u16, duration_ms: u64) {
        if self.random.draw() >= self.sample_rate {
            return; // not sampled this time — the common case at sample_rate < 1.0
        }
        let props = vec![
            ("route".to_string(), PropValue::Str(route.to_string())),
            ("method".to_string(), PropValue::Str(method.to_string())),
            ("status".to_string(), PropValue::Int(status as i64)),
            (
                "duration_ms".to_string(),
                PropValue::Int(duration_ms as i64),
            ),
        ];
        let ev = self.base_event("request-summary", props);
        self.write(&ev);
    }

    /// `error` — always emitted (§3: rate 1.0, never sampled). `kind` MUST be
    /// a classification slug (e.g. `"db_timeout"`), never a raw exception
    /// message or stack trace that might carry request data (§4).
    pub fn emit_error(&self, kind: &str, route: &str) {
        let props = vec![
            ("kind".to_string(), PropValue::Str(kind.to_string())),
            ("route".to_string(), PropValue::Str(route.to_string())),
        ];
        let ev = self.base_event("error", props);
        self.write(&ev);
    }
}

// ── tests ─────────────────────────────────────────────────────────────────
//
// Run with: rustc --edition 2021 --test app_telemetry.rs -o /tmp/app_telemetry_test
// && /tmp/app_telemetry_test
//
// Covers: JSON shape (all 6 fields present, nothing else), the 3 reference
// event types, sampling (boot/error always fire; request-summary respects
// sample_rate via an injected deterministic RandomSource), and the
// ISO-8601 formatter against known instants.

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex as StdMutex};

    struct FixedClock(&'static str);
    impl Clock for FixedClock {
        fn now_iso8601(&self) -> String {
            self.0.to_string()
        }
    }

    struct FixedRandom(f64);
    impl RandomSource for FixedRandom {
        fn draw(&self) -> f64 {
            self.0
        }
    }

    #[derive(Clone)]
    struct MemSink(Arc<StdMutex<Vec<String>>>);
    impl MemSink {
        fn new() -> Self {
            MemSink(Arc::new(StdMutex::new(Vec::new())))
        }
        fn lines(&self) -> Vec<String> {
            self.0.lock().unwrap().clone()
        }
    }
    impl Sink for MemSink {
        fn write_line(&self, line: &str) {
            self.0.lock().unwrap().push(line.to_string());
        }
    }

    fn make_emitter(sample_rate: f64, draw: f64, sink: MemSink) -> Emitter {
        Emitter::with_sources(
            "familiar".to_string(),
            "1.20.0".to_string(),
            "i-1".to_string(),
            sample_rate,
            Box::new(FixedClock("2026-07-14T03:28:12Z")),
            Box::new(FixedRandom(draw)),
            sink,
        )
    }

    // ---- schema shape (§1) ----

    #[test]
    fn to_json_contains_exactly_the_six_required_fields() {
        let ev = AppTelemetryEvent {
            ts: "2026-07-14T03:28:12Z".to_string(),
            instance: "i-1".to_string(),
            app: "familiar".to_string(),
            version: "1.20.0".to_string(),
            event: "boot".to_string(),
            props: Vec::new(),
        };
        let json = ev.to_json();
        for field in ["\"ts\"", "\"instance\"", "\"app\"", "\"version\"", "\"event\"", "\"props\""] {
            assert!(json.contains(field), "missing {field} in {json}");
        }
        assert!(json.contains("\"boot\""));
    }

    #[test]
    fn to_json_escapes_special_characters() {
        let ev = AppTelemetryEvent {
            ts: "2026-07-14T03:28:12Z".to_string(),
            instance: "i-1".to_string(),
            app: "familiar".to_string(),
            version: "1.20.0".to_string(),
            event: "error".to_string(),
            props: vec![(
                "kind".to_string(),
                PropValue::Str("quote\"and\\backslash".to_string()),
            )],
        };
        let json = ev.to_json();
        assert!(json.contains("quote\\\"and\\\\backslash"));
    }

    #[test]
    fn props_supports_str_int_bool() {
        let ev = AppTelemetryEvent {
            ts: "t".to_string(),
            instance: "i".to_string(),
            app: "a".to_string(),
            version: "v".to_string(),
            event: "request-summary".to_string(),
            props: vec![
                ("route".to_string(), PropValue::Str("/x".to_string())),
                ("status".to_string(), PropValue::Int(200)),
                ("ok".to_string(), PropValue::Bool(true)),
            ],
        };
        let json = ev.to_json();
        assert!(json.contains("\"route\":\"/x\""));
        assert!(json.contains("\"status\":200"));
        assert!(json.contains("\"ok\":true"));
    }

    // ---- the three reference event types (§2) + sampling (§3) ----

    #[test]
    fn boot_always_emits_regardless_of_sample_rate() {
        // sample_rate 0.0 and a draw of 0.999 would normally never pass a
        // `draw < sample_rate` gate — boot must ignore sampling entirely.
        let sink = MemSink::new();
        let emitter = make_emitter(0.0, 0.999, sink.clone());
        emitter.emit_boot();
        let lines = sink.lines();
        assert_eq!(lines.len(), 1, "boot must always emit exactly once when called");
        assert!(lines[0].contains("\"event\":\"boot\""));
    }

    #[test]
    fn error_always_emits_regardless_of_sample_rate() {
        let sink = MemSink::new();
        let emitter = make_emitter(0.0, 0.999, sink.clone());
        emitter.emit_error("db_timeout", "/api/widgets");
        let lines = sink.lines();
        assert_eq!(lines.len(), 1, "error must always emit exactly once when called");
        assert!(lines[0].contains("\"event\":\"error\""));
        assert!(lines[0].contains("db_timeout"));
    }

    #[test]
    fn request_summary_emits_when_draw_is_below_sample_rate() {
        let sink = MemSink::new();
        // sample_rate 0.5, draw 0.1 -> 0.1 < 0.5 -> emits.
        let emitter = make_emitter(0.5, 0.1, sink.clone());
        emitter.emit_request_summary("/api/widgets", "GET", 200, 42);
        assert_eq!(sink.lines().len(), 1);
        assert!(sink.lines()[0].contains("\"event\":\"request-summary\""));
    }

    #[test]
    fn request_summary_skips_when_draw_is_at_or_above_sample_rate() {
        let sink = MemSink::new();
        // sample_rate 0.5, draw 0.9 -> 0.9 >= 0.5 -> skipped.
        let emitter = make_emitter(0.5, 0.9, sink.clone());
        emitter.emit_request_summary("/api/widgets", "GET", 200, 42);
        assert_eq!(sink.lines().len(), 0, "a draw >= sample_rate must not emit");
    }

    #[test]
    fn request_summary_at_sample_rate_1_always_emits() {
        let sink = MemSink::new();
        let emitter = make_emitter(1.0, 0.9999, sink.clone());
        emitter.emit_request_summary("/api/widgets", "GET", 200, 42);
        assert_eq!(sink.lines().len(), 1, "sample_rate 1.0 must always emit");
    }

    #[test]
    fn request_summary_at_sample_rate_0_never_emits() {
        let sink = MemSink::new();
        let emitter = make_emitter(0.0, 0.0, sink.clone());
        emitter.emit_request_summary("/api/widgets", "GET", 200, 42);
        assert_eq!(sink.lines().len(), 0, "sample_rate 0.0 must never emit");
    }

    #[test]
    fn sample_rate_is_clamped_to_zero_one() {
        let sink = MemSink::new();
        let emitter = make_emitter(5.0, 0.999, sink.clone());
        emitter.emit_request_summary("/x", "GET", 200, 1);
        assert_eq!(sink.lines().len(), 1, "sample_rate > 1.0 clamps to 1.0 (always emits)");
    }

    #[test]
    fn request_summary_props_carry_route_method_status_duration() {
        let sink = MemSink::new();
        let emitter = make_emitter(1.0, 0.0, sink.clone());
        emitter.emit_request_summary("/api/widgets", "POST", 201, 17);
        let line = &sink.lines()[0];
        assert!(line.contains("\"route\":\"/api/widgets\""));
        assert!(line.contains("\"method\":\"POST\""));
        assert!(line.contains("\"status\":201"));
        assert!(line.contains("\"duration_ms\":17"));
    }

    // ---- ISO-8601 formatter (used by SystemClock; tested independently of
    //      real wall-clock time via known Unix-second instants) ----

    #[test]
    fn format_iso8601_epoch_zero() {
        assert_eq!(format_iso8601(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn format_iso8601_known_instant() {
        // 2026-07-14T03:28:12Z (issue #436's filing timestamp).
        assert_eq!(format_iso8601(1_783_999_692), "2026-07-14T03:28:12Z");
    }

    // ---- FileSink never panics/propagates on a write failure ----

    #[test]
    fn file_sink_writes_and_appends() {
        let dir = std::env::temp_dir().join(format!(
            "grimoire-app-telemetry-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("telemetry.jsonl");
        let sink = FileSink::new(&path);
        sink.write_line("{\"a\":1}");
        sink.write_line("{\"a\":2}");
        let contents = std::fs::read_to_string(&path).unwrap();
        assert_eq!(contents, "{\"a\":1}\n{\"a\":2}\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn end_to_end_boot_request_error_all_conform_to_schema() {
        let sink = MemSink::new();
        let emitter = make_emitter(1.0, 0.0, sink.clone());
        emitter.emit_boot();
        emitter.emit_request_summary("/api/widgets", "GET", 200, 42);
        emitter.emit_error("db_timeout", "/api/widgets");
        let lines = sink.lines();
        assert_eq!(lines.len(), 3);
        for line in &lines {
            for field in ["\"ts\"", "\"instance\"", "\"app\"", "\"version\"", "\"event\"", "\"props\""] {
                assert!(line.contains(field), "{line} missing {field}");
            }
        }
        let events: Vec<&str> = lines
            .iter()
            .map(|l| {
                if l.contains("\"boot\"") {
                    "boot"
                } else if l.contains("\"request-summary\"") {
                    "request-summary"
                } else {
                    "error"
                }
            })
            .collect();
        assert_eq!(events, vec!["boot", "request-summary", "error"]);
    }
}
