// grimoire:placeholder — token-bookkeeper cost/throughput surface scaffold SEAM
// (required-feature catalog Entry 3, key: adopt-token-bookkeeper). OPTIONAL and
// AGENTIC-ONLY: this seam is relevant only to a web app that runs its own
// agentic/LLM workloads and wants to surface its own token cost/throughput
// (config: web-app.agentic == "yes"). A static or non-agentic app should delete
// this file. This is a SCAFFOLD SEAM, not a working telemetry surface.
//
// token-bookkeeper is the standard package for agentic token/cost/throughput
// bookkeeping. It consumes the framework's canonical run.json artifacts
// (.claude/cache/runs/<id>.json — the PUBLISHED input contract, do NOT re-derive
// its shape) and computes rollups (pass_rate / cost_per_passed_item /
// items_per_hour) via `compute_rollups`, rather than the app re-deriving cost.
//
// Consume path:
//   1. Add token-bookkeeper to vendor.toml and run `recipe.py sync-deps`:
//        [deps.token-bookkeeper]
//        repo    = "rhohn94/token-bookkeeper"
//        channel = "stable"
//        kind    = "vendored-crate"
//        dest    = "lib/third-party/token-bookkeeper"   # standard structure; never a top-level vendor/
//   2. Depend on the vendored path in Cargo.toml:
//        token-bookkeeper = { path = "lib/third-party/token-bookkeeper" }
//   3. Point it at your run.json directory and render the rollups on a cost surface.
//
// run.json schema authority (§A schema / §F the "do not drift; published
//   contract" producer obligation) and standard-package authority (§5.5) live
//   in the upstream Grimoire repository (framework-internal); required-feature
//   catalog Entry 3 (TB-1..TB-4).

// grimoire:placeholder — import seam. Uncomment once token-bookkeeper is vendored.
// use token_bookkeeper::compute_rollups;

// grimoire:placeholder — cost/throughput surface wiring point. Reads run.json
// artifacts and returns rollups to render on an operator/cost surface. Returns
// nothing yet — it marks the seam, not a working surface.
#[allow(dead_code)]
pub fn wire_cost_surface_seam() {
    // grimoire:placeholder — e.g.:
    //   let runs_dir = ".claude/cache/runs";               // canonical run.json location
    //   let rollups = token_bookkeeper::compute_rollups(runs_dir)?;
    //   // render rollups.pass_rate / cost_per_passed_item / items_per_hour ...
    unimplemented!("token-bookkeeper cost surface seam — agentic apps only, catalog Entry 3 (TB-1..TB-4)");
}
