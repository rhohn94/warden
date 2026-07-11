// grimoire:placeholder — gatekeeper auth scaffold SEAM (required-feature catalog
// Entry 4, key: adopt-gatekeeper). This is a SCAFFOLD SEAM, not a working auth
// backend: it shows WHERE the app enables gatekeeper's Cargo features and wires
// the store-trait seam with an in-memory reference store for first boot. Replace
// the placeholders with real, persistent stores (recordkeeper — Entry 5 — is the
// default implementation of these traits) before shipping.
//
// Consume path (do NOT re-implement auth in-tree):
//   1. Add gatekeeper to vendor.toml and run `recipe.py sync-deps`:
//        [deps.gatekeeper]
//        repo    = "rhohn94/gatekeeper"
//        channel = "stable"
//        kind    = "vendored-crate"
//        dest    = "lib/third-party/gatekeeper"   # standard structure; never a top-level vendor/
//   2. Depend on the vendored path in Cargo.toml, enabling the feature(s) you need:
//        gatekeeper = { path = "lib/third-party/gatekeeper", features = ["session"] }        # password login + session cookie
//      # gatekeeper = { path = "lib/third-party/gatekeeper", features = ["bearer"] }         # opaque API tokens + scopes
//      # gatekeeper = { path = "lib/third-party/gatekeeper", features = ["session", "bearer"] }
//   3. Implement the store traits below against your database, then mount
//      gatekeeper's FromRequestParts extractors (CurrentUser / AuthUser / AdminUser)
//      and/or bearer middleware in your Axum router.
//
// Design rationale (§5.6) lives in the upstream Grimoire repository
// (framework-internal); required-feature catalog Entry 4 (GK-1..GK-5).

// grimoire:placeholder — feature-gated import seam. Uncomment the trait/type
// imports for the feature(s) you enabled in Cargo.toml. Kept commented so the
// scaffold compiles before gatekeeper is vendored.
//
// #[cfg(feature = "session")]
// use gatekeeper::session::{UserStore, SessionStore};   // password login + server-side sessions
// #[cfg(feature = "bearer")]
// use gatekeeper::bearer::ApiTokenStore;                 // opaque API tokens + Scope model

// grimoire:placeholder — in-memory reference stores for FIRST BOOT ONLY.
// gatekeeper owns the crypto (Argon2id, SHA-256 token hashing, constant-time
// compare) and cookie/token plumbing; the app owns persistence via these traits.
// Swap each `InMemory*Store` for a recordkeeper-backed store (Entry 5) before any
// non-local environment — an in-memory store loses every user/session on restart.

#[allow(dead_code)]
pub struct InMemoryUserStore {
    // grimoire:placeholder — replace with a recordkeeper `Db` handle (db_seam.rs).
}

#[allow(dead_code)]
pub struct InMemorySessionStore {
    // grimoire:placeholder — replace with a recordkeeper-backed session table.
}

#[allow(dead_code)]
pub struct InMemoryApiTokenStore {
    // grimoire:placeholder — replace with a recordkeeper-backed api_token table.
}

// grimoire:placeholder — wiring point. Call this from your router setup once
// gatekeeper is vendored and the stores are implemented. Returns nothing yet —
// it exists to mark the seam, not to run auth.
#[allow(dead_code)]
pub fn wire_gatekeeper_seam() {
    // grimoire:placeholder — e.g.:
    //   let users    = InMemoryUserStore { /* db: db_seam::connect(...)? */ };
    //   let sessions = InMemorySessionStore { /* db */ };
    //   let tokens   = InMemoryApiTokenStore { /* db */ };
    //   let auth = gatekeeper::AuthLayer::builder()
    //       .user_store(users)
    //       .session_store(sessions)      // #[cfg(feature = "session")]
    //       .api_token_store(tokens)      // #[cfg(feature = "bearer")]
    //       .build();
    //   // router.layer(auth) ...
    unimplemented!("gatekeeper auth seam — implement per catalog Entry 4 (GK-1..GK-5)");
}
