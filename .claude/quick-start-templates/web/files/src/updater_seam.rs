// grimoire:placeholder — meta-updater self-update scaffold SEAM (required-feature
// catalog Entry 6, key: adopt-meta-updater). This is a SCAFFOLD SEAM, not a
// working self-updater. As of catalog-version 6 the meta-updater crate itself
// is SPEC-ONLY (docs/grimoire/design/meta-updater-package-design.md) — there is
// no published release to vendor yet. This file marks the intended wiring point
// so the app's adoption work is a drop-in once the crate ships.
//
// meta-updater is the standard package for web-app health-gated self-update
// with auto-rollback (web-app-deployment-protocol.md §6): trait ReleaseChannel
// (default GithubChannel), a checksum-then-minisign verify-then-swap pipeline,
// allowlisted asset names, a fixed staging dir, atomic rename, N-1 rollback, and
// an UpdatePolicy enum ({Disabled, PromptOnly, AutoWithinChannel}) with a
// two-tier operator-vs-service default (service binaries default Disabled).
//
// Consume path (once meta-updater is published):
//   1. Add meta-updater to vendor.toml and run `recipe.py sync-deps`:
//        [deps.meta-updater]
//        repo    = "rhohn94/meta-updater"
//        channel = "stable"
//        kind    = "vendored-crate"
//        dest    = "lib/third-party/meta-updater"   # standard structure; never a top-level vendor/
//   2. Depend on the vendored path in Cargo.toml:
//        meta-updater = { path = "lib/third-party/meta-updater" }
//   3. Wire a ReleaseChannel (GithubChannel by default) and an explicit
//      UpdatePolicy for this deployment context (service contexts MUST default
//      to Disabled unless an operator has opted into AutoWithinChannel).
//   4. Retire any in-tree check/download/verify/apply/rollback implementation.
//
// Full trait surface + invariants: docs/grimoire/design/meta-updater-package-design.md.
// Standard-package authority: docs/grimoire/design/web-app-support-design.md §5.8;
//   required-feature catalog Entry 6 (MU-1..MU-5).
// §6 mandate this package satisfies: docs/web-app-deployment-protocol.md §6.

// grimoire:placeholder — import seam. Uncomment once meta-updater is vendored.
// use meta_updater::{GithubChannel, UpdatePolicy, Updater};

// grimoire:placeholder — self-update wiring point. Constructs the channel +
// policy and drives the check/verify/apply/health-gate/rollback sequence.
// Returns nothing yet — it marks the seam, not a working updater.
#[allow(dead_code)]
pub fn wire_updater_seam() {
    // grimoire:placeholder — e.g.:
    //   let channel = GithubChannel { repo: "your-org/your-app".into(), channel: "stable".into() };
    //   let policy = UpdatePolicy::Disabled; // service-tier default; PromptOnly for operator contexts
    //   let updater = Updater::new(channel, policy);
    //   updater.check_and_apply()?; // health-gates + auto-rolls-back internally
    unimplemented!("meta-updater self-update seam — catalog Entry 6 (MU-1..MU-5); crate not yet published, see docs/grimoire/design/meta-updater-package-design.md");
}
