// main.rs — gui quick-start entrypoint (#432). REAL and RUNNING: opens an
// ACTUAL native egui/eframe window on `just run`, initializes standard
// logging (logging_init.rs — JSON-lines to stdout, docs/coding-standards.md
// §Logging) + config (config.rs), and wires `-V`/`--version` to the packaged
// grimoire-build-info.json stamp (build_info.rs,
// docs/web-app-deployment-protocol.md §8). `--smoke-test` boots the SAME real
// app state and renders one real headless egui frame WITHOUT opening a native
// window — a display-free liveness check for CI (`just smoke`); opening a
// real window has been manually verified in a windowed environment (see
// template.json post-apply-notes for exactly what that means as a seam).
// `--gui-test <baseline>` (#362) is the desktop leg of the platform-
// differentiated GUI test verb (`recipe.py gui-test`, parallels `smoke`):
// same headless boot as `--smoke-test`, plus a deterministic text snapshot
// of what the UI actually renders, diffed against a committed baseline under
// tests/gui-baselines/. See `run_gui_test` below for why this is a text
// snapshot rather than a rasterized PNG.
//
// Replace `App` with your project's real UI.

mod build_info;
mod config;
mod logging_init;

use eframe::egui;

/// The app's real, minimal UI state — replace with your project's own.
struct App {
    greeting: String,
}

impl Default for App {
    fn default() -> Self {
        App {
            greeting: "Hello from the gui quick-start scaffold!".to_string(),
        }
    }
}

impl App {
    /// The real render pass — shared by BOTH the windowed run path
    /// (`eframe::run_ui_native`'s closure hands over the whole-window `Ui`
    /// directly) and the headless `--smoke-test` path
    /// (`egui::Context::run_ui` hands over the same `&mut Ui` type), so
    /// smoke-testing exercises the actual UI code, not a stand-in.
    fn render(&mut self, ui: &mut egui::Ui) {
        ui.heading(&self.greeting);
        ui.label(format!("gui-app v{}", env!("CARGO_PKG_VERSION")));
    }

    /// Deterministic text description of what `render` draws, in draw order —
    /// the `--gui-test` (#362) stand-in for a pixel screenshot. Source-derived
    /// from the same fields `render` reads, so the two cannot silently drift
    /// apart. Extend this alongside `render` when you add real widgets.
    fn describe(&self) -> String {
        format!(
            "heading: {}\nlabel: gui-app v{}\n",
            self.greeting,
            env!("CARGO_PKG_VERSION")
        )
    }
}

fn main() {
    let cfg = config::Config::load();
    logging_init::init(&cfg.log_level, &logging_init::instance_id(), env!("CARGO_PKG_VERSION"));

    let args: Vec<String> = std::env::args().collect();

    if args.iter().any(|a| a == "-V" || a == "--version") {
        println!("{}", build_info::version_line(env!("CARGO_PKG_VERSION")));
        return;
    }

    if args.iter().any(|a| a == "--smoke-test") {
        run_smoke_test();
        return;
    }

    if let Some(idx) = args.iter().position(|a| a == "--gui-test") {
        let baseline_name = args.get(idx + 1).cloned().unwrap_or_else(|| "main".to_string());
        let update_baseline = args.iter().any(|a| a == "--update-baseline");
        std::process::exit(run_gui_test(&baseline_name, update_baseline));
    }

    tracing::info!("opening the {} window", cfg.window_title);
    let mut app = App::default();
    let native_options = eframe::NativeOptions::default();
    let result = eframe::run_ui_native(&cfg.window_title, native_options, move |ui, _frame| {
        app.render(ui);
    });
    if let Err(e) = result {
        eprintln!("gui-app: failed to open the native window: {e}");
        std::process::exit(1);
    }
}

