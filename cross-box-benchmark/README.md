# cross-box-benchmark

Compare servers before and after a host move with the same short benchmark everywhere, and don't let a busy
"before" machine make the move look better than it was.

```
$ bash bench.sh                          # one JSON line: CPU, disk, network, steal, tools and parameters
$ PERF_INTERVAL=60 bash sample.sh        # one line per interval: steal, busy, PSI pressure, load, memory
$ python3 compare.py examples/old-vm-idle.jsonl examples/new-vps.jsonl --old-label "old VM (idle)" --new-label "new VPS"
| metric | old VM (idle) | new VPS | new VPS / old VM (idle) |
|---|---|---|---|
| CPU, 1 thread (events/s) | 1,146 | 1,442 | 1.26x |
| CPU, all threads (events/s) | 1,201 | 5,693 | 4.74x |
| random read 4k (IOPS) | 3,196 | 100,836 | 31.55x |
...
```

These are the scripts behind a before/after report for moving a small fleet from a cloud VM to a VPS provider.
The numbers in `examples/` are the real runs, with host names removed.

**Who wrote it:** AI coding agents (Claude Code) wrote the scripts and ran the benchmarks, to my requirements. My
part was asking for the before/after numbers, and later approving a re-measurement of the old machine once it was idle.

## What it measures

- **`bench.sh`** (about 2 minutes): sysbench CPU at 1 thread and at all threads; fio on a 512 MiB scratch file with
  direct I/O (4k random read and write at queue depth 32, 1M sequential read and write at depth 8, 15 s each); one
  100 MB download from a fixed public speed-test target; and the CPU steal during the run from `/proc/stat`. The
  JSON line records the exact tool versions and parameters, and the load before the run.
- **`sample.sh`**: a passive sampler that reads only `/proc`. Each interval it records steal and busy percent, PSI
  pressure for CPU, memory and I/O (`avg60`), load and available memory. In production it ran 24 hours on each box as a
  transient systemd unit (`systemd-run -p RuntimeMaxSec=25h`).
- **`compare.py`**: a Markdown table of new/old ratios from two bench files, and a summary of a samples file.

## Design decisions

- **The same script, unchanged, on every box.** The point of a before/after number is that only the box changed.
  Every result carries its tool versions and parameters, and `compare.py` refuses to compare two runs whose
  parameters differ.
- **A failed download is null, not zero.** A speed-test endpoint refused the 100 MB file on the first run. A zero
  would have read as "the network is dead" and dragged an average down; null reads as "not measured".
- **Quiet windows only.** Each run happens when the box isn't doing backups, peak work or maintenance, and the
  sampler is light enough to leave running.

## The number that was too good

The first report said the new box was **1.59x** faster single-threaded. The old cloud VM had been benchmarked while
it was doing its normal job: load 3.12 on 2 vCPUs. Its CPU numbers were the capacity left over, not the machine's.
Before the old VM was switched off, it was benchmarked again once it was empty: same script, unchanged, with an idle
check first (agents stopped, no runners, no game servers, nobody logged in, load 0.00) and an hour of samples
(steal at most 0.01 percent, busy 0.19 percent on average).

| metric | old VM busy | old VM idle | new VPS / old VM idle |
|---|---|---|---|
| CPU, 1 thread (events/s) | 908 | 1,146 | **1.26x** |
| CPU, all threads (events/s) | 949 | 1,201 | 4.7x |
| random read 4k (IOPS) | 3,199 | 3,196 | 32x |
| random write 4k (IOPS) | 2,936 | 3,055 | 22x |
| sequential read 1M (MB/s) | 129 | 133 | 31x |
| sequential write 1M (MB/s) | 119 | 128 | 18x |
| download 100 MB (MB/s) | 110 | 133 | 1.22x |

Idle, the old VM was 26 percent faster on CPU, so the fair single-thread gain is 1.26x. Its disk didn't move,
because it was at its storage volume's provisioned limit either way. The all-core and storage gaps are large
regardless. `compare.py` now prints a warning whenever a run's load before the benchmark is above a quarter of a core
per CPU, so a busy baseline can't slip into a report unnoticed again.

## Verified

`python3 test_benchmark.py`: 9 tests.

- `compare.py`: the published ratios reproduce from the example data; the busy baseline gets its warning; runs with
  different parameters are refused; a refused download shows as n/a; the samples summary; the command line.
- `bench.sh` and `sample.sh` (Linux only, skipped elsewhere): with stub `sysbench`, `fio` and `curl` first on `PATH`,
  bench writes one complete JSON line and appends it to the results file, a refused download records null, and
  the sampler writes one line per interval with the expected fields. No network and no real disk load.
