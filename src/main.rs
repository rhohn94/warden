mod app;
mod detector;
mod dump_ui;
mod launcher;
mod models;
mod scanner;

use app::{App, AppState};
use launcher::Launcher;
use obsidian::AppRunner;
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::sync::Mutex as TokioMutex;
use tracing::info;

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "warden=info".into()),
        )
        .init();

    let args: Vec<String> = std::env::args().collect();

    if args.iter().any(|a| a == "--help" || a == "-h") {
        println!("Usage: warden [--apps-dir <path>] [--refresh <secs>] [--dump-ui]");
        println!();
        println!("Options:");
        println!("  --apps-dir <path>   Directory to watch for deployed apps");
        println!("                      (default: ~/Projects/deployed-apps/)");
        println!("  --refresh <secs>    Scan interval in seconds (default: 5)");
        println!("  --dump-ui           Print a JSON snapshot of app state to stdout and exit");
        println!("                      (no window opened; suitable for scripting and regression tests)");
        return;
    }

    let apps_dir = parse_flag(&args, "--apps-dir")
        .map(PathBuf::from)
        .unwrap_or_else(default_apps_dir);

    if args.iter().any(|a| a == "--dump-ui") {
        if let Err(e) = dump_ui::scan_and_dump(apps_dir) {
            eprintln!("warden --dump-ui: {}", e);
            std::process::exit(1);
        }
        return;
    }

    let refresh_secs: u64 = parse_flag(&args, "--refresh")
        .and_then(|v| v.parse().ok())
        .unwrap_or(5);

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime failed to build");

    let state = Arc::new(std::sync::Mutex::new(AppState::new(
        apps_dir.clone(),
        refresh_secs,
    )));

    // Enter the runtime so tokio::spawn in scanner::start works from the main thread.
    let _runtime_guard = runtime.enter();
    info!("warden v{} starting — watching {}", env!("CARGO_PKG_VERSION"), apps_dir.display());
    let (scanner_rx, force_scan_tx) = scanner::start(apps_dir, Duration::from_secs(refresh_secs));
    let launcher = Arc::new(TokioMutex::new(Launcher::new()));
    let runtime_handle = runtime.handle().clone();
    let mut app = App::new(state, scanner_rx, force_scan_tx, launcher, runtime_handle);

    // AppRunner::run blocks until the window is closed.
    if let Err(e) = AppRunner::run(&mut app) {
        eprintln!("warden: event loop error: {e}");
        std::process::exit(1);
    }
}

fn parse_flag<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|w| w[0] == flag)
        .map(|w| w[1].as_str())
}

fn default_apps_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Projects")
        .join("deployed-apps")
}
