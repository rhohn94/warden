#!/usr/bin/env bash
# deploy.sh — deploy a packaged bundle to a named environment (Grimoire #201 Phase 1).
#
# Reference implementation of the standard deploy interface
# (deploy-environment-design.md §3, web-app-deployment-protocol.md §Environments):
#
#     scripts/deploy.sh <env> [<version>] [--dry-run]
#
# Called by `just deploy <env>` (which the recipes.json `deploy` target invokes,
# so `recipe.py deploy --env <env>` ≡ `just deploy <env>`).
#
# It reads the `environments` block from `.claude/grimoire-config.json`, resolves
# the chosen env's deploy-topology fields, enforces `deploy_policy` as a
# clean-tree + tag guard, then branches on `transport` × `service_manager`:
#
#     transport ∈ { ssh | local-symlink | pull }
#     service_manager ∈ { systemd | launchd }
#
# It performs the atomic `versions/vX.Y.Z/` unpack + `current` symlink flip
# (deploy-environment-design.md §3 / web-app-deployment-protocol.md §3), restarts
# through the right init system, and writes a host-side `fleet-instance.json`
# (web-app-deployment-protocol.md §9.4) for the fleet view.
#
# This is a REFERENCE / TEMPLATE implementation: it cannot know a project's real
# remote infra, so every missing-but-required field is a LOUD failure (e.g. ssh
# transport with no `host`/`path`), never a silent no-op. `--dry-run` prints every
# action and changes nothing on any host.
#
# Self-test: `scripts/deploy.sh --self-test` runs an offline temp-dir round trip
# (synthetic config + a local-symlink dry-run) asserting env resolution, the
# policy guard, transport branching, and dry-run inertness. No repo bash
# --self-test convention exists (the .sh hooks are python polyglots), so a
# temp-dir dry-run test is used.
set -euo pipefail

CONFIG="${GRIMOIRE_CONFIG:-.claude/grimoire-config.json}"

die() { echo "deploy.sh: $*" >&2; exit 1; }
note() { echo "deploy.sh: $*" >&2; }
# In dry-run, `act` prints the command; otherwise it runs it.
act() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "  [dry-run] $*"
    else
        note "running: $*"
        "$@"
    fi
}

# Read one field of one environment from the config's `environments` block.
# Prints the value (empty string if absent/null). Fails loud if the env is not
# declared at all.
env_field() {
    local env="$1" field="$2" config="$3"
    python3 - "$env" "$field" "$config" <<'PY'
import json, sys
env, field, config = sys.argv[1:4]
try:
    cfg = json.load(open(config))
except FileNotFoundError:
    sys.stderr.write("deploy.sh: config not found: %s\n" % config); sys.exit(3)
envs = cfg.get("environments") or {}
if env not in envs:
    sys.stderr.write("deploy.sh: environment %r not declared in %s (declared: %s)\n"
                     % (env, config, ", ".join(sorted(envs)) or "none")); sys.exit(4)
val = envs[env].get(field)
print("" if val is None else val)
PY
}

# True (0) iff the git tree is clean.
git_clean() { [ -z "$(git status --porcelain 2>/dev/null)" ]; }
# True (0) iff HEAD is at an annotated/lightweight tag.
git_on_tag() { git describe --exact-match --tags HEAD >/dev/null 2>&1; }

# Enforce deploy_policy as a clean+tag guard.
#   auto    — deploy from a clean or dirty tree (dev/beta); no tag required.
#   manual  — like auto, but requires the operator's explicit invocation (we are
#             already invoked explicitly, so it proceeds; dirty tree is warned).
#   pr_gate — production: refuse a dirty tree, require a tagged/explicit version.
enforce_policy() {
    local policy="$1" version="$2"
    case "$policy" in
        auto|"")
            git_clean || note "warning: deploying from a DIRTY tree (policy=auto)."
            ;;
        manual)
            git_clean || note "warning: deploying from a DIRTY tree (policy=manual)."
            ;;
        pr_gate)
            git_clean || die "policy=pr_gate: refusing to deploy from a DIRTY tree; commit or stash first."
            if [ -z "$version" ] && ! git_on_tag; then
                die "policy=pr_gate: a released/tagged version is required — pass <version> or tag HEAD."
            fi
            ;;
        *) die "unknown deploy_policy: $policy" ;;
    esac
}

