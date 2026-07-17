// logging_init.rs — standardized JSON-lines logging (docs/coding-standards.md
// §Logging). ONE call at process start; every tracing::{trace,debug,info,
// warn,error}! call site downstream needs no per-call boilerplate.
//
// Emits one JSON object per line to stdout with the standard field
// contract: ts (ms since Unix epoch), level, target, msg, correlation_id,
// instance, version. Level is set via instance config (the caller passes
// the resolved `log_level`, sourced from config.rs's LOG_LEVEL knob).
// Rotation stays the deployment supervisor's job — it already captures
// stdout/stderr into logs/ (docs/web-app-deployment-protocol.md §4); this
// module never opens or rotates a file itself.
//
// `correlation_id` is ambient, not threaded through every call site: request
// handling code calls `set_correlation_id`/`clear_correlation_id` once per
// request; every log line emitted on that thread while it's set carries the
// id automatically.
//
// Copied per-template code, not a shared crate — the cross-repo rule-of-two
// policy (coding-standards.md §Cross-repo extraction policy) hasn't
// triggered: nothing has been hand-rolled twice across sibling repos yet.
// Extract to a standard package only once a second app duplicates this file.

use std::cell::RefCell;
use std::fmt::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

use tracing::{Event, Subscriber};
use tracing_subscriber::fmt::format::Writer;
use tracing_subscriber::fmt::{FmtContext, FormatEvent, FormatFields};
use tracing_subscriber::registry::LookupSpan;

thread_local! {
    static CORRELATION_ID: RefCell<String> = const { RefCell::new(String::new()) };
}

/// Set the correlation id for every log line emitted on THIS thread until
/// cleared. Call once at the top of a request/task; no per-log-site
/// argument needed.
///
/// Unused until the app wires a request/task boundary that calls it — kept
/// `#[allow(dead_code)]` rather than left as scaffold warning noise, same as
/// the other seam-style starter modules in this template.
#[allow(dead_code)]
pub fn set_correlation_id(id: &str) {
    CORRELATION_ID.with(|c| *c.borrow_mut() = id.to_string());
}

/// Clear the current thread's correlation id (e.g. at the end of a request).
#[allow(dead_code)]
pub fn clear_correlation_id() {
    CORRELATION_ID.with(|c| c.borrow_mut().clear());
}

struct JsonLineFormatter {
    instance: String,
    version: String,
}

impl<S, N> FormatEvent<S, N> for JsonLineFormatter
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    N: for<'a> FormatFields<'a> + 'static,
{
    fn format_event(
        &self,
        _ctx: &FmtContext<'_, S, N>,
        mut writer: Writer<'_>,
        event: &Event<'_>,
    ) -> std::fmt::Result {
        let meta = event.metadata();
        let ts_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);

        let mut msg = String::new();
        let mut visitor = MessageVisitor(&mut msg);
        event.record(&mut visitor);

        let correlation_id = CORRELATION_ID.with(|c| c.borrow().clone());

        writeln!(
            writer,
            "{{\"ts\":{ts_ms},\"level\":\"{level}\",\"target\":\"{target}\",\"msg\":\"{msg}\",\"correlation_id\":\"{cid}\",\"instance\":\"{instance}\",\"version\":\"{version}\"}}",
            level = meta.level().to_string().to_lowercase(),
            target = json_escape(meta.target()),
            msg = json_escape(&msg),
            cid = json_escape(&correlation_id),
            instance = json_escape(&self.instance),
            version = json_escape(&self.version),
        )
    }
}

struct MessageVisitor<'a>(&'a mut String);

impl tracing::field::Visit for MessageVisitor<'_> {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            let _ = write!(self.0, "{value:?}");
        }
    }
}

/// Minimal JSON string escaper (quotes, backslashes, control chars) — kept
/// hand-rolled rather than pulling in `serde_json` for a single-purpose
/// starter module.
fn json_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for c in input.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out
}

/// ONE call at process start (main.rs, before any other logging). `level` is
/// the instance-config-resolved filter string (config.rs's LOG_LEVEL —
/// trace/debug/info/warn/error); `instance` and `version` are stamped onto
/// every emitted line.
pub fn init(level: &str, instance: &str, version: &str) {
    let filter = tracing_subscriber::EnvFilter::try_new(level)
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    let formatter = JsonLineFormatter {
        instance: instance.to_string(),
        version: version.to_string(),
    };
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .event_format(formatter)
        .with_writer(std::io::stdout)
        .try_init();
}

/// Resolve the instance identifier from instance config: `INSTANCE_ID` env
/// var, falling back to a generic default when unset — mirrors config.rs's
/// env-var-overrides-default convention (no magic-number-style silent
/// blank; the fallback is named and explicit).
pub fn instance_id() -> String {
    std::env::var("INSTANCE_ID").unwrap_or_else(|_| "local".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_escape_handles_quotes_and_control_chars() {
        assert_eq!(json_escape("hello \"world\""), "hello \\\"world\\\"");
        assert_eq!(json_escape("line1\nline2"), "line1\\nline2");
        assert_eq!(json_escape("a\\b"), "a\\\\b");
    }

    #[test]
    fn instance_id_falls_back_to_local_when_unset() {
        std::env::remove_var("INSTANCE_ID");
        assert_eq!(instance_id(), "local");
    }
}
