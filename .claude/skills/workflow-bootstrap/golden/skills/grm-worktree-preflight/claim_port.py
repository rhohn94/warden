#!/usr/bin/env python3
"""claim_port.py — claim a unique, pre-verified free TCP port for a worktree (#77, v3.7).

When several agents are dispatched to parallel worktrees and each builds + runs
the project, they all default to the same port (3000/8080) — causing launch
conflicts AND silent test-poisoning (one agent's traffic hitting a sibling's
instance). This helper hands each worktree a unique, verified-free port BEFORE
any app launches, as a single deterministic call (per #75: a script, not ad-hoc
agent Bash).

It PROBES and REPORTS — it does not hold the port bound; binding is the app's
job. To minimize the probe→bind race it is a single claim-and-verify unit, and
it is **idempotent per worktree-id** (a repeat call returns the same port if it
is still free) via a small gitignored cache (`.claude/cache/port-claims.json`).

Strategies (default os-assign):
  os-assign    bind to port 0, let the kernel pick a free port, read + release it
               (most reliable where the stack cooperates — the recommended path)
  random-probe pick a random candidate in [range-start, range-end], verify free,
               retry on conflict (no shared state required)
  index        deterministic base + worktree index (range-start + index), probing
               upward on conflict (predictable; needs an index)

No git writes, no issue writes — reads OS network state + its own cache only.

Usage:
  claim_port.py [--strategy os-assign|random-probe|index] [--range-start N]
                [--range-end N] [--worktree-id ID] [--index N] [--count K]
                [--cache PATH] [--export VARNAME] [--self-test]
Stdout: the claimed port (one integer; K lines under --count), or an export line.
Exit 0: a free port was identified. Exit 1: none found (fatal — abort dispatch).
"""
import argparse
import json
import os
import random
import socket
import sys

DEFAULT_RANGE_START = 20000
DEFAULT_RANGE_END = 29999
DEFAULT_CACHE = os.path.join(".claude", "cache", "port-claims.json")
DEFAULT_ENV_VAR = "GRIMOIRE_APP_PORT"
MAX_PROBES = 200


def is_free(port, host="127.0.0.1"):
    """True iff a TCP socket can bind `port` right now (probe, then release)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def os_assign(host="127.0.0.1"):
    """Bind to port 0 so the kernel assigns a free port; read it, release it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def random_probe(start, end, taken, rng):
    span = list(range(start, end + 1))
    rng.shuffle(span)
    for port in span[:MAX_PROBES]:
        if port not in taken and is_free(port):
            return port
    return None


def index_probe(start, end, index, taken):
    port = start + max(0, int(index))
    while port <= end:
        if port not in taken and is_free(port):
            return port
        port += 1
    return None


def _worktree_id(explicit):
    if explicit:
        return explicit
    return os.path.basename(os.path.abspath(os.getcwd())) or "default"


def _index_from_id(worktree_id):
    """Best-effort numeric index from a worktree id (trailing digits, else hash)."""
    digits = "".join(ch for ch in worktree_id if ch.isdigit())
    if digits:
        return int(digits) % 10000
    return sum(ord(c) for c in worktree_id) % 10000


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path, cache):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def claim_one(key, strategy, start, end, index, taken, cache, rng):
    """Return a port for `key`, honoring idempotency via the cache."""
    prev = cache.get(key)
    if isinstance(prev, dict):
        p = prev.get("port")
        if isinstance(p, int) and p not in taken and is_free(p):
            return p  # idempotent: same worktree-id, still free
    if strategy == "os-assign":
        port = os_assign()
        # avoid colliding with another port claimed in the same invocation
        tries = 0
        while port in taken and tries < MAX_PROBES:
            port = os_assign()
            tries += 1
    elif strategy == "random-probe":
        port = random_probe(start, end, taken, rng)
    elif strategy == "index":
        port = index_probe(start, end, index, taken)
    else:
        raise ValueError("unknown strategy: %s" % strategy)
    return port