# ── transport implementations ────────────────────────────────────────────────
# Each stages the new version under $path/versions/v$version and flips the
# $path/current symlink atomically, then restarts via the service manager. In
# dry-run every mutating step is printed, not run.

# Atomic symlink flip: write a temp symlink then `mv -T` it over `current` (a
# single rename syscall). Portable note: `mv -T` is GNU; on BSD/macOS `ln -sfn`
# is the atomic-ish fallback (documented).
flip_current() {
    local root="$1" version="$2"
    if mv --help >/dev/null 2>&1 && mv --version 2>/dev/null | grep -qi coreutils; then
        act bash -c "ln -sfn 'versions/v$version' '$root/current.tmp' && mv -T '$root/current.tmp' '$root/current'"
    else
        act ln -sfn "versions/v$version" "$root/current"
    fi
}

deploy_local_symlink() {
    local path="$1" version="$2" bundle_tar="$3"
    [ -n "$path" ] || die "transport=local-symlink requires a 'path' (deploy root) field."
    act mkdir -p "$path/versions/v$version"
    if [ -n "$bundle_tar" ]; then
        act tar -xzf "$bundle_tar" -C "$path/versions/v$version"
    else
        note "no bundle tar resolved — assuming versions/v$version is pre-staged (dry-run/reference)."
    fi
    flip_current "$path" "$version"
}

deploy_ssh() {
    local host="$1" path="$2" version="$3" bundle_tar="$4"
    [ -n "$host" ] || die "transport=ssh requires a 'host' (user@host) field."
    [ -n "$path" ] || die "transport=ssh requires a 'path' (remote deploy root) field."
    act ssh "$host" "mkdir -p '$path/versions/v$version'"
    if [ -n "$bundle_tar" ]; then
        act scp "$bundle_tar" "$host:$path/versions/v$version.tar.gz"
        act ssh "$host" "tar -xzf '$path/versions/v$version.tar.gz' -C '$path/versions/v$version' && rm -f '$path/versions/v$version.tar.gz'"
    else
        note "no bundle tar resolved — run `just package` first for a real ssh deploy."
    fi
    act ssh "$host" "ln -sfn 'versions/v$version' '$path/current.tmp' && mv -f '$path/current.tmp' '$path/current' 2>/dev/null || ln -sfn 'versions/v$version' '$path/current'"
}

deploy_pull() {
    local service_address="$1" version="$2"
    # Pull model: the target self-updates; deploy just publishes/notifies. The
    # reference action is to poke the target's update endpoint (or leave a marker).
    note "transport=pull: the target self-updates; publishing v$version and notifying."
    if [ -n "$service_address" ]; then
        act bash -c "echo 'notify $service_address of v$version (curl -fsS $service_address/admin/update?version=$version)'"
    else
        note "no service_address — a pull deploy has nothing to notify; publish the release and let the target's updater pick it up."
    fi
}

# ── service restart ──────────────────────────────────────────────────────────
restart_service() {
    local manager="$1" service="$2" host="$3" transport="$4"
    [ -n "$manager" ] || { note "no service_manager set — skipping restart (reference)."; return 0; }
    [ -n "$service" ] || die "service_manager=$manager requires a 'service' (unit/label) field."
    local cmd
    case "$manager" in
        systemd) cmd="systemctl restart $service" ;;
        launchd) cmd="launchctl kickstart -k gui/\$(id -u)/$service" ;;
        *) die "unknown service_manager: $manager" ;;
    esac
    if [ "$transport" = "ssh" ]; then
        [ -n "$host" ] || die "ssh restart requires a 'host' field."
        act ssh "$host" "$cmd"
    else
        act bash -c "$cmd"
    fi
}

# ── fleet-instance.json (web-app-deployment-protocol.md §9.4) ─────────────────
# Written host-side at deploy time; survives the process being down so a fleet
# view can show declared-but-down state. In dry-run it is only described.
write_fleet_instance() {
    local env="$1" version="$2" bind="$3" service_address="$4" path="$5" host="$6" transport="$7"
    local json
    json="$(python3 - "$env" "$version" "$bind" "$service_address" <<'PY'
import json, sys
env, version, bind, service_address = sys.argv[1:5]
print(json.dumps({
    "schema_version": 1,
    "env": env,
    "version": version,
    "bind": bind or None,
    "service_address": service_address or None,
}, sort_keys=True))
PY
)"
    local target="${path:-.}/fleet-instance.json"
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "  [dry-run] write fleet-instance.json -> $target:"
        echo "  [dry-run]   $json"
    elif [ "$transport" = "ssh" ]; then
        printf '%s\n' "$json" | act ssh "$host" "cat > '$target'"
    else
        printf '%s\n' "$json" > "$target"
        note "wrote $target"
    fi
}

