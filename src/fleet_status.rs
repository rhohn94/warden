//! Fleet Status Contract v1 — local consumer (#55).
//!
//! Two-source contract (spec: Meta Planner/fleet-status-contract.md):
//! - **Half 2 (static):** `fleet-instance.json` in the app dir — declared
//!   intent, survives the process being down. Parsed during scan.
//! - **Half 1 (runtime):** `GET /fleet/v1/status` on the detected port —
//!   what is *actually* running. Falls back to bare `/healthz`.
//! - **Reconciliation:** declared vs running version disagreement is the
//!   drift signal (a failed/pending self-update caught on-machine).
//!
//! Heterogeneity is the default: today most fleet instances expose neither
//! source. Absence is therefore a first-class, honestly-displayed state
//! (`NoEndpoint`), never an error. Field names deliberately mirror
//! mission-control's `core/src/fleet.rs` types.
//!
//! Health-probe rule (issue-tracker incident, 2026-07-12): a 200 alone is NOT
//! health — issue-tracker's health endpoint returned HTTP 200 `ok` for hours
//! while its database was dead. Probes here always surface the response
//! body's `status` field, not just the HTTP code.

use serde::{Deserialize, Deserializer, Serialize};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::time::Duration;

/// Contract schema version this consumer targets. Versions `{N, N-1}` are
/// tolerated per the cross-cutting rules (a bare `/healthz` body carries 0).
pub const SUPPORTED_SCHEMA: u32 = 1;

/// True iff `schema_version` is within the tolerated window `{N, N-1, ...0}`.
pub fn schema_supported(schema_version: u32) -> bool {
    schema_version <= SUPPORTED_SCHEMA
}

/// Accept `"schema_version": 1` and `"schema_version": "1"` — the spec's
/// example shows a string, mission-control parses a number; tolerate both.
fn de_schema_version<'de, D: Deserializer<'de>>(d: D) -> Result<u32, D::Error> {
    let val = serde_json::Value::deserialize(d)?;
    Ok(match val {
        serde_json::Value::Number(n) => n.as_u64().unwrap_or(0) as u32,
        serde_json::Value::String(s) => s.trim().parse().unwrap_or(0),
        _ => 0,
    })
}

/// Instance identity carried by both halves of the contract.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetInstanceIdentity {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub env: Option<String>,
}

/// Build metadata from the runtime endpoint (`build{}` in the contract).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetBuild {
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub git_sha: Option<String>,
    #[serde(default)]
    pub built_at: Option<String>,
}

/// Runtime metadata from the endpoint (`runtime{}` in the contract).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetRuntime {
    /// `up | degraded | starting | draining` — the app's own claim.
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub bind: Option<String>,
    #[serde(default)]
    pub started_at: Option<String>,
}

/// Update-channel verdict from the endpoint (`update{}` in the contract).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetUpdate {
    #[serde(default)]
    pub channel: Option<String>,
    /// `UpToDate | UpdateAvailable | Unknown | NotConfigured`.
    #[serde(default)]
    pub verdict: Option<String>,
    #[serde(default)]
    pub current: Option<String>,
    #[serde(default)]
    pub available: Option<String>,
    #[serde(default)]
    pub last_checked: Option<String>,
}

/// One dependency edge (`dependencies[]`). Kept stringly-typed and tolerant —
/// warden displays these; mission-control owns the typed reconciliation.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetDependency {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub endpoint: Option<String>,
    #[serde(default)]
    pub reachable: Option<bool>,
    #[serde(default)]
    pub status: Option<String>,
}

/// Half 1 — the runtime endpoint body (`GET /fleet/v1/status`). All
/// sub-objects optional so a bare `/healthz` body (`{status,version}`) still
/// deserializes into a degraded-but-valid value.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetStatus {
    #[serde(default, deserialize_with = "de_schema_version")]
    pub schema_version: u32,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub instance: FleetInstanceIdentity,
    #[serde(default)]
    pub build: FleetBuild,
    #[serde(default)]
    pub runtime: FleetRuntime,
    #[serde(default)]
    pub dependencies: Vec<FleetDependency>,
    #[serde(default)]
    pub update: FleetUpdate,
    /// Bare-`/healthz` top-level fallbacks (no nested `build`/`runtime`).
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub env: Option<String>,
}

impl FleetStatus {
    /// Parse a contract (or bare `/healthz`) JSON body. `None` for non-JSON
    /// or a schema version outside the tolerated `{N, N-1}` window.
    pub fn parse(body: &str) -> Option<Self> {
        let s: FleetStatus = serde_json::from_str(body.trim()).ok()?;
        if !schema_supported(s.schema_version) {
            return None;
        }
        Some(s)
    }