def claim(strategy="os-assign", start=DEFAULT_RANGE_START, end=DEFAULT_RANGE_END,
          worktree_id=None, index=None, count=1, cache_path=DEFAULT_CACHE, rng=None):
    """Claim `count` unique ports; persist them under the worktree id. Returns a
    list of ports (raises RuntimeError if any cannot be allocated)."""
    rng = rng or random.Random()
    wid = _worktree_id(worktree_id)
    base_index = _index_from_id(wid) if index is None else int(index)
    cache = _load_cache(cache_path)
    taken = set()
    ports = []
    for i in range(max(1, count)):
        key = wid if count == 1 else "%s#%d" % (wid, i)
        port = claim_one(key, strategy, start, end, base_index + i, taken, cache, rng)
        if port is None:
            raise RuntimeError("no free port in range %d-%d (strategy %s)"
                               % (start, end, strategy))
        taken.add(port)
        cache[key] = {"port": port, "strategy": strategy}
        ports.append(port)
    _save_cache(cache_path, cache)
    return ports


def _self_test():
    import tempfile
    failures = []

    # is_free: a bound port reads busy, free again once released.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    busy = s.getsockname()[1]
    if is_free(busy):
        failures.append("is_free should report a bound port busy")
    s.close()

    # os_assign returns a usable, currently-free port.
    p = os_assign()
    if not (1 <= p <= 65535):
        failures.append("os_assign out of range: %r" % p)

    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, "port-claims.json")

        # idempotency: same worktree-id returns the same port across calls.
        a = claim(worktree_id="wt-alpha", cache_path=cache)
        b = claim(worktree_id="wt-alpha", cache_path=cache)
        if a != b:
            failures.append("not idempotent for same worktree-id: %r vs %r" % (a, b))

        # distinct worktree ids get distinct ports.
        c = claim(worktree_id="wt-beta", cache_path=cache)
        if c[0] == a[0]:
            failures.append("distinct worktree-ids collided: %r" % c)

        # --count returns K distinct ports.
        many = claim(worktree_id="wt-multi", count=3, cache_path=cache)
        if len(set(many)) != 3:
            failures.append("count=3 not distinct: %r" % many)

        # index strategy is deterministic: base + index.
        i0 = claim(strategy="index", worktree_id="lane", index=0,
                   start=41000, end=41999, cache_path=os.path.join(d, "i0.json"))
        if i0 != [41000]:
            failures.append("index base wrong: %r" % i0)
        i5 = claim(strategy="index", worktree_id="lane", index=5,
                   start=41000, end=41999, cache_path=os.path.join(d, "i5.json"))
        if i5 != [41005]:
            failures.append("index offset wrong: %r" % i5)

        # random-probe stays inside the range.
        rp = claim(strategy="random-probe", worktree_id="wt-rand",
                   start=42000, end=42050, cache_path=os.path.join(d, "rp.json"),
                   rng=random.Random(1234))
        if not (42000 <= rp[0] <= 42050):
            failures.append("random-probe out of range: %r" % rp)

        # exhausted range is fatal (RuntimeError).
        try:
            # occupy the single-port range, then demand another distinct one.
            occupy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            occupy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupy.bind(("127.0.0.1", 0))
            only = occupy.getsockname()[1]
            try:
                claim(strategy="index", worktree_id="x", index=0,
                      start=only, end=only, cache_path=os.path.join(d, "ex.json"))
                failures.append("exhausted range should raise")
            except RuntimeError:
                pass
            finally:
                occupy.close()
        except OSError:
            pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("claim_port self-test: OK (is_free, os_assign, idempotency, distinct "
          "ids, count, index determinism, random-probe range, exhausted-range fatal)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Claim a unique free TCP port for a worktree.")
    ap.add_argument("--strategy", default="os-assign",
                    choices=["os-assign", "random-probe", "index"])
    ap.add_argument("--range-start", type=int, default=DEFAULT_RANGE_START)
    ap.add_argument("--range-end", type=int, default=DEFAULT_RANGE_END)
    ap.add_argument("--worktree-id", default=None)
    ap.add_argument("--index", type=int, default=None)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--export", metavar="VARNAME", default=None,
                    help="print `export VARNAME=port` instead of a bare integer")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.range_start > args.range_end:
        print("error: --range-start must be <= --range-end", file=sys.stderr)
        return 1
    try:
        ports = claim(strategy=args.strategy, start=args.range_start,
                      end=args.range_end, worktree_id=args.worktree_id,
                      index=args.index, count=args.count, cache_path=args.cache)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    if args.export:
        # one export line per port; for --count>1 the vars are suffixed _1.._K
        if len(ports) == 1:
            print("export %s=%d" % (args.export, ports[0]))
        else:
            for i, p in enumerate(ports, 1):
                print("export %s_%d=%d" % (args.export, i, p))
    else:
        for p in ports:
            print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
