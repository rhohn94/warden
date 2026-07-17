# service-app

A real, running backend service: a std-only HTTP/1.1 server exposing `GET
/healthz` (the same `{status, version}` contract the web quick-start
template's `just smoke` recipe checks), config loading (`config.rs`),
standard logging init (`env_logger`/`log`), and `-V`/`--version` wired to the
packaged `grimoire-build-info.json` build-provenance stamp — plus the
`package`/`release`/`deploy` recipes from the release-automation starter pack
(#431), and a Dockerized-Postgres `db-up`/`db-down` pair ready for when you
wire real persistence.

This is a Grimoire quick-start scaffold for the `service` profile. Replace
this README, the crate name (`Cargo.toml`), and the request router
(`src/http.rs`) with your project's own. Run `just --list` for every recipe;
`just run` to start the server; `just smoke` to probe `/healthz`; `just test`
to run the unit + integration tests; `just package` to build a distributable
bundle in `dist/`.

See `template.json`'s `post-apply-notes` for exactly what runs out of the box
vs. what is a seam to fill in.