    /// The running version: prefer `build.version`, fall back to the bare
    /// top-level `version`.
    pub fn running_version(&self) -> Option<&str> {
        self.build.version.as_deref().or(self.version.as_deref())
    }

    /// The app's own status claim: prefer `runtime.status`, fall back to the
    /// bare top-level `status`.
    pub fn reported_status(&self) -> Option<&str> {
        self.runtime.status.as_deref().or(self.status.as_deref())
    }
}

/// The declared half of the contract (`declared{}` in `fleet-instance.json`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetDeclared {
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub git_sha: Option<String>,
    #[serde(default)]
    pub bind: Option<String>,
    #[serde(default)]
    pub endpoint: Option<String>,
    #[serde(default)]
    pub channel: Option<String>,
}

/// Half 2 — the static manifest `fleet-instance.json` (declared intent).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FleetManifest {
    #[serde(default, deserialize_with = "de_schema_version")]
    pub schema_version: u32,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub instance: FleetInstanceIdentity,
    #[serde(default)]
    pub declared: FleetDeclared,
    #[serde(default)]
    pub dependencies: Vec<FleetDependency>,
    #[serde(default)]
    pub deployed_at: Option<String>,
    #[serde(default)]
    pub deployed_by: Option<String>,
}

impl FleetManifest {
    /// Parse a `fleet-instance.json` body. `None` on non-JSON or an
    /// out-of-tolerance schema version.
    pub fn parse(body: &str) -> Option<Self> {
        let m: FleetManifest = serde_json::from_str(body.trim()).ok()?;
        if !schema_supported(m.schema_version) {
            return None;
        }
        Some(m)
    }

    /// Load from an app dir; absence is clean `None` (no instance writes the
    /// manifest yet — the consumer is tolerant-first).
    pub fn load(app_dir: &Path) -> Option<Self> {
        let path = app_dir.join("fleet-instance.json");
        let body = std::fs::read_to_string(path).ok()?;
        let parsed = Self::parse(&body);
        if parsed.is_none() {
            tracing::warn!(
                "fleet-instance.json present but unparseable/unsupported in {}",
                app_dir.display()
            );
        }
        parsed
    }
}

/// Outcome of the runtime probe — absence is a first-class state, not an
/// error. An honest "this app exposes nothing" display is the point.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum FleetProbe {
    /// Not probed: app not running or no known port.
    #[default]
    NotProbed,
    /// `/fleet/v1/status` answered with a parseable contract body.
    Contract(Box<FleetStatus>),
    /// Only bare `/healthz` answered. `status` is the response BODY's status
    /// field (never trust the HTTP code alone); `None` when the body carried
    /// no parseable status.
    Healthz {
        status: Option<String>,
        version: Option<String>,
    },
    /// The port accepts connections but serves neither `/fleet/v1/status`
    /// nor `/healthz` — the app exposes no status surface at all.
    NoEndpoint,
    /// TCP connect to the detected port failed (refused/timeout) even though
    /// the process looks alive.
    Unreachable,
}

impl FleetProbe {
    /// The running version reported by the live process, when any.
    pub fn running_version(&self) -> Option<&str> {
        match self {
            FleetProbe::Contract(s) => s.running_version(),
            FleetProbe::Healthz { version, .. } => version.as_deref(),
            _ => None,
        }
    }

    /// The app's own status claim (`up | degraded | ...`), when any.
    pub fn reported_status(&self) -> Option<&str> {
        match self {
            FleetProbe::Contract(s) => s.reported_status(),
            FleetProbe::Healthz { status, .. } => status.as_deref(),
            _ => None,
        }
    }
}

/// Declared-vs-running version drift (#55 acceptance 3). Returns
/// `Some((declared, running))` when both are known and disagree after
/// normalization — the on-machine signal of a failed or pending self-update.
pub fn version_drift(declared: Option<&str>, running: Option<&str>) -> Option<(String, String)> {
    let d = declared?.trim();
    let r = running?.trim();
    if d.is_empty() || r.is_empty() {
        return None;
    }
    let norm = |v: &str| v.strip_prefix(['v', 'V']).unwrap_or(v).to_string();
    if norm(d) != norm(r) {
        Some((d.to_string(), r.to_string()))
    } else {
        None
    }
}

// ── Runtime probe ────────────────────────────────────────────────────────────

/// Per-request budget. Localhost round-trips; keep it short so the scanner's
/// per-app detection window is never blown by a wedged listener.
const CONNECT_TIMEOUT: Duration = Duration::from_millis(300);
const IO_TIMEOUT: Duration = Duration::from_millis(700);

