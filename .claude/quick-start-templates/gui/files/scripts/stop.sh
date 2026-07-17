#!/usr/bin/env bash
# stop.sh — kill running instance(s) of THIS project's process (#322, INTERFACE v6).
#
# Reference implementation of the standard `stop` recipe
# (docs/design/justfile-standard-design.md §stop):
#
#     scripts/stop.sh [<port>]
#
# Called by `just stop ${port}` (POSITIONAL — just does not parse `key=value`
# CLI args as named; they'd become a literal string). Wired via recipes.json,
# so `recipe.py stop --port <p>` ≡ `just stop <p>`.
#
# Resolution order (first that identifies a live, killable process wins):
#   1. the `port` argument (explicit `--port` / positional param)
#   2. $GRIMOIRE_APP_PORT (the per-worktree port claimed by claim_port.py)
#   3. the pidfile written by `run` ($GRIMOIRE_RUN_PIDFILE, default
#      .grimoire-run.pid)
#   4. a project-declared process pattern ($GRIMOIRE_APP_PATTERN, or the
#      `process_pattern` field of .claude/grimoire-config.json)
#
# This is generic/stack-agnostic (like sync-deps/vendor-check) — it never needs a
# project-specific rewrite. It only kills processes it can POSITIVELY IDENTIFY as
# this project's own (bound to the resolved port, named by the resolved pidfile,
# or matching the declared pattern) — never a broad/ambiguous kill. It is
# IDEMPOTENT: nothing running at any resolution stage is exit 0 with a report,
# never an error.
#
# Self-test: `scripts/stop.sh --self-test` spawns real local sentinel processes
# (a pidfile'd sleep, a python http.server on a port, a pattern-matched sleep)
# and asserts each resolution stage kills its target and that a second stop is
# idempotent (exit 0, no-op).
set -uo pipefail

note() { echo "stop.sh: $*" >&2; }

PIDFILE="${GRIMOIRE_RUN_PIDFILE:-.grimoire-run.pid}"

# Read the `process_pattern` field from .claude/grimoire-config.json, if present.
config_pattern() {
    local config="${GRIMOIRE_CONFIG:-.claude/grimoire-config.json}"
    [ -f "$config" ] || return 0
    python3 - "$config" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
val = cfg.get("process_pattern")
if val:
    print(val)
PY
}

# Graceful-then-forceful kill of one PID. Waits up to ~5s for SIGTERM to land
# before escalating to SIGKILL. Returns 0 if the PID is confirmed gone.
kill_pid() {
    local pid="$1"
    kill -0 "$pid" 2>/dev/null || return 0   # already gone
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.5
    done
    note "pid $pid still alive after SIGTERM — escalating to SIGKILL."
    kill -KILL "$pid" 2>/dev/null || true
    sleep 0.2
    kill -0 "$pid" 2>/dev/null && return 1
    return 0
}

# Find PIDs listening on $1 (TCP), one per line. Empty output = nothing bound.
pids_on_port() {
    lsof -t -i "TCP:$1" -sTCP:LISTEN 2>/dev/null || true
}

