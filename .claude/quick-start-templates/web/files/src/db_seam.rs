// grimoire:placeholder — recordkeeper database-access scaffold SEAM (required-feature
// catalog Entry 5, key: adopt-recordkeeper). This is a SCAFFOLD SEAM, not a working
// data layer: it shows WHERE the app opens a recordkeeper `Db` connection, selects a
// backend, and honors the APP_ENV / DSN-per-env convention. Replace the placeholders
// with a real connection + migration run before shipping.
//
// Consume path (do NOT wire turso/sqlx directly):
//   1. Add recordkeeper to vendor.toml and run `recipe.py sync-deps`:
//        [deps.recordkeeper]
//        repo    = "rhohn94/recordkeeper"
//        channel = "stable"
//        kind    = "vendored-crate"
//        dest    = "lib/third-party/recordkeeper"   # standard structure; never a top-level vendor/
//   2. Depend on the vendored path in Cargo.toml. Turso is the DEFAULT backend
//      (embedded, single-binary, zero-daemon); Postgres is opt-in:
//        recordkeeper = { path = "lib/third-party/recordkeeper" }                        # turso (default feature)
//      # recordkeeper = { path = "lib/third-party/recordkeeper", features = ["postgres"], default-features = false }
//   3. Author per-backend migration sets under migrations/<backend>/ (see the
//      skeleton this template scaffolds) and run them via recordkeeper's `migrate`.
//
// Environment / DSN convention: the active environment is selected at runtime via
// APP_ENV per the `environments` block; each environment supplies its own DSN
// (design rationale §1-§2 in the upstream Grimoire repository,
// framework-internal). recordkeeper applies the SQLite-family pragma baseline
// (WAL / busy_timeout / synchronous=NORMAL / foreign_keys) on the turso backend
// automatically.
//
// Key constraint (documented, not to be worked around): Turso has no SQLx driver, so
// the portable API (connect/execute/query/transaction/migrate) is RUNTIME-checked, not
// sqlx::query! compile-time-checked; a Postgres-only SQLx escape hatch exists. SQL
// dialect differs per backend — hence per-backend migration dirs + a portable subset.
//
// Design rationale (§5.7) lives in the upstream Grimoire repository
// (framework-internal); required-feature catalog Entry 5 (RK-1..RK-6).

// grimoire:placeholder — import seam. Uncomment once recordkeeper is vendored.
// use recordkeeper::{Db, DbError};

// grimoire:placeholder — resolve the DSN for the active environment. Reads APP_ENV
// (default "local") and returns the matching environment's DSN. Replace the stub
// bodies with a real read of your `environments` config / env vars.
#[allow(dead_code)]
pub fn dsn_for_active_env() -> String {
    let app_env = std::env::var("APP_ENV").unwrap_or_else(|_| "local".to_string());
    // grimoire:placeholder — map app_env -> DSN per the `environments` block.
    // Turso default example: "file:./data/<app_env>.db" (embedded, per-env file);
    // Postgres example:      std::env::var("DATABASE_URL") for the postgres backend.
    let _ = app_env;
    unimplemented!("recordkeeper DSN resolution — implement per catalog Entry 5 (RK-4)");
}

// grimoire:placeholder — the `Db` connect point. Call this at app startup, then run
// migrations for the active backend before serving. Returns the connected handle you
// pass into gatekeeper's stores (auth_seam.rs) and your own repositories.
#[allow(dead_code)]
pub fn connect() /* -> Result<Db, DbError> */ {
    // grimoire:placeholder — e.g.:
    //   let dsn = dsn_for_active_env();
    //   let db = recordkeeper::connect(&dsn)?;        // turso default; pragma baseline applied
    //   recordkeeper::migrate(&db, "migrations")?;    // applies the per-backend set for this backend
    //   Ok(db)
    unimplemented!("recordkeeper Db connect seam — implement per catalog Entry 5 (RK-1..RK-6)");
}