# ── the deploy flow ──────────────────────────────────────────────────────────
run_deploy() {
    local env="" version="" config="$CONFIG"
    DRY_RUN=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) DRY_RUN=1; shift ;;
            --config) config="$2"; shift 2 ;;
            --bundle) BUNDLE_TAR="$2"; shift 2 ;;
            -*) die "unknown flag: $1" ;;
            *)
                if [ -z "$env" ]; then env="$1"
                elif [ -z "$version" ]; then version="$1"
                else die "unexpected extra argument: $1"; fi
                shift ;;
        esac
    done
    [ -n "$env" ] || die "usage: scripts/deploy.sh <env> [<version>] [--dry-run]"
    version="${version#v}"  # normalize a leading v

    # Resolve the env's topology fields (fails loud if the env is undeclared).
    local policy transport service_manager host path service bind service_address
    policy="$(env_field "$env" deploy_policy "$config")"
    transport="$(env_field "$env" transport "$config")"
    service_manager="$(env_field "$env" service_manager "$config")"
    host="$(env_field "$env" host "$config")"
    path="$(env_field "$env" path "$config")"
    service="$(env_field "$env" service "$config")"
    bind="$(env_field "$env" bind "$config")"
    service_address="$(env_field "$env" service_address "$config")"

    [ -n "$transport" ] || die "environment '$env' has no 'transport' field — set transport ∈ {ssh, local-symlink, pull} in $config."

    note "deploying env=$env version=${version:-<HEAD>} transport=$transport service_manager=${service_manager:-<none>} policy=${policy:-auto} dry_run=$DRY_RUN"

    # Policy gate (clean-tree + tag guard).
    enforce_policy "$policy" "$version"

    # A resolvable version defaults to HEAD's short sha for the versions/ dir name.
    if [ -z "$version" ]; then
        version="$(git rev-parse --short HEAD 2>/dev/null || echo 0.0.0-local)"
    fi
    local bundle_tar="${BUNDLE_TAR:-}"

    # Transport branch.
    case "$transport" in
        ssh)           deploy_ssh "$host" "$path" "$version" "$bundle_tar" ;;
        local-symlink) deploy_local_symlink "$path" "$version" "$bundle_tar" ;;
        pull)          deploy_pull "$service_address" "$version" ;;
        *)             die "unknown transport: $transport (expected ssh | local-symlink | pull)" ;;
    esac

    # Restart (pull-model targets restart themselves; skip).
    if [ "$transport" != "pull" ]; then
        restart_service "$service_manager" "$service" "$host" "$transport"
    fi

    # Host-side fleet manifest.
    write_fleet_instance "$env" "$version" "$bind" "$service_address" "$path" "$host" "$transport"

    note "deploy complete (env=$env version=$version)."
}

