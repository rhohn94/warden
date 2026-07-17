# cli-app

A real, running command-line tool: clap-based argument parsing, config
loading (`config.rs`), standard logging init (`env_logger`/`log`), and
`-V`/`--version` wired to the packaged `grimoire-build-info.json` build-
provenance stamp — plus the `package`/`release` recipes from the release-
automation starter pack (#431).

This is a Grimoire quick-start scaffold for the `cli` profile. Replace this
README, the crate name (`Cargo.toml`), and the `greet` example subcommand
(`src/main.rs`) with your project's own. Run `just --list` for every recipe;
`just build && just run` to try it; `just test` to run the unit + integration
tests; `just package` to build a distributable bundle in `dist/`.

See `template.json`'s `post-apply-notes` for exactly what runs out of the box
vs. what is a seam to fill in.
