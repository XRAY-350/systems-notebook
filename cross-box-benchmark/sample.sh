#!/usr/bin/env bash
# sample.sh: passive sampler for the before/after report: every PERF_INTERVAL seconds (default 5 min), CPU steal % and busy % over the last
# interval (/proc/stat), PSI cpu/memory/io avg60 (/proc/pressure), load and memory available. One JSON line per sample to
# /var/lib/perf/samples.jsonl. Light: reads /proc only. Run as: systemd-run --unit=perf-sample -p RuntimeMaxSec=25h sample.sh
set -uo pipefail
OUT="${PERF_OUT:-/var/lib/perf}"; mkdir -p "$OUT"; INT="${PERF_INTERVAL:-300}"
prev=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9, $5+$6}' /proc/stat)
while sleep "$INT"; do
  cur=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9, $5+$6}' /proc/stat)
  python3 - "$prev" "$cur" <<'PY' >> "$OUT/samples.jsonl"
import json, sys, time, platform
(t0, s0, i0), (t1, s1, i1) = (map(int, x.split()) for x in sys.argv[1:3])
dt = max(t1 - t0, 1)
psi = {}
for k in ("cpu", "memory", "io"):
    try:
        line = open(f"/proc/pressure/{k}").readline().split()
        psi[k] = float(dict(p.split("=") for p in line[1:])["avg60"])
    except Exception:
        psi[k] = None
mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith(("MemTotal", "MemAvailable"))}
print(json.dumps({"host": platform.node(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "steal_pct": round(100 * (s1 - s0) / dt, 2), "busy_pct": round(100 * (1 - (i1 - i0) / dt), 1),
                  "psi_avg60": psi, "load1": float(open("/proc/loadavg").read().split()[0]),
                  "mem_avail_pct": round(100 * mem["MemAvailable"] / mem["MemTotal"], 1)}))
PY
  prev="$cur"
done