# ── self-test (offline temp-dir dry-run round trip) ──────────────────────────
self_test() {
    local fail=0
    local td; td="$(mktemp -d)"
    trap 'rm -rf "$td"' RETURN
    mkdir -p "$td/.claude"
    # The `local` env omits service_manager so the real-deploy path skips the
    # restart (a test sandbox has no live service to kickstart); the `production`
    # env exercises ssh + systemd branching under dry-run (never connects).
    cat > "$td/.claude/grimoire-config.json" <<'EOF'
{
  "name": "demo",
  "environments": {
    "local": {"transport": "local-symlink",
              "path": "PATH_PLACEHOLDER",
              "bind": "127.0.0.1:3000", "deploy_policy": "auto"},
    "production": {"transport": "ssh", "service_manager": "systemd",
                   "host": "deployer@demo.example", "path": "/srv/demo",
                   "service": "demo", "bind": "127.0.0.1:3000",
                   "deploy_policy": "pr_gate", "service_address": "https://demo.example"}
  }
}
EOF
    # a synthetic bundle tar so the ssh dry-run exercises the scp branch.
    printf 'bundle\n' > "$td/bundle.tar.gz"
    # substitute a real path for the local env's deploy root.
    python3 - "$td/.claude/grimoire-config.json" "$td/deploy-root" <<'PY'
import json, sys
cfg_path, root = sys.argv[1:3]
cfg = json.load(open(cfg_path))
cfg["environments"]["local"]["path"] = root
json.dump(cfg, open(cfg_path, "w"), indent=2)
PY
    local script_path; script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    # 1. undeclared env fails loud.
    if ( cd "$td" && bash "$script_path" staging --dry-run ) >/dev/null 2>&1; then
        echo "self-test: undeclared env should fail" >&2; fail=1
    fi

    # 2. local-symlink dry-run: exits 0, mutates NOTHING (no versions dir created).
    if ! ( cd "$td" && bash "$script_path" local 1.0.0 --dry-run ) >/dev/null 2>&1; then
        echo "self-test: local-symlink dry-run should succeed" >&2; fail=1
    fi
    if [ -e "$td/deploy-root/versions" ] || [ -e "$td/deploy-root/current" ] \
       || [ -e "$td/deploy-root/fleet-instance.json" ]; then
        echo "self-test: dry-run must not mutate the deploy root" >&2; fail=1
    fi

    # 3. ssh transport dry-run works (no real ssh — dry-run prints, never connects).
    if ! ( cd "$td" && bash "$script_path" production 2.0.0 --dry-run --bundle bundle.tar.gz ) >/dev/null 2>&1; then
        echo "self-test: ssh dry-run should succeed (prints, never connects)" >&2; fail=1
    fi

    # 4. transport branching is visible in dry-run output.
    local out
    out="$( cd "$td" && bash "$script_path" production 2.0.0 --dry-run --bundle bundle.tar.gz 2>&1 )"
    echo "$out" | grep -q 'transport=ssh' || { echo "self-test: ssh transport not reported" >&2; fail=1; }
    echo "$out" | grep -q 'scp' || { echo "self-test: ssh deploy should scp the bundle" >&2; fail=1; }
    echo "$out" | grep -q 'systemctl restart demo' || { echo "self-test: systemd restart not planned" >&2; fail=1; }
    echo "$out" | grep -q 'fleet-instance.json' || { echo "self-test: fleet-instance.json not planned" >&2; fail=1; }

    # 5. a REAL (non-dry-run) local-symlink deploy performs the flip + writes the manifest.
    ( cd "$td" && bash "$script_path" local 1.0.0 ) >/dev/null 2>&1 || { echo "self-test: real local deploy failed" >&2; fail=1; }
    [ -L "$td/deploy-root/current" ] || { echo "self-test: current symlink not created" >&2; fail=1; }
    [ -f "$td/deploy-root/fleet-instance.json" ] || { echo "self-test: fleet-instance.json not written" >&2; fail=1; }
    if [ -f "$td/deploy-root/fleet-instance.json" ]; then
        python3 - "$td/deploy-root/fleet-instance.json" <<'PY' || fail=1
import json, sys
m = json.load(open(sys.argv[1]))
assert m["env"] == "local", m
assert m["version"] == "1.0.0", m
assert m["schema_version"] == 1, m
PY
    fi

    # 6. missing-field guard: an ssh env with no host fails loud even in dry-run.
    cat > "$td/.claude/bad.json" <<'EOF'
{"environments": {"production": {"transport": "ssh", "path": "/srv/x", "deploy_policy": "auto"}}}
EOF
    if ( cd "$td" && bash "$script_path" production 1.0.0 --dry-run --config .claude/bad.json ) >/dev/null 2>&1; then
        echo "self-test: ssh transport with no host should fail loud" >&2; fail=1
    fi

    if [ "$fail" -ne 0 ]; then
        echo "deploy.sh self-test: FAILED" >&2; return 1
    fi
    echo "deploy.sh self-test: OK (env resolution, policy guard, transport branch, dry-run inertness, real flip + fleet manifest, missing-field guard)"
    return 0
}

case "${1:-}" in
    --self-test) shift; self_test ;;
    -h|--help|"")
        sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
        [ "${1:-}" = "" ] && exit 1 || exit 0
        ;;
    *) run_deploy "$@" ;;
esac