# ── the stop flow ─────────────────────────────────────────────────────────────
run_stop() {
    local killed_any=0
    local port="${1:-}"
    [ -n "$port" ] || port="${GRIMOIRE_APP_PORT:-}"

    # 1/2: port param -> $GRIMOIRE_APP_PORT.
    if [ -n "$port" ]; then
        local pids; pids="$(pids_on_port "$port")"
        if [ -n "$pids" ]; then
            local pid
            for pid in $pids; do
                note "killing pid $pid listening on port $port."
                kill_pid "$pid" && killed_any=1
            done
        else
            note "no process listening on port $port."
        fi
    fi

    # 3: pidfile written by `run`.
    if [ -f "$PIDFILE" ]; then
        local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            note "killing pid $pid from pidfile $PIDFILE."
            kill_pid "$pid" && killed_any=1
        else
            note "pidfile $PIDFILE is stale (no live process) — removing."
        fi
        rm -f "$PIDFILE"
    fi

    # 4: declared process pattern.
    local pattern="${GRIMOIRE_APP_PATTERN:-}"
    [ -n "$pattern" ] || pattern="$(config_pattern)"
    if [ -n "$pattern" ]; then
        local pids; pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            local pid
            for pid in $pids; do
                note "killing pid $pid matching declared pattern '$pattern'."
                kill_pid "$pid" && killed_any=1
            done
        fi
    fi

    if [ "$killed_any" -eq 1 ]; then
        note "stop complete."
    else
        note "nothing running (checked port=${port:-<none>}, pidfile=$PIDFILE, pattern=${pattern:-<none>}) — idempotent no-op."
    fi
    return 0
}

# ── self-test (offline, spawns/kills real local test processes) ─────────────
self_test() {
    local fail=0
    local td; td="$(mktemp -d)"
    trap 'rm -rf "$td"' RETURN
    local script_path; script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    # 1. idempotent no-op: nothing running, no pidfile, no pattern -> exit 0.
    if ! ( cd "$td" && bash "$script_path" ) >/dev/null 2>&1; then
        echo "self-test: no-op stop should exit 0" >&2; fail=1
    fi

    # 2. pidfile-based kill.
    ( cd "$td" && sleep 60 & echo $! > "$td/.grimoire-run.pid" )
    local pid; pid="$(cat "$td/.grimoire-run.pid")"
    ( cd "$td" && bash "$script_path" ) >/dev/null 2>&1
    if kill -0 "$pid" 2>/dev/null; then
        echo "self-test: pidfile-based stop should have killed pid $pid" >&2; fail=1
        kill -KILL "$pid" 2>/dev/null || true
    fi
    if [ -f "$td/.grimoire-run.pid" ]; then
        echo "self-test: stop should remove the consumed pidfile" >&2; fail=1
    fi

    # 3. port-based kill (python's http.server as a stand-in served process).
    local port=18420
    ( cd "$td" && python3 -m http.server "$port" --bind 127.0.0.1 >/dev/null 2>&1 & )
    sleep 0.5
    if ! ( cd "$td" && bash "$script_path" "$port" ) >/dev/null 2>&1; then
        echo "self-test: port-based stop should exit 0" >&2; fail=1
    fi
    sleep 0.3
    if [ -n "$(lsof -t -i "TCP:$port" -sTCP:LISTEN 2>/dev/null || true)" ]; then
        echo "self-test: port $port should be free after stop" >&2; fail=1
    fi

    # 4. declared-pattern-based kill.
    ( sleep 54321 & )
    sleep 0.2
    if ! ( cd "$td" && GRIMOIRE_APP_PATTERN="sleep 54321" bash "$script_path" ) >/dev/null 2>&1; then
        echo "self-test: pattern-based stop should exit 0" >&2; fail=1
    fi
    sleep 0.2
    if pgrep -f "sleep 54321" >/dev/null 2>&1; then
        echo "self-test: pattern-matched process should be killed" >&2; fail=1
        pkill -f "sleep 54321" 2>/dev/null || true
    fi

    # 5. second stop (nothing left running) is still idempotent, exit 0.
    if ! ( cd "$td" && bash "$script_path" "$port" ) >/dev/null 2>&1; then
        echo "self-test: second stop should exit 0 (idempotent)" >&2; fail=1
    fi

    if [ "$fail" -ne 0 ]; then
        echo "stop.sh self-test: FAILED" >&2; return 1
    fi
    echo "stop.sh self-test: OK (idempotent no-op, pidfile kill, port kill, pattern kill, idempotent re-stop)"
    return 0
}

case "${1:-}" in
    --self-test) shift; self_test ;;
    -h|--help)
        sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) run_stop "$@" ;;
esac
