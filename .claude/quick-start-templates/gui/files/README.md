# gui-app

A real, running desktop application: an egui/eframe window shell that
actually opens (`just run`), config loading (`config.rs`), standard logging
init (`env_logger`/`log`), and `-V`/`--version` wired to the packaged
`grimoire-build-info.json` build-provenance stamp — plus the `package`/
`release`/`deploy` recipes from the release-automation starter pack (#431).

This is a Grimoire quick-start scaffold for the `gui` profile. Replace this
README, the crate name (`Cargo.toml`), and the `App` UI (`src/main.rs`) with
your project's own; pairs with `design-language-adapt` + `ux-demo-build` for
the UX tier. Run `just --list` for every recipe; `just run` to open the real
window; `just smoke` for a display-free liveness check; `just test` to run
the unit + smoke integration tests; `just package` to build a distributable
bundle in `dist/`.

See `template.json`'s `post-apply-notes` for exactly what runs out of the box
vs. what is a seam to fill in.
