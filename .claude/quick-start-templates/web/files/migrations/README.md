<!-- grimoire:placeholder — recordkeeper per-backend migration-set SKELETON
     (required-feature catalog Entry 5, key: adopt-recordkeeper). Scaffold only. -->

# Migrations (recordkeeper per-backend sets)

recordkeeper is a **dual-backend** data-access layer: **Turso** (embedded — the
default backend) and **PostgreSQL** (opt-in via the `postgres` feature). Because
Turso has no SQLx driver and SQL dialect differs per backend, migrations live in
**per-backend directories** with a documented **portable subset** for statements
that are identical across both.

```
migrations/
  turso/        # default backend — SQLite-family DDL
  postgres/     # opt-in backend — PostgreSQL DDL
```

This is a **scaffold skeleton**. Author your real migrations here and run them
via recordkeeper's `migrate` (or the `recipe.py migrate` recipe, aligned with
recordkeeper's per-backend sets — recipe-spec work, #201). Naming/ordering follow
recordkeeper's own convention (see the vendored crate's docs once synced).

- **Turso (default):** `migrations/turso/` — the SQLite-family pragma baseline
  (WAL / `busy_timeout` / `synchronous=NORMAL` / `foreign_keys`) is applied by
  recordkeeper on connect; your migrations carry only schema DDL.
- **Postgres (opt-in):** `migrations/postgres/` — used when the app enables the
  `postgres` feature; a Postgres-only SQLx escape hatch is available for
  statements outside the portable subset.

Design rationale (§5.7; environment / DSN convention §1–§2) lives in the
upstream Grimoire repository (framework-internal — not shipped). The `Db`
connect + migration-run wiring point is `src/db_seam.rs`.
