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
