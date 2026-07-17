// main.rs — cli quick-start entrypoint (#432). REAL and RUNNING: parses real
// subcommands via clap, loads config (config.rs), initializes standard
// logging (logging_init.rs — JSON-lines to stdout, docs/coding-standards.md
// §Logging), and wires `--version`/`-V` to the packaged
// grimoire-build-info.json stamp (build_info.rs,
// docs/web-app-deployment-protocol.md §8) instead of clap's default
// crate-version-only flag. Replace the `greet` subcommand with your
// project's real commands.

mod build_info;
mod config;
mod logging_init;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "cli-app",
    about = "Grimoire cli quick-start scaffold — replace with your project's description.",
    disable_version_flag = true
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,

    /// Print version + build provenance (wired to grimoire-build-info.json,
    /// §8) and exit.
    #[arg(short = 'V', long = "version")]
    version: bool,
}

#[derive(Subcommand)]
enum Commands {
    /// A real, working example subcommand — replace with your project's own.
    Greet {
        /// Name to greet.
        #[arg(default_value = "world")]
        name: String,
    },
}

fn main() {
    let cli = Cli::parse();
    let cfg = config::Config::load();
    logging_init::init(&cfg.log_level, &logging_init::instance_id(), env!("CARGO_PKG_VERSION"));

    if cli.version {
        println!("{}", build_info::version_line(env!("CARGO_PKG_VERSION")));
        return;
    }

    match cli.command {
        Some(Commands::Greet { name }) => {
            tracing::info!("running the greet command for {name}");
            println!("Hello, {name}! (cli-app v{})", env!("CARGO_PKG_VERSION"));
        }
        None => {
            println!(
                "cli-app v{} — run with --help for commands, -V/--version for build provenance.",
                env!("CARGO_PKG_VERSION")
            );
        }
    }
}