/// Probe a Running app's detected port: `/fleet/v1/status` first, bare
/// `/healthz` as the degraded fallback.
pub fn probe(port: u16) -> FleetProbe {
    match http_get_local(port, "/fleet/v1/status") {
        Err(_) => return FleetProbe::Unreachable,
        Ok(resp) => {
            if resp.code == 200 {
                if let Some(status) = FleetStatus::parse(&resp.body) {
                    return FleetProbe::Contract(Box::new(status));
                }
            }
        }
    }
    match http_get_local(port, "/healthz") {
        // Connected a moment ago; treat a mid-probe drop as no surface rather
        // than flapping to Unreachable.
        Err(_) => FleetProbe::NoEndpoint,
        Ok(resp) if resp.code == 200 => {
            let (status, version) = parse_health_body(&resp.body);
            FleetProbe::Healthz { status, version }
        }
        Ok(_) => FleetProbe::NoEndpoint,
    }
}

/// Extract (`status`, `version`) from a health body. JSON bodies yield their
/// `status`/`version` string fields; a plain-text body like `ok` becomes the
/// status itself; anything else yields `None`s.
fn parse_health_body(body: &str) -> (Option<String>, Option<String>) {
    let trimmed = body.trim();
    if let Ok(val) = serde_json::from_str::<serde_json::Value>(trimmed) {
        let status = val
            .get("status")
            .and_then(serde_json::Value::as_str)
            .map(str::to_string);
        let version = val
            .get("version")
            .and_then(serde_json::Value::as_str)
            .map(str::to_string);
        return (status, version);
    }
    // Short plain-text health bodies ("ok", "healthy") count as a status.
    if !trimmed.is_empty() && trimmed.len() <= 32 && !trimmed.contains('<') {
        return (Some(trimmed.to_string()), None);
    }
    (None, None)
}

struct HttpResponse {
    code: u16,
    body: String,
}

/// Minimal HTTP/1.1 GET against 127.0.0.1:<port> with strict timeouts.
/// `Connection: close` + read-to-EOF keeps parsing simple; chunked bodies
/// are decoded. No external HTTP dependency.
fn http_get_local(port: u16, path: &str) -> std::io::Result<HttpResponse> {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT)?;
    stream.set_read_timeout(Some(IO_TIMEOUT))?;
    stream.set_write_timeout(Some(IO_TIMEOUT))?;

    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\nUser-Agent: warden\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;

    // Read to EOF, bounded (a status body is small; don't slurp a runaway stream).
    const MAX_RESPONSE: usize = 256 * 1024;
    let mut raw = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() >= MAX_RESPONSE {
                    break;
                }
            }
            Err(e) => {
                // Timeout after some data: parse what we have.
                if raw.is_empty() {
                    return Err(e);
                }
                break;
            }
        }
    }

    parse_http_response(&raw).ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "malformed HTTP response")
    })
}

/// Parse status code and body out of a raw HTTP/1.1 response, decoding
/// chunked transfer-encoding when present.
fn parse_http_response(raw: &[u8]) -> Option<HttpResponse> {
    let text = String::from_utf8_lossy(raw);
    let header_end = text.find("\r\n\r\n")?;
    let (head, body) = text.split_at(header_end);
    let body = &body[4..];

    let status_line = head.lines().next()?;
    let code: u16 = status_line.split_whitespace().nth(1)?.parse().ok()?;

    let chunked = head
        .lines()
        .any(|l| {
            let l = l.to_ascii_lowercase();
            l.starts_with("transfer-encoding:") && l.contains("chunked")
        });

    let body = if chunked {
        decode_chunked(body)
    } else {
        body.to_string()
    };
    Some(HttpResponse { code, body })
}