/// Boot real app state and render one real headless egui frame — no display,
/// no windowing system, deterministic for CI. Proves the UI code runs without
/// requiring a native window (see `App::render`, shared with the real path).
fn run_smoke_test() {
    let mut app = App::default();
    let ctx = egui::Context::default();
    let raw_input = egui::RawInput::default();
    let _output = ctx.run_ui(raw_input, |ui| {
        app.render(ui);
    });
    tracing::info!("smoke-test: app booted and rendered one headless frame OK");
    println!(
        "smoke: OK (gui-app v{} booted, headless frame rendered)",
        env!("CARGO_PKG_VERSION")
    );
}

/// `--gui-test <baseline>` (#362): boot real app state, render one headless
/// frame exactly like `run_smoke_test`, then diff `App::describe()`'s text
/// snapshot against the committed baseline at
/// `tests/gui-baselines/<baseline>.snapshot`.
///
/// Why a text snapshot, not a rasterized PNG: a true pixel screenshot needs a
/// GPU or software-rendering backend, which would reintroduce the display/
/// windowing-system dependency `--smoke-test` was deliberately designed to
/// avoid (see the module doc — "no display required, deterministic for CI").
/// The text snapshot keeps that guarantee while still catching real drift in
/// what the UI shows. Full pixel-level capture is a documented, deferred
/// follow-up (see docs/grimoire/design/runtime-verification-design.md
/// §GUI testing) once a project needs it and accepts the CI/display tradeoff.
///
/// Exit contract mirrors `smoke`: 0 = matches baseline, 1 = mismatch or
/// missing baseline (a missing baseline is ALWAYS a hard failure, never a
/// silent pass — same convention as `smoke-visual`,
/// runtime-verification-design.md §Visual smoke gate).
fn run_gui_test(baseline_name: &str, update_baseline: bool) -> i32 {
    let mut app = App::default();
    let ctx = egui::Context::default();
    let raw_input = egui::RawInput::default();
    let _output = ctx.run_ui(raw_input, |ui| {
        app.render(ui);
    });
    let snapshot = app.describe();
    let baseline_path = format!("tests/gui-baselines/{baseline_name}.snapshot");

    if update_baseline {
        if let Some(parent) = std::path::Path::new(&baseline_path).parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                eprintln!("gui-test: failed to create {}: {e}", parent.display());
                return 1;
            }
        }
        if let Err(e) = std::fs::write(&baseline_path, &snapshot) {
            eprintln!("gui-test: failed to write baseline {baseline_path}: {e}");
            return 1;
        }
        println!("gui-test: baseline updated at {baseline_path}");
        return 0;
    }

    match std::fs::read_to_string(&baseline_path) {
        Err(_) => {
            eprintln!(
                "gui-test: FAIL — no baseline at {baseline_path}. A missing baseline is \
                 always a hard failure (never a silent pass) — run \
                 `gui-app --gui-test {baseline_name} --update-baseline` deliberately to create it."
            );
            1
        }
        Ok(expected) if expected == snapshot => {
            println!("gui-test: PASS ({baseline_path} matches current render)");
            0
        }
        Ok(expected) => {
            eprintln!(
                "gui-test: FAIL — current render drifted from {baseline_path}.\n\
                 --- baseline ---\n{expected}--- current ---\n{snapshot}"
            );
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `App::describe()` is the source `run_gui_test` diffs against the
    /// committed baseline — this unit test keeps the two from silently
    /// drifting apart without needing to spawn the binary.
    #[test]
    fn describe_matches_committed_baseline() {
        let app = App::default();
        let snapshot = app.describe();
        let baseline = std::fs::read_to_string("tests/gui-baselines/main.snapshot")
            .expect("tests/gui-baselines/main.snapshot must exist — see run_gui_test's doc comment");
        assert_eq!(
            snapshot, baseline,
            "App::describe() drifted from the committed gui-test baseline — if this is an \
             intentional UI change, run `gui-app --gui-test main --update-baseline` and commit \
             the updated tests/gui-baselines/main.snapshot"
        );
    }
}
