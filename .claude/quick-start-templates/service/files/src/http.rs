// http.rs — a tiny, REAL, std-only HTTP/1.1 request handler. No framework
// dependency (matches the version_report.rs zero-dependency philosophy):
// parse the request line, route GET /healthz to a `{status, version}` JSON
// body — the EXACT contract the web quick-start template's `just smoke`
// recipe already asserts (docs/web-app-deployment-protocol.md §5) — and 404
// everything else. Replace the router with axum/actix/etc. as your service
// grows; this is a real, minimal starting point, not a toy.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpStream;

/// Handle one connection: read the request line + headers off the socket,
/// route it, and write a real HTTP/1.1 response.
pub fn handle_connection(stream: TcpStream, version: &str) {
    let mut reader = BufReader::new(match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    });
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
        return;
    }
    // Drain remaining header lines off the socket (unused, but must be read
    // so the connection: close response below is written cleanly).
    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                if line.trim().is_empty() {
                    break;
                }
            }
        }
    }
    let (status_line, body) = route(&request_line, version);
    let response = format!(
        "HTTP/1.1 {status_line}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let mut stream = stream;
    let _ = stream.write_all(response.as_bytes());
}

/// Pure routing logic (no I/O) — directly unit-testable. `GET /healthz`
/// returns the standard `{status, version}` JSON body; everything else 404s.
fn route(request_line: &str, version: &str) -> (&'static str, String) {
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");
    if method == "GET" && path == "/healthz" {
        (
            "200 OK",
            format!("{{\"status\": \"ok\", \"version\": \"{version}\"}}"),
        )
    } else {
        ("404 Not Found", "{\"error\": \"not found\"}".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn healthz_returns_ok_status_and_version() {
        let (status, body) = route("GET /healthz HTTP/1.1", "1.2.3");
        assert_eq!(status, "200 OK");
        assert!(body.contains("\"status\": \"ok\""));
        assert!(body.contains("\"version\": \"1.2.3\""));
    }

    #[test]
    fn unknown_route_returns_404() {
        let (status, _body) = route("GET /nope HTTP/1.1", "1.2.3");
        assert_eq!(status, "404 Not Found");
    }

    #[test]
    fn wrong_method_on_healthz_returns_404() {
        let (status, _body) = route("POST /healthz HTTP/1.1", "1.2.3");
        assert_eq!(status, "404 Not Found");
    }

    #[test]
    fn malformed_request_line_does_not_panic() {
        let (status, _body) = route("", "1.2.3");
        assert_eq!(status, "404 Not Found");
    }
}
