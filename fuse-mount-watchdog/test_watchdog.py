#!/usr/bin/env python3
"""Tests for watchdog.sh. systemctl, fusermount, pgrep and stat are stubs first on PATH; a "wedged" mount is a stat
stub that blocks while a flag file exists. No root, no FUSE, no real mounts. Run: python3 test_watchdog.py"""
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WD = os.path.join(HERE, "watchdog.sh")

UNITS = {
    # the mountpoint is NOT derivable from the unit name: it has to be read from ExecStart
    "sshfs-media.service": "ExecStart=/usr/bin/sshfs -f -o reconnect,ServerAliveInterval=15 backup@media-host:/srv/media /mnt/legacy/old-name/media -o allow_other",
    "sshfs-bus.service": "ExecStart=-/usr/bin/sshfs -f bus@store-host:/var/bus /mnt/bus",
    "sshfs-off.service": "ExecStart=/usr/bin/sshfs -f a@b:/x /mnt/off",                 # disabled: not watched
    "sshfs-notfuse.service": "ExecStart=/usr/bin/rsync -a a@b:/x /mnt/elsewhere",       # not sshfs: not watched
}
STATES = {"sshfs-media.service": "enabled", "sshfs-bus.service": "enabled", "sshfs-off.service": "disabled",
          "sshfs-notfuse.service": "enabled"}


def stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        b = self.bin = os.path.join(self.d, "bin")
        os.mkdir(b)
        self.calls = os.path.join(self.d, "calls.log")
        self.wedge = os.path.join(self.d, "wedged")          # while this exists, stat on /mnt/bus hangs
        os.mkdir(os.path.join(self.d, "units"))
        for u, line in UNITS.items():
            with open(os.path.join(self.d, "units", u), "w") as f:
                f.write(f"[Service]\n{line}\n")
        with open(os.path.join(self.d, "list"), "w") as f:
            f.write("".join(f"{u} {s} enabled\n" for u, s in STATES.items()))
        rec = f'echo "$(basename "$0") $*" >> {self.calls}'
        stub(b, "systemctl", rec + f'''
case "$1" in
  list-unit-files) cat {self.d}/list ;;
  cat) cat "{self.d}/units/$2" ;;
  restart) : ;;
esac''')
        stub(b, "fusermount", rec)
        stub(b, "pgrep", f'cat {self.d}/sshfs.pid 2>/dev/null')
        stub(b, "stat", f'case "$1" in /mnt/bus) while [ -e {self.wedge} ]; do sleep 0.1; done ;; esac; exit 0')
        self.env = dict(os.environ, PATH=b + os.pathsep + os.environ["PATH"], STATE_FILE=os.path.join(self.d, "state"),
                        NOTIFY_CMD=f"cat >> {self.d}/notify.log", VERIFY_S="1", WATCHDOG_NOJOURNAL="1")
        self.children = []

    def tearDown(self):
        if os.path.exists(self.wedge):
            os.remove(self.wedge)                            # lets any blocked stub canary finish
        time.sleep(0.3)
        for p in self.children:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run_wd(self, *args):
        t = time.monotonic()
        r = subprocess.run(["bash", WD, *args], env=self.env, capture_output=True, text=True, timeout=30)
        return r, time.monotonic() - t

    def read(self, name):
        p = os.path.join(self.d, name)
        return open(p).read() if os.path.exists(p) else ""


class Discovery(Base):
    def test_lists_enabled_sshfs_units_with_the_mountpoint_from_execstart(self):
        r, _ = self.run_wd("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(r.stdout.split()), ["sshfs-bus:/mnt/bus:sshfs-bus.service",
                                                    "sshfs-media:/mnt/legacy/old-name/media:sshfs-media.service"])

    def test_list_changes_nothing(self):
        self.run_wd("--list")
        self.assertNotIn("restart", self.read("calls.log"))
        self.assertEqual(self.read("state"), "")


class Detection(Base):
    def test_healthy_mounts_are_never_recovered(self):
        for _ in range(3):
            r, _ = self.run_wd()
            self.assertEqual(r.returncode, 0, r.stderr)
            time.sleep(0.4)
        self.assertNotIn("restart", self.read("calls.log"))
        self.assertEqual(self.read("notify.log"), "")

    def test_a_wedged_mount_is_detected_on_the_next_run_and_healed(self):
        open(self.wedge, "w").close()
        sshfs = subprocess.Popen(["sleep", "60"])            # stands in for the wedged sshfs daemon
        self.children.append(sshfs.pid)
        with open(os.path.join(self.d, "sshfs.pid"), "w") as f:
            f.write(f"{sshfs.pid}\n")
        r, took = self.run_wd()                               # run 1: drops canaries, judges nothing
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("restart", self.read("calls.log"))
        time.sleep(0.4)
        # the run-1 canary is still blocked; the remount will work, so the wedge clears shortly after run 2 starts
        unwedge = subprocess.Popen(["sh", "-c", f"sleep 0.3; rm -f {self.wedge}"])
        r, took = self.run_wd()                               # run 2: the run-1 canary is still alive -> recover
        unwedge.wait()
        calls = self.read("calls.log")
        self.assertIn("fusermount -uz /mnt/bus", calls)
        self.assertIn("systemctl restart sshfs-bus.service", calls)
        self.assertNotIn("restart sshfs-media.service", calls)
        self.assertEqual(sshfs.wait(timeout=5), -signal.SIGKILL)                         # wedged daemon killed
        self.assertIn("RECOVERED: sshfs-bus (/mnt/bus)", self.read("notify.log"))
        self.assertLess(took, 10, "the watchdog itself must never block on the mount")

    def test_still_wedged_after_restart_is_reported_unconfirmed(self):
        open(self.wedge, "w").close()
        self.run_wd()
        time.sleep(0.4)
        r, took = self.run_wd()                               # wedge never clears: verify can't return
        self.assertIn("RECOVERY UNCONFIRMED: sshfs-bus (/mnt/bus)", self.read("notify.log"))
        self.assertLess(took, 10)

    def test_a_reused_pid_is_not_mistaken_for_a_stuck_canary(self):
        other = subprocess.Popen(["sleep", "60"])             # alive, but not our canary
        self.children.append(other.pid)
        with open(os.path.join(self.d, "state"), "w") as f:
            f.write(f"sshfs-bus {other.pid}\n")
        self.run_wd()
        self.assertNotIn("restart", self.read("calls.log"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
