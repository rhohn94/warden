// main.rs — service quick-start entrypoint (#432). REAL and RUNNING: a
// minimal std-only HTTP/1.1 server (no framework dependency — matches the
// version_report.rs zero-dependency philosophy; swap in axum/actix/etc. as
// your service grows) serving `GET /healthz` with the SAME `{status,
// version}` JSON contract the web quick-start template's `just smoke` recipe
// already asserts (docs/web-app-deployment-protocol.md §5), initializes
// standard logging (logging_init.rs — JSON-lines to stdout,
// docs/coding-standards.md §Logging) + config (config.rs), and wires
// `-V`/`--version` to the packaged grimoire-build-info.json stamp
// (build_info.rs, §8). Replace the request router (http.rs) with your real
// service.

mod build_info;
mod config;
mod http;
mod logging_init;

use std::env;
use std::net::TcpListener;

fn main() {
    let cfg = config::Config::load();
    logging_init::init(&cfg.log_level, &logging_init::instance_id(), env!("CARGO_PKG_VERSION"));

    let args: Vec<String> = env::args().collect();
    if args.iter().any(|a| a == "-V" || a == "--version") {
        println!("{}", build_info::version_line(env!("CARGO_PKG_VERSION")));
        return;
    }

    let port: u16 = args
        .iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(cfg.port);

    let addr = format!("127.0.0.1:{port}");
    let listener = TcpListener::bind(&addr).unwrap_or_else(|e| {
        eprintln!("service-app: failed to bind {addr}: {e}");
        std::process::exit(1);
    });
    tracing::info!("service-app listening on {addr}");
    println!(
        "service-app v{} listening on {addr}",
        env!("CARGO_PKG_VERSION")
    );

    for stream in listener.incoming() {
        match stream {
            Ok(s) => http::handle_connection(s, env!("CARGO_PKG_VERSION")),
            Err(e) => tracing::warn!("connection error: {e}"),
        }
    }
}