/// Decode an HTTP/1.1 chunked body (sizes in hex, CRLF-delimited).
fn decode_chunked(body: &str) -> String {
    let mut out = String::new();
    let mut rest = body;
    while let Some(line_end) = rest.find("\r\n") {
        let size_str = rest[..line_end].trim();
        let Ok(size) = usize::from_str_radix(size_str.split(';').next().unwrap_or(""), 16) else {
            break;
        };
        if size == 0 {
            break;
        }
        let chunk_start = line_end + 2;
        let chunk_end = chunk_start + size;
        if chunk_end > rest.len() {
            // Truncated read — take what's there.
            out.push_str(&rest[chunk_start..]);
            break;
        }
        out.push_str(&rest[chunk_start..chunk_end]);
        rest = rest[chunk_end..].strip_prefix("\r\n").unwrap_or(&rest[chunk_end..]);
    }
    out
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write as _;
    use std::net::TcpListener;

    const CONTRACT_BODY: &str = r#"{
        "schema_version": "1",
        "app": "familiar",
        "instance": {"id": "01JABCD", "name": "windows-lan-shared", "env": "beta"},
        "build": {"version": "1.14.0", "git_sha": "9a6ea76", "built_at": "2026-06-07T20:13:00Z"},
        "runtime": {"status": "up", "bind": "0.0.0.0:2121", "started_at": "2026-06-08T09:00:00Z"},
        "dependencies": [
            {"name": "obsidian", "kind": "build", "version": "0.9.0", "source": "git:tag=v0.9.0"}
        ],
        "update": {"channel": "beta", "verdict": "UpToDate", "current": "1.14.0", "available": null}
    }"#;

    // ── contract parsing (#55 acceptance 4: fixture JSON) ────────────────

    #[test]
    fn parses_full_contract_fixture() {
        let s = FleetStatus::parse(CONTRACT_BODY).expect("contract should parse");
        assert_eq!(s.schema_version, 1);
        assert_eq!(s.app.as_deref(), Some("familiar"));
        assert_eq!(s.instance.id.as_deref(), Some("01JABCD"));
        assert_eq!(s.instance.env.as_deref(), Some("beta"));
        assert_eq!(s.build.git_sha.as_deref(), Some("9a6ea76"));
        assert_eq!(s.running_version(), Some("1.14.0"));
        assert_eq!(s.reported_status(), Some("up"));
        assert_eq!(s.update.verdict.as_deref(), Some("UpToDate"));
        assert_eq!(s.dependencies.len(), 1);
        assert_eq!(s.dependencies[0].name.as_deref(), Some("obsidian"));
    }

    #[test]
    fn schema_version_accepts_number_and_string() {
        let n = FleetStatus::parse(r#"{"schema_version": 1, "app": "x"}"#).unwrap();
        assert_eq!(n.schema_version, 1);
        let s = FleetStatus::parse(r#"{"schema_version": "1", "app": "x"}"#).unwrap();
        assert_eq!(s.schema_version, 1);
    }

    #[test]
    fn future_schema_version_is_rejected() {
        assert!(FleetStatus::parse(r#"{"schema_version": 99}"#).is_none());
        assert!(FleetManifest::parse(r#"{"schema_version": 99}"#).is_none());
    }

    #[test]
    fn bare_healthz_body_parses_with_top_level_fallbacks() {
        let s = FleetStatus::parse(r#"{"status": "ok", "version": "9.29.0"}"#).unwrap();
        assert_eq!(s.schema_version, 0);
        assert_eq!(s.running_version(), Some("9.29.0"));
        assert_eq!(s.reported_status(), Some("ok"));
    }

    #[test]
    fn non_json_body_is_none() {
        assert!(FleetStatus::parse("<html>hi</html>").is_none());
        assert!(FleetStatus::parse("").is_none());
    }

    // ── manifest (#55 acceptance 1) ───────────────────────────────────────

    #[test]
    fn parses_manifest_fixture() {
        let body = r#"{
            "schema_version": "1",
            "app": "familiar",
            "instance": {"id": "01JABCD", "name": "windows-lan-shared", "env": "beta"},
            "declared": {"version": "1.14.0", "git_sha": "9a6ea76", "bind": "0.0.0.0:2121",
                          "endpoint": "http://win-lan:2121/fleet/v1/status", "channel": "beta"},
            "dependencies": [{"name": "obsidian", "version": "0.9.0"}],
            "deployed_at": "2026-06-08T08:55:00Z",
            "deployed_by": "deploy.sh@v1.14.0"
        }"#;
        let m = FleetManifest::parse(body).expect("manifest should parse");
        assert_eq!(m.declared.version.as_deref(), Some("1.14.0"));
        assert_eq!(m.instance.env.as_deref(), Some("beta"));
        assert_eq!(m.deployed_by.as_deref(), Some("deploy.sh@v1.14.0"));
    }

    #[test]
    fn absent_manifest_is_clean_none() {
        let tmp = tempfile::TempDir::new().unwrap();
        assert!(FleetManifest::load(tmp.path()).is_none());
    }

    #[test]
    fn unparseable_manifest_is_none_not_panic() {
        let tmp = tempfile::TempDir::new().unwrap();
        std::fs::write(tmp.path().join("fleet-instance.json"), "not json").unwrap();
        assert!(FleetManifest::load(tmp.path()).is_none());
    }

    // ── drift (#55 acceptance 3) ──────────────────────────────────────────

    #[test]
    fn drift_when_declared_and_running_disagree() {
        assert_eq!(
            version_drift(Some("1.14.0"), Some("1.13.2")),
            Some(("1.14.0".to_string(), "1.13.2".to_string()))
        );
    }

    #[test]
    fn no_drift_on_match_or_v_prefix_difference() {
        assert_eq!(version_drift(Some("1.14.0"), Some("1.14.0")), None);
        assert_eq!(version_drift(Some("v1.14.0"), Some("1.14.0")), None);
    }

    #[test]
    fn no_drift_when_either_side_unknown() {
        assert_eq!(version_drift(None, Some("1.14.0")), None);
        assert_eq!(version_drift(Some("1.14.0"), None), None);
        assert_eq!(version_drift(None, None), None);
    }

    // ── health-body rule (issue-tracker incident) ─────────────────────────

    #[test]
    fn health_body_status_field_is_surfaced_not_just_http_200() {
        // A 200 with a degraded body must NOT read as healthy.
        let (status, _) = parse_health_body(r#"{"status": "degraded", "version": "2.2.2"}"#);
        assert_eq!(status.as_deref(), Some("degraded"));

        let (status, version) = parse_health_body(r#"{"status": "ok", "version": "2.2.2"}"#);
        assert_eq!(status.as_deref(), Some("ok"));
        assert_eq!(version.as_deref(), Some("2.2.2"));
    }

    #[test]
    fn plain_text_ok_body_becomes_status() {
        let (status, version) = parse_health_body("ok\n");
        assert_eq!(status.as_deref(), Some("ok"));
        assert_eq!(version, None);
    }

    #[test]
    fn html_or_empty_body_yields_no_status() {
        assert_eq!(parse_health_body("<html>login</html>").0, None);
        assert_eq!(parse_health_body("").0, None);
    }

    // ── live probe against a canned local server ──────────────────────────

    /// Serve exactly one canned HTTP response per accepted connection, routed
    /// by request path, on an ephemeral port. Returns the port.
    fn serve_routes(routes: Vec<(&'static str, u16, &'static str)>, connections: usize) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for _ in 0..connections {
                let Ok((mut sock, _)) = listener.accept() else { return };
                let mut buf = [0u8; 2048];
                let n = sock.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let path = req
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("/")
                    .to_string();
                let (code, body) = routes
                    .iter()
                    .find(|(p, _, _)| *p == path)
                    .map(|(_, c, b)| (*c, *b))
                    .unwrap_or((404, "not found"));
                let reason = if code == 200 { "OK" } else { "Not Found" };
                let resp = format!(
                    "HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = sock.write_all(resp.as_bytes());
            }
        });
        port
    }

    #[test]
    fn probe_reads_full_contract_endpoint() {
        let port = serve_routes(vec![("/fleet/v1/status", 200, CONTRACT_BODY)], 1);
        match probe(port) {
            FleetProbe::Contract(s) => {
                assert_eq!(s.app.as_deref(), Some("familiar"));
                assert_eq!(s.running_version(), Some("1.14.0"));
            }
            other => panic!("expected Contract, got {other:?}"),
        }
    }

    #[test]
    fn probe_falls_back_to_healthz_and_surfaces_body_status() {
        let port = serve_routes(
            vec![("/healthz", 200, r#"{"status": "degraded", "version": "2.2.2"}"#)],
            2,
        );
        match probe(port) {
            FleetProbe::Healthz { status, version } => {
                assert_eq!(status.as_deref(), Some("degraded"));
                assert_eq!(version.as_deref(), Some("2.2.2"));
            }
            other => panic!("expected Healthz, got {other:?}"),
        }
    }

    #[test]
    fn probe_no_endpoint_is_distinct_not_error() {
        // Server up, but neither /fleet/v1/status nor /healthz exists (the
        // common case fleet-wide today) → the honest NoEndpoint state.
        let port = serve_routes(vec![("/", 200, "app homepage")], 2);
        assert_eq!(probe(port), FleetProbe::NoEndpoint);
    }

    #[test]
    fn probe_connection_refused_is_unreachable() {
        // Bind then drop to get a port with no listener.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert_eq!(probe(port), FleetProbe::Unreachable);
    }

    #[test]
    fn chunked_body_is_decoded() {
        let raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\n{\"statu\r\n9\r\ns\": \"ok\"}\r\n0\r\n\r\n";
        let resp = parse_http_response(raw).unwrap();
        assert_eq!(resp.code, 200);
        assert_eq!(resp.body, r#"{"status": "ok"}"#);
    }
}
