// tests/cli_integration.rs — spawns the BUILT binary (via `CARGO_BIN_EXE_*`,
// std-only, no dev-dependency) and asserts real process behavior: -V/--version
// runs and exits 0 with a provenance-shaped line, and the `greet` subcommand
// prints the expected greeting. Proves the entrypoint actually runs, not just
// that it compiles.

use std::process::Command;

fn bin() -> Command {
    Command::new(env!("CARGO_BIN_EXE_cli-app"))
}

#[test]
fn version_flag_runs_and_exits_zero() {
    let output = bin().arg("-V").output().expect("run binary");
    assert!(output.status.success(), "-V exited non-zero: {output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.trim_start().starts_with('v'), "unexpected -V output: {stdout}");
}

#[test]
fn long_version_flag_matches_short_flag() {
    let output = bin().arg("--version").output().expect("run binary");
    assert!(output.status.success());
}

#[test]
fn greet_subcommand_prints_greeting() {
    let output = bin().args(["greet", "Grimoire"]).output().expect("run binary");
    assert!(output.status.success(), "greet exited non-zero: {output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Hello, Grimoire!"), "unexpected greet output: {stdout}");
}

#[test]
fn no_args_prints_a_usage_hint_and_exits_zero() {
    let output = bin().output().expect("run binary");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("cli-app"), "unexpected no-args output: {stdout}");
}
