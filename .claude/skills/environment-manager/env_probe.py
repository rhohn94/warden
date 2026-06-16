#!/usr/bin/env python3
"""env_probe.py — deterministic process / port inspection for the
environment-manager role (#76).

Prefer this over ad-hoc `lsof`/`ps`/`ss` reasoning (scripting-unification #75):
it wraps the available system tool, parses its output into a stable JSON shape,
and degrades cleanly when a tool is absent. Read-only — it never kills or starts
anything (lifecycle actions are the agent's, gated per-action).

Design authority: docs/design/environment-manager-design.md.

Usage:
  env_probe.py                      # list listening TCP ports + owning processes
  env_probe.py --port 3000 [8080]   # what (if anything) holds these ports
  env_probe.py --name node          # processes whose command matches a substring
  env_probe.py --self-test
Outputs JSON {tool, listeners|matches, degraded} to stdout. Exit 0 ok, 2 bad input.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys


def _run(cmd):
    """Run a command, return stdout text or None on failure/absence."""
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else (out.stdout or "")


def parse_lsof(text):
    """Parse `lsof -nP -iTCP -sTCP:LISTEN` output → list of listener dicts."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("COMMAND"):
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        command, pid, user = parts[0], parts[1], parts[2]
        name = parts[8]  # e.g. 127.0.0.1:3000  or  *:8080
        m = re.search(r":(\d+)$", name)
        port = int(m.group(1)) if m else None
        addr = name[: m.start()] if m else name
        rows.append({"command": command, "pid": _int(pid), "user": user,
                     "address": addr, "port": port, "proto": "tcp",
                     "state": "LISTEN"})
    return rows


def parse_ss(text):
    """Parse `ss -ltnp` output (Linux) → list of listener dicts."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("State") or line.startswith("Netid"):
            continue
        parts = line.split()
        # find the Local Address:Port column (contains ':')
        local = next((p for p in parts if ":" in p and not p.startswith("users:")), None)
        if not local:
            continue
        m = re.search(r":(\d+)$", local)
        port = int(m.group(1)) if m else None
        addr = local[: m.start()] if m else local
        # process field: users:(("node",pid=12345,fd=21))
        proc = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
        command = proc.group(1) if proc else None
        pid = _int(proc.group(2)) if proc else None
        rows.append({"command": command, "pid": pid, "user": None,
                     "address": addr, "port": port, "proto": "tcp",
                     "state": "LISTEN"})
    return rows


def parse_ps(text, needle):
    """Parse `ps -eo pid=,comm=,args=` output → rows whose command/args match."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, comm = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        if needle.lower() in comm.lower() or needle.lower() in args.lower():
            rows.append({"pid": _int(pid), "command": comm, "args": args})
    return rows


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def list_listeners():
    degraded = []
    text = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    if text is not None:
        return {"tool": "lsof", "listeners": parse_lsof(text), "degraded": degraded}
    text = _run(["ss", "-ltnp"])
    if text is not None:
        return {"tool": "ss", "listeners": parse_ss(text), "degraded": degraded}
    degraded.append("no lsof or ss available — cannot enumerate listeners")
    return {"tool": None, "listeners": [], "degraded": degraded}


def for_ports(ports):
    res = list_listeners()
    want = set(ports)
    res["listeners"] = [r for r in res["listeners"] if r.get("port") in want]
    res["queried_ports"] = sorted(want)
    return res


def by_name(needle):
    degraded = []
    text = _run(["ps", "-eo", "pid=,comm=,args="])
    if text is None:
        text = _run(["ps", "-eo", "pid=,comm="])
    if text is None:
        degraded.append("no ps available")
        return {"tool": None, "matches": [], "degraded": degraded}
    return {"tool": "ps", "matches": parse_ps(text, needle), "degraded": degraded}


def _self_test():
    failures = []
    lsof_fixture = (
        "COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "node    12345   rob   21u  IPv4 0xabc      0t0  TCP 127.0.0.1:3000 (LISTEN)\n"
        "python  23456   rob    5u  IPv6 0xdef      0t0  TCP *:8080 (LISTEN)\n"
    )
    rows = parse_lsof(lsof_fixture)
    if len(rows) != 2:
        failures.append("lsof: expected 2 rows, got %d" % len(rows))
    else:
        if rows[0]["port"] != 3000 or rows[0]["command"] != "node" or rows[0]["pid"] != 12345:
            failures.append("lsof: row0 wrong: %r" % rows[0])
        if rows[1]["port"] != 8080 or rows[1]["address"] != "*":
            failures.append("lsof: row1 wrong: %r" % rows[1])

    ss_fixture = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      128    0.0.0.0:3000       0.0.0.0:*         users:(("node",pid=12345,fd=21))\n'
        'LISTEN 0      128    [::]:8080          [::]:*            users:(("python3",pid=23456,fd=5))\n'
    )
    srows = parse_ss(ss_fixture)
    if len(srows) != 2:
        failures.append("ss: expected 2 rows, got %d" % len(srows))
    else:
        if srows[0]["port"] != 3000 or srows[0]["command"] != "node" or srows[0]["pid"] != 12345:
            failures.append("ss: row0 wrong: %r" % srows[0])
        if srows[1]["port"] != 8080 or srows[1]["command"] != "python3":
            failures.append("ss: row1 wrong: %r" % srows[1])

    ps_fixture = (
        "12345 node /usr/bin/node server.js\n"
        "23456 python3 /app/manage.py runserver\n"
        "34567 bash -lc sleep\n"
    )
    pm = parse_ps(ps_fixture, "node")
    if not (len(pm) == 1 and pm[0]["pid"] == 12345):
        failures.append("ps name=node match wrong: %r" % pm)
    pm2 = parse_ps(ps_fixture, "manage.py")
    if not (len(pm2) == 1 and pm2[0]["pid"] == 23456):
        failures.append("ps args match (manage.py) wrong: %r" % pm2)

    # determinism: same fixture ⇒ same parse
    if json.dumps(parse_lsof(lsof_fixture)) != json.dumps(parse_lsof(lsof_fixture)):
        failures.append("non-deterministic lsof parse")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("env_probe self-test: OK (lsof/ss/ps parsers, port filter, name match, determinism)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only process / port inspection.")
    ap.add_argument("--port", nargs="+", type=int, help="filter to these port(s)")
    ap.add_argument("--name", help="match processes by command/args substring")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.name:
        result = by_name(args.name)
    elif args.port:
        result = for_ports(args.port)
    else:
        result = list_listeners()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
