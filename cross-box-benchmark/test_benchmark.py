#!/usr/bin/env python3
"""Tests for the benchmark kit. Run: python3 test_benchmark.py

compare.py tests run anywhere. The bench.sh / sample.sh smoke tests need Linux (/proc/stat, /proc/pressure); they put
stub sysbench, fio and curl first on PATH, so they finish in seconds, touch no disk beyond a temp dir, and use no
network. Elsewhere they are skipped with a message."""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "examples")
sys.path.insert(0, HERE)
import compare  # noqa: E402

LINUX = os.path.exists("/proc/stat")


class Compare(unittest.TestCase):
    def load(self, name):
        return compare.last_line(os.path.join(EX, name))

    def test_ratios_match_the_published_table(self):
        _, r, notes = compare.compare(self.load("old-vm-idle.jsonl"), self.load("new-vps.jsonl"))
        self.assertAlmostEqual(r["sysbench_cpu_1t_eps"], 1.26, places=2)
        self.assertAlmostEqual(r["sysbench_cpu_nt_eps"], 4.74, places=2)
        self.assertAlmostEqual(r["fio_randread_4k_iops"], 31.55, places=1)
        self.assertEqual(notes, [])

    def test_busy_before_run_is_flagged(self):
        _, r, notes = compare.compare(self.load("old-vm-busy.jsonl"), self.load("new-vps.jsonl"), "before", "after")
        self.assertAlmostEqual(r["sysbench_cpu_1t_eps"], 1.59, places=2)       # the flattering number...
        self.assertEqual(len(notes), 1)                                         # ...comes with a warning
        self.assertIn("before: load1 was 3.12 on 2 CPU(s)", notes[0])

    def test_different_parameters_are_not_compared(self):
        a, b = self.load("old-vm-idle.jsonl"), dict(self.load("new-vps.jsonl"), params="fio 1G 30s")
        with self.assertRaises(SystemExit):
            compare.compare(a, b)

    def test_a_refused_download_is_na_not_zero(self):
        b = dict(self.load("new-vps.jsonl"), net_hetzner_ash_100MB_MBps=None)
        table, r, _ = compare.compare(self.load("old-vm-idle.jsonl"), b)
        self.assertIsNone(r["net_hetzner_ash_100MB_MBps"])
        self.assertIn("| download 100 MB (MB/s) | 133 | n/a | n/a |", table)

    def test_samples_summary(self):
        s = compare.summarize_samples(os.path.join(EX, "old-vm-idle-samples.jsonl"))
        self.assertEqual((s["samples"], s["steal_max"], s["busy_avg"]), (61, 0.01, 0.19))

    def test_cli(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "compare.py"), os.path.join(EX, "old-vm-busy.jsonl"),
                            os.path.join(EX, "new-vps.jsonl"), "--old-label", "A", "--new-label", "B"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| CPU, 1 thread (events/s) | 908 | 1,442 | 1.59x |", r.stdout)
        self.assertIn("WARNING: A: load1", r.stdout)


def stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


@unittest.skipUnless(LINUX, "bench.sh / sample.sh read /proc: Linux only")
class Scripts(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bin = os.path.join(self.d, "bin")
        os.mkdir(self.bin)
        stub(self.bin, "sysbench", 'case "$1" in --version) echo "sysbench 1.0.20";; *) echo "    events per second:  1234.56";; esac')
        stub(self.bin, "fio", 'case "$1" in --version) echo "fio-3.33";; *) echo \'{"jobs":[{"read":{"iops":1000.4,"bw":102400},"write":{"iops":900.6,"bw":51200}}]}\';; esac')
        stub(self.bin, "curl", 'case "$1" in --version) echo "curl 7.88.1 (x86_64)";; *) echo "200 104857600";; esac')
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], PERF_OUT=os.path.join(self.d, "out"))

    def test_bench_writes_one_complete_json_line(self):
        r = subprocess.run(["bash", os.path.join(HERE, "bench.sh")], env=self.env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(line["sysbench_cpu_1t_eps"], 1234.56)
        self.assertEqual((line["fio_randread_4k_iops"], line["fio_seqwrite_1m_MBps"]), (1000, 50.0))
        self.assertEqual(line["net_hetzner_ash_100MB_MBps"], 100.0)
        self.assertEqual(line["tools"]["fio"], "fio-3.33")
        with open(os.path.join(self.d, "out", "bench.jsonl")) as f:
            self.assertEqual(json.loads(f.read().strip()), line)          # appended to the results file too

    def test_bench_records_null_not_zero_for_a_refused_download(self):
        stub(self.bin, "curl", 'case "$1" in --version) echo "curl 7.88.1";; *) echo "403 0";; esac')
        r = subprocess.run(["bash", os.path.join(HERE, "bench.sh")], env=self.env, capture_output=True, text=True,
                           timeout=60)
        self.assertIsNone(json.loads(r.stdout.strip().splitlines()[-1])["net_hetzner_ash_100MB_MBps"])

    def test_sampler_writes_a_line_per_interval(self):
        env = dict(self.env, PERF_INTERVAL="1")
        p = subprocess.Popen(["bash", os.path.join(HERE, "sample.sh")], env=env, start_new_session=True)
        time.sleep(3.5)
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
        with open(os.path.join(self.d, "out", "samples.jsonl")) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {"host", "ts", "steal_pct", "busy_pct", "psi_avg60", "load1", "mem_avail_pct"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
