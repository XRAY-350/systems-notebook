#!/usr/bin/env python3
"""compare.py: turn bench.sh / sample.sh output into a before/after table, and say when a number can't be trusted.

  python3 compare.py OLD-bench.jsonl NEW-bench.jsonl [--old-label old] [--new-label new]
  python3 compare.py --samples samples.jsonl

Uses the LAST line of each bench file (bench.sh appends). Prints a Markdown table with the ratio new/old per metric.
Refuses to compare runs whose tools or parameters differ, and warns when a run's CPU numbers include the box's own
load (load1 before the run above a quarter of a core per CPU), because a busy "before" box makes any move look better
than it is.
"""
import json
import sys

METRICS = [
    ("sysbench_cpu_1t_eps", "CPU, 1 thread (events/s)"),
    ("sysbench_cpu_nt_eps", "CPU, all threads (events/s)"),
    ("fio_randread_4k_iops", "random read 4k (IOPS)"),
    ("fio_randwrite_4k_iops", "random write 4k (IOPS)"),
    ("fio_seqread_1m_MBps", "sequential read 1M (MB/s)"),
    ("fio_seqwrite_1m_MBps", "sequential write 1M (MB/s)"),
    ("net_hetzner_ash_100MB_MBps", "download 100 MB (MB/s)"),
]
BUSY_PER_CPU = 0.25


def last_line(path):
    with open(path) as f:
        lines = [l for l in f if l.strip()]
    if not lines:
        raise SystemExit(f"compare: {path} has no results")
    return json.loads(lines[-1])


def warnings(run, label):
    out = []
    try:
        load, n = float(run.get("load1_before", 0)), int(run.get("nproc", 1))
    except (TypeError, ValueError):
        return out
    if load > BUSY_PER_CPU * n:
        out.append(f"{label}: load1 was {load:g} on {n} CPU(s) before the run, so its CPU numbers include the box's own "
                   f"load; re-run it idle before trusting a CPU ratio")
    if str(run.get("steal_pct_during", "0")) not in ("0", "0.0", "0.00", "?"):
        try:
            if float(run["steal_pct_during"]) > 5:
                out.append(f"{label}: {run['steal_pct_during']}% CPU steal during the run (a noisy neighbour)")
        except ValueError:
            pass
    return out


def fmt(v):
    if v is None:
        return "n/a"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"


def compare(old, new, old_label="old", new_label="new"):
    if old.get("params") != new.get("params") or old.get("tools", {}).get("fio", "").split("-")[0] != \
            new.get("tools", {}).get("fio", "").split("-")[0]:
        raise SystemExit("compare: the two runs used different parameters or tools; not comparable")
    rows = [f"| metric | {old_label} | {new_label} | {new_label} / {old_label} |", "|---|---|---|---|"]
    ratios = {}
    for key, name in METRICS:
        a, b = old.get(key), new.get(key)
        r = (b / a) if (a and b is not None) else None
        ratios[key] = r
        rows.append(f"| {name} | {fmt(a)} | {fmt(b)} | {'n/a' if r is None else f'{r:.2f}x'} |")
    notes = warnings(old, old_label) + warnings(new, new_label)
    return "\n".join(rows), ratios, notes


def summarize_samples(path):
    with open(path) as f:
        rows = [json.loads(l) for l in f if l.strip()]
    if not rows:
        raise SystemExit(f"compare: {path} has no samples")
    st = [r["steal_pct"] for r in rows]
    busy = [r["busy_pct"] for r in rows]
    psi = {k: max((r["psi_avg60"].get(k) or 0) for r in rows) for k in ("cpu", "memory", "io")}
    return {"samples": len(rows), "from": rows[0]["ts"], "to": rows[-1]["ts"], "steal_max": max(st),
            "steal_avg": round(sum(st) / len(st), 3), "busy_avg": round(sum(busy) / len(busy), 2), "busy_max": max(busy),
            "psi_avg60_max": psi}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="before/after table from bench.sh output, or a samples summary")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--samples", metavar="SAMPLES_JSONL")
    ap.add_argument("--old-label", default="old")
    ap.add_argument("--new-label", default="new")
    a = ap.parse_args(argv)
    if a.samples:
        print(json.dumps(summarize_samples(a.samples), indent=2))
        return 0
    if len(a.files) != 2:
        ap.error("give OLD-bench.jsonl NEW-bench.jsonl, or --samples FILE")
    table, _, notes = compare(last_line(a.files[0]), last_line(a.files[1]), a.old_label, a.new_label)
    print(table)
    for n in notes:
        print(f"\nWARNING: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
