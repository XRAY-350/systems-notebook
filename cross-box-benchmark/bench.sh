#!/usr/bin/env bash
# bench.sh: the SAME short benchmark set on every box, so a before/after host move compares like with like.
# Quiet windows only (no backups, peaks or maintenance running); stop if anyone is using the box. Prints one JSON line
# and appends it to /var/lib/perf/bench.jsonl. Exact commands + tool versions are in the JSON so results are reproducible.
#   sysbench cpu  : --cpu-max-prime=20000 --time=15, threads=1 and threads=$(nproc)  -> events/s
#   fio           : 512 MiB scratch file in /var/tmp, direct I/O, libaio, 15 s each: randread/randwrite 4k iodepth 32 (IOPS),
#                   read/write 1M iodepth 8 (MB/s); file removed after
#   network       : curl 100 MB from https://ash-speed.hetzner.com/100MB.bin (Hetzner Ashburn, same target everywhere) -> MB/s;
#                   a non-200 answer records null, never 0 (a CDN speed endpoint refused 100 MB with 403 on the first run)
#   steal         : % of CPU time stolen by the hypervisor during the run (/proc/stat delta)
set -uo pipefail
OUT="${PERF_OUT:-/var/lib/perf}"; mkdir -p "$OUT"; DIR=/var/tmp/bench.$$; mkdir -p "$DIR"; trap 'rm -rf "$DIR"' EXIT
T=15; N=$(nproc)
stat() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9}' /proc/stat; }
read t0 s0 < <(stat)
cpu() { sysbench cpu --cpu-max-prime=20000 --threads="$1" --time=$T run 2>/dev/null | awk '/events per second/{print $4}'; }
c1=$(cpu 1); cn=$(cpu "$N")
f() { fio --name=b --filename="$DIR/f" --size=512M --direct=1 --ioengine=libaio --runtime=$T --time_based --group_reporting \
        --rw="$1" --bs="$2" --iodepth="$3" --output-format=json 2>/dev/null; }
iops() { python3 -c 'import json,sys; j=json.load(sys.stdin)["jobs"][0]; print(round(j[sys.argv[1]]["iops"]))' "$1"; }
mbs()  { python3 -c 'import json,sys; j=json.load(sys.stdin)["jobs"][0]; print(round(j[sys.argv[1]]["bw"]/1024,1))' "$1"; }
rr=$(f randread 4k 32 | iops read); rw=$(f randwrite 4k 32 | iops write)
sr=$(f read 1M 8 | mbs read); sw=$(f write 1M 8 | mbs write)
net=$(curl -s -o /dev/null -m 90 -w '%{http_code} %{speed_download}' 'https://ash-speed.hetzner.com/100MB.bin' | awk '$1==200{printf "%.1f", $2/1048576}')
read t1 s1 < <(stat)
steal=$(awk -v a="$t0" -v b="$t1" -v c="$s0" -v d="$s1" 'BEGIN{ if (b>a) printf "%.2f", 100*(d-c)/(b-a); else print "?" }')
python3 - "$@" <<PY | tee -a "$OUT/bench.jsonl"
import json, platform, subprocess, time
v = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip().splitlines()[0] if subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip() else ""
print(json.dumps({
  "host": platform.node(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "nproc": $N,
  "cpu_model": v("awk -F: '/model name/{print \$2; exit}' /proc/cpuinfo").strip(),
  "kernel": platform.release(), "load1_before": open("/proc/loadavg").read().split()[0],
  "sysbench_cpu_1t_eps": float("$c1" or 0), "sysbench_cpu_nt_eps": float("$cn" or 0),
  "fio_randread_4k_iops": int("$rr" or 0), "fio_randwrite_4k_iops": int("$rw" or 0),
  "fio_seqread_1m_MBps": float("$sr" or 0), "fio_seqwrite_1m_MBps": float("$sw" or 0),
  "net_hetzner_ash_100MB_MBps": (float("$net") if "$net" else None), "steal_pct_during": "$steal",
  "tools": {"sysbench": v("sysbench --version"), "fio": v("fio --version"), "curl": v("curl --version").split(" (")[0]},
  "params": "sysbench cpu prime=20000 time=15 threads=1|nproc; fio 512M direct libaio 15s randread/randwrite 4k qd32, read/write 1M qd8; curl 100MB ash-speed.hetzner.com"
}))
PY
