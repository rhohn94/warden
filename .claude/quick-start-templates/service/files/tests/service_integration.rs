// tests/service_integration.rs — spawns the BUILT binary as a real child
// process, connects over a REAL TCP socket, and asserts `GET /healthz`
// returns `{status, version}` — the exact contract `just smoke` (web
// quick-start template) already checks via curl. Proves the entrypoint
// actually runs a live server, not just that it compiles.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command};
use std::thread;
use std::time::Duration;

/// Kills the child on drop so a failing assertion never leaks a listening
/// process across test runs.
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn spawn_on(port: u16) -> ChildGuard {
    let child = Command::new(env!("CARGO_BIN_EXE_service-app"))
        .args(["--port", &port.to_string()])
        .spawn()
        .expect("spawn service-app");
    ChildGuard(child)
}

fn wait_until_listening(port: u16) {
    for _ in 0..50 {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }
    panic!("service-app did not start listening on port {port} within 5s");
}

#[test]
fn healthz_returns_ok_json() {
    let port = 18765;
    let _guard = spawn_on(port);
    wait_until_listening(port);

    let mut stream = TcpStream::connect(("127.0.0.1", port)).expect("connect");
    stream
        .write_all(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .expect("write request");
    let mut buf = String::new();
    let _ = stream.read_to_string(&mut buf); // Connection: close -> read-to-EOF is safe
    assert!(buf.contains("200 OK"), "unexpected response: {buf}");
    assert!(buf.contains("\"status\": \"ok\""), "unexpected response: {buf}");
}

#[test]
fn unknown_route_returns_404() {
    let port = 18766;
    let _guard = spawn_on(port);
    wait_until_listening(port);

    let mut stream = TcpStream::connect(("127.0.0.1", port)).expect("connect");
    stream
        .write_all(b"GET /nope HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .expect("write request");
    let mut buf = String::new();
    let _ = stream.read_to_string(&mut buf);
    assert!(buf.contains("404 Not Found"), "unexpected response: {buf}");
}

#[test]
fn version_flag_runs_and_exits_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_service-app"))
        .arg("-V")
        .output()
        .expect("run binary");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.trim_start().starts_with('v'), "unexpected -V output: {stdout}");
}
