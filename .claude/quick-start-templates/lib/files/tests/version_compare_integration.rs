// tests/version_compare_integration.rs — an integration test exercising the
// crate's PUBLIC API only (via `lib_app::...`), matching the standard
// structure's tests/ convention for a fresh Cargo library scaffold. Unit
// tests (src/version_compare.rs) and doctests (`cargo test --doc`) cover the
// module's internals + examples; this proves the crate is usable exactly as
// a real consumer would use it — imported by name, nothing internal reached.

use lib_app::version_compare::{compare, format_token, parse};
use std::cmp::Ordering;

#[test]
fn public_api_round_trip() {
    assert_eq!(parse("v3.94"), Some((3, 94, 0)));
    assert_eq!(compare("v3.94", "v3.93"), Some(Ordering::Greater));
    assert_eq!(format_token((3, 94, 0)), "v3.94.0");
}

#[test]
fn public_api_sorts_a_real_version_list() {
    let mut versions = vec!["v3.9", "v3.10", "v3.2", "v3.93"];
    versions.sort_by(|a, b| compare(a, b).expect("valid version tokens"));
    assert_eq!(versions, vec!["v3.2", "v3.9", "v3.10", "v3.93"]);
}
