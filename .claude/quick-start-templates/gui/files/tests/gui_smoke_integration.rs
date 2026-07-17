// tests/gui_smoke_integration.rs — spawns the BUILT binary with --smoke-test
// (boots real app state + renders one headless egui frame, never opens a
// native window) and asserts a clean exit. Opening the REAL native window
// (`just run`, no flag) has been manually verified in a windowed environment
// — see template.json post-apply-notes for what that means as a seam; a
// display-dependent open cannot be asserted deterministically in a headless
// test runner.

use std::process::Command;

fn bin() -> Command {
    Command::new(env!("CARGO_BIN_EXE_gui-app"))
}

#[test]
fn smoke_test_flag_boots_and_exits_zero() {
    let output = bin().arg("--smoke-test").output().expect("run binary");
    assert!(output.status.success(), "smoke-test exited non-zero: {output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("smoke: OK"), "unexpected output: {stdout}");
}

#[test]
fn version_flag_runs_and_exits_zero() {
    let output = bin().arg("-V").output().expect("run binary");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.trim_start().starts_with('v'), "unexpected -V output: {stdout}");
}

// gui-test flag (#362): spawns the built binary against the committed
// tests/gui-baselines/main.snapshot baseline and asserts a clean exit — the
// process-spawning counterpart to the in-process describe_matches_committed_
// baseline unit test in src/main.rs (which checks App::describe() directly
// without a subprocess).
#[test]
fn gui_test_flag_matches_committed_baseline() {
    let output = bin().args(["--gui-test", "main"]).output().expect("run binary");
    assert!(output.status.success(), "gui-test exited non-zero: {output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("gui-test: PASS"), "unexpected output: {stdout}");
}

#[test]
fn gui_test_flag_fails_loud_on_missing_baseline() {
    let output = bin()
        .args(["--gui-test", "no-such-baseline-#362"])
        .output()
        .expect("run binary");
    assert!(!output.status.success(), "missing baseline must be a hard failure, not a silent pass");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("FAIL"), "unexpected output: {stderr}");
}
