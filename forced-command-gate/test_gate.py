#!/usr/bin/env python3
"""Tests for gate.py: verbs route to their fixed helper, every hostile request is refused with exit 9 and runs nothing,
and a secret only ever travels on stdin. Helpers are stubs in a temp dir. Run: python3 test_gate.py"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "gate.py")
SECRET = "SENTINEL-CODE-7f3a9c"

HOSTILE = [
    # not a verb / straight shell
    "", "bash", "sh -c id", "id", "/bin/sh", "python3 -c 1", "cat /etc/shadow", "systemctl restart app-web",
    # shell metacharacters glued onto a real verb
    "status ssh;id", "status ssh && id", "status ssh || id", "status ssh|id", "status $(id)", "status `id`",
    "status ssh > /tmp/x", "status ssh < /etc/passwd", "status ssh &", "status 'ssh id'", 'status "a b"',
    "status ../../etc/passwd", "status /etc/passwd", "status *", "status ssh\nid", "status 'unterminated",
    # real verbs, wrong arguments
    "status", "status " + " ".join(f"u{i}" for i in range(41)), "restart", "restart app-db", "restart app-web extra",
    "restart APP-WEB", "restart app-web-confirmed", "check-restart nginx", "check-restart", "login-code",
    "login-code nobody", "login-code root extra", "login-code operator root",
    # a secret in argv instead of stdin
    f"login-code root {SECRET}",
    # look-alikes
    "Status ssh", "restart\tapp-web;id", "x" * 200,
]


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.calls = os.path.join(self.d, "calls.log")
        rec = f'echo "$(basename "$0") $*" >> {self.calls}'
        stub(f"{self.d}/is-active", rec + '\n[ "$1" = ssh ] && echo active || echo inactive')
        stub(f"{self.d}/login-code", rec + f'\ncat > {self.d}/code-stdin; echo "code delivered"')
        stub(f"{self.d}/restart", rec + '\nfor a; do last=$a; done; [ "$last" = app-worker ] && { echo "busy, not restarted"; exit 4; }; echo "restarted $last"')
        self.env = dict(os.environ, GATE_BIN=self.d, GATE_NOLOG="1")
        self.env.pop("SSH_ORIGINAL_COMMAND", None)

    def call(self, *argv, original=None, stdin=""):
        env = dict(self.env)
        if original is not None:
            env["SSH_ORIGINAL_COMMAND"] = original
        return subprocess.run([sys.executable, "-I", GATE, *argv], input=stdin, capture_output=True, text=True,
                              env=env, timeout=30)

    def logged(self):
        return open(self.calls).read() if os.path.exists(self.calls) else ""


class Routing(Base):
    def test_status_is_read_only_and_bounded(self):
        r = self.call(original="status ssh missing-unit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ssh=active", r.stdout)
        self.assertIn("missing-unit=inactive", r.stdout)

    def test_program_name_prefix_is_accepted(self):
        self.assertEqual(self.call(original="/usr/local/sbin/gate status ssh").returncode, 0)
        self.assertEqual(self.call(original="gate status ssh").returncode, 0)

    def test_argv_mode_works_before_the_key_line_is_swapped(self):
        r = self.call("status", "ssh")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_restart_passes_the_helpers_exit_code_through(self):
        r = self.call(original="restart app-web")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "restarted app-web"))
        r = self.call(original="restart app-worker")
        self.assertEqual(r.returncode, 4)                    # the helper's own refusal, reported as-is, not as 9

    def test_check_restart_is_the_dry_run(self):
        self.call(original="check-restart app-web")
        self.assertIn("restart --dry-run app-web", self.logged())

    def test_secret_reaches_the_helper_on_stdin_only(self):
        r = self.call(original="login-code root", stdin=SECRET)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(f"{self.d}/code-stdin").read(), SECRET)   # delivered intact
        self.assertNotIn(SECRET, self.logged())                         # never in any helper's argv
        self.assertNotIn(SECRET, r.stdout + r.stderr)                   # never echoed


class Hostile(Base):
    def test_every_hostile_request_is_refused_and_runs_nothing(self):
        for req in HOSTILE:
            with self.subTest(req=req[:40]):
                r = self.call(original=req)
                self.assertEqual(r.returncode, 9, f"{req!r}: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
                self.assertIn("refused", r.stderr)
        self.assertEqual(self.logged(), "", "a refused request must not start any helper")

    def test_refusals_never_echo_the_rejected_value(self):
        r = self.call(original=f"login-code root {SECRET}")
        self.assertNotIn(SECRET, r.stderr)
        r = self.call(original="status $(cat${IFS}/etc/shadow)")
        self.assertNotIn("shadow", r.stderr)

    def test_hostile_count(self):
        self.assertGreaterEqual(len(HOSTILE), 40)


if __name__ == "__main__":
    unittest.main(verbosity=1)
