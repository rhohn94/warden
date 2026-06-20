mod app;
mod changelog;
mod config;
mod detector;
mod dump_ui;
mod history;
mod launcher;
mod log_capture;
mod models;
mod notifier;
mod perf;
mod scanner;
mod version_checker;

use app::{App, AppState};
use config::Config;
use history::HistoryStore;
use launcher::Launcher;
use obsidian::AppRunner;
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::sync::Mutex as TokioMutex;
use tracing::info;
use version_checker::VersionChecker;

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "warden=info".into()),
        )
        .init();

    let args: Vec<String> = std::env::args().collect();

    if args.iter().any(|a| a == "--help" || a == "-h") {
        println!("Usage: warden [--apps-dir <path>]... [--refresh <secs>] [--dump-ui]");
        println!();
        println!("Options:");
        println!("  --apps-dir <path>   Directory to watch for deployed apps (repeatable)");
        println!("                      (default: ~/Projects/deployed-apps/)");
        println!("  --refresh <secs>    Scan interval in seconds (default: 5)");
        println!("  --dump-ui           Print a JSON snapshot of app state to stdout and exit");
        println!("                      (no window opened; suitable for scripting and regression tests)");
        return;
    }

    // Load persistent config; CLI args override config values.
    let mut config = Config::load();

    // Collect all --apps-dir values (flag is repeatable).
    let cli_apps_dirs: Vec<&str> = parse_flag_multi(&args, "--apps-dir");

    // Persist the last explicit --apps-dir value (single-value backward-compat).
    if let Some(dir) = cli_apps_dirs.last() {
        config.apps_dir = Some(dir.to_string());
        if let Err(e) = config.save() {
            tracing::warn!("could not save config: {}", e);
        }
    }

    // Build the watched directory list: CLI values win; fall back to config then default.
    let apps_dirs: Vec<PathBuf> = if !cli_apps_dirs.is_empty() {
        cli_apps_dirs.iter().map(PathBuf::from).collect()
    } else if let Some(ref dir) = config.apps_dir {
        vec![PathBuf::from(dir)]
    } else {
        vec![default_apps_dir()]
    };

    if args.iter().any(|a| a == "--dump-ui") {
        if let Err(e) = dump_ui::scan_and_dump(apps_dirs) {
            eprintln!("warden --dump-ui: {}", e);
            std::process::exit(1);
        }
        return;
    }

    // Resolve refresh_secs: CLI flag wins, then config, then default 5.
    let cli_refresh = parse_flag(&args, "--refresh");
    if let Some(val) = cli_refresh {
        if let Ok(secs) = val.parse::<u64>() {
            config.refresh_secs = Some(secs);
            if let Err(e) = config.save() {
                tracing::warn!("could not save config: {}", e);
            }
        }
    }
    let refresh_secs: u64 = config.refresh_secs.unwrap_or(5);

    let notifications_enabled: bool = config.notifications_enabled.unwrap_or(true);
    let log_tail_lines: usize = config.log_tail_lines.unwrap_or(500);
    let version_check_interval_secs: u64 = config.version_check_interval_secs.unwrap_or(3600);

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime failed to build");

    // Build the version checker before constructing AppState so the Arc can be
    // shared into AppState without an extra clone step.
    let version_checker = VersionChecker::new();
    let version_results = version_checker.results();
    let history = Arc::new(std::sync::Mutex::new(HistoryStore::load()));

    let state = Arc::new(std::sync::Mutex::new(AppState::new(
        apps_dirs.clone(),
        refresh_secs,
        notifications_enabled,
        log_tail_lines,
        Arc::clone(&version_results),
        history,
    )));

    // Enter the runtime so tokio::spawn in scanner::start works from the main thread.
    let runtime_guard = runtime.enter();
    let dirs_display: Vec<String> = apps_dirs.iter().map(|d| d.display().to_string()).collect();
    info!("warden v{} starting — watching [{}]", env!("CARGO_PKG_VERSION"), dirs_display.join(", "));
    let (scanner_rx, force_scan_tx) = scanner::start(apps_dirs, Duration::from_secs(refresh_secs));
    let launcher = Arc::new(TokioMutex::new(Launcher::new()));
    let runtime_handle = runtime.handle().clone();

    // Start the background version-check sweep with an empty initial entries list;
    // the checker will pick up real entries on its first sweep after the scanner
    // has populated state.  Pass a clone of the runtime handle.
    version_checker.start(Vec::new(), version_check_interval_secs, runtime_handle.clone());

    let mut app = App::new(state, config, scanner_rx, force_scan_tx, Arc::clone(&launcher), runtime_handle);

    // AppRunner::run blocks until the window is closed.
    if let Err(e) = AppRunner::run(&mut app) {
        eprintln!("warden: event loop error: {e}");
        std::process::exit(1);
    }

    // Drop the runtime guard before calling block_on; entering a runtime
    // from within a runtime context causes a panic.
    drop(runtime_guard);
    runtime.block_on(async {
        launcher.lock().await.shutdown_all().await;
    });
}

fn parse_flag<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|w| w[0] == flag)
        .map(|w| w[1].as_str())
}

/// Collect all values associated with a repeatable flag.
/// `--flag val1 --flag val2` returns `["val1", "val2"]`.
fn parse_flag_multi<'a>(args: &'a [String], flag: &str) -> Vec<&'a str> {
    args.windows(2)
        .filter(|w| w[0] == flag)
        .map(|w| w[1].as_str())
        .collect()
}

fn default_apps_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Projects")
        .join("deployed-apps")
}
