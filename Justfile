default: run

# Launch warden against the local deployed-apps fleet
run:
    cargo run -- --apps-dir ~/Projects/deployed-apps

# Run the test suite
test:
    cargo test

# Build the release binary
build:
    cargo build --release

# Build and install into ~/Projects/deployed-apps/warden/
deploy: build
    mkdir -p ~/Projects/deployed-apps/warden/current/bin
    cp target/release/warden ~/Projects/deployed-apps/warden/current/bin/warden
    @echo "Deployed → ~/Projects/deployed-apps/warden/current/bin/warden"

# Static analysis (clippy)
lint:
    cargo clippy

# Remove build artifacts
clean:
    cargo clean

# Assemble the release asset trio (tarball + release.json + SHA256SUMS) into dist/
package version="" target="":
    python3 scripts/build_dist.py --version "{{version}}" --target "{{target}}"

# Changelog-derived release ceremony: guards, version bump, test+build, package, commit, tag.
# Never pushes; publish via .claude/skills/grm-project-release/publish_release.py post-push.
release *ARGS:
    bash scripts/release.sh {{ARGS}}

# seed — grimoire vocabulary recipe (not applicable: warden has no data store)
seed fixture="" env="dev":
    # grimoire:placeholder
    @echo "TODO: replace with the seed command"

# migrate — grimoire vocabulary recipe (not applicable: warden has no schema)
migrate env="dev":
    # grimoire:placeholder
    @echo "TODO: replace with the migrate command"

# smoke — grimoire vocabulary recipe (desktop GUI app: no HTTP surface to curl)
smoke port="3000":
    # grimoire:placeholder
    @echo "TODO: replace with the smoke command"

# stop — grimoire vocabulary recipe (RSS-4 #322)
stop:
    # grimoire:placeholder
    @echo "TODO: replace with the stop command"
