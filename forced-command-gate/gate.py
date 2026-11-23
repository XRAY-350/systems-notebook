#!/usr/bin/env python3
"""gate.py: the only program a restricted SSH key may run. Exact verbs in, a fixed argv out, everything else refused.

Installed as the forced command of one key line, for example:

    restrict,from="192.0.2.10",command="/usr/local/sbin/gate" ssh-ed25519 AAAA... caller-key

sshd then ignores what the client asked to run, starts this file, and puts the client's string in SSH_ORIGINAL_COMMAND.
That string is the whole request. It is split with shlex (never handed to a shell), every token must match a small
character class, and the first token must be a verb in VERBS. Each verb checks its own arguments and runs one fixed
helper. A refusal prints one line to stderr and exits 9, so a caller can tell "refused" from "the helper failed".

Secrets (a one-time login code, say) travel on STDIN, which is passed straight through to the helper and never read,
logged or echoed here. A secret put in argv is refused, because argv ends up in process lists and logs.

The same file also works when called with argv and no SSH_ORIGINAL_COMMAND, so callers can switch to the new calling
form first and the key line can be swapped to the forced command afterwards without breaking anything.
"""
import os
import re
import shlex
import subprocess
import sys
import syslog

BIN = os.environ.get("GATE_BIN", "/usr/local/libexec/gate")     # where the fixed helpers live (tests point it at stubs)
SELF_NAMES = ("gate", "/usr/local/sbin/gate")                     # a caller may prefix the program name; it is dropped
TOKEN = re.compile(r"^[A-Za-z0-9@._-]{1,80}$")                     # no spaces, quotes, $, `, ;, |, &, <, >, / or globs
REFUSED = 9

# The allowlists ARE the policy. Keep them short and literal.
USERS = ("root", "operator")
RESTARTABLE = ("app-web", "app-worker")                           # nothing else can be restarted through this key
MAX_STATUS_UNITS = 40


def log(msg):
    if os.environ.get("GATE_NOLOG"):          # tests keep fake calls out of the real journal
        return
    try:
        syslog.openlog("gate")
        syslog.syslog(syslog.LOG_INFO, msg)
    except Exception:
        pass


def refuse(why):
    log(f"refused: {why}")
    print(f"gate: refused: {why}", file=sys.stderr)
    sys.exit(REFUSED)


def run(argv, timeout):
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 124, f"{type(e).__name__}\n"


# ------------------------------------------------------------------ verbs
def v_status(args):
    """Read-only: is each unit active? 1..MAX_STATUS_UNITS unit names."""
    if not 1 <= len(args) <= MAX_STATUS_UNITS:
        refuse(f"status needs 1..{MAX_STATUS_UNITS} units")
    for u in args:
        _, out = run([f"{BIN}/is-active", u], timeout=5)
        print(f"{u}={out.strip() or 'inactive'}")
    return 0


def v_login_code(args):
    """Deliver a one-time code to the login helper. The code arrives on STDIN only."""
    if len(args) != 1 or args[0] not in USERS:
        refuse(f"login-code takes exactly one user: {' | '.join(USERS)}")
    log(f"login-code {args[0]}")              # the verb and the user, never the code
    target = [f"{BIN}/login-code", args[0]]
    os.execv(target[0], target)               # stdin/stdout go straight to the helper


def _restart(args, dry):
    verb = "check-restart" if dry else "restart"
    if len(args) != 1 or args[0] not in RESTARTABLE:
        refuse(f"{verb} allows only: {' | '.join(RESTARTABLE)}")
    rc, out = run([f"{BIN}/restart"] + (["--dry-run"] if dry else []) + [args[0]], timeout=120)
    sys.stdout.write(out)
    log(f"{verb} {args[0]} rc={rc}")
    return rc


VERBS = {
    "status": v_status,
    "login-code": v_login_code,
    "restart": lambda a: _restart(a, dry=False),
    "check-restart": lambda a: _restart(a, dry=True),    # same checks and exit codes, never acts
}


def parse(argv):
    raw = os.environ.get("SSH_ORIGINAL_COMMAND")
    if raw is not None:                       # forced-command mode: the client's string is the request
        if any(not (" " <= c <= "~") for c in raw):
            refuse("control or non-ASCII characters in the request")   # shlex would treat a newline as a separator
        try:
            argv = shlex.split(raw)
        except ValueError:
            refuse("unparseable request")
        if argv[:1] and argv[0] in SELF_NAMES:
            argv = argv[1:]
    for i, a in enumerate(argv):
        if not TOKEN.match(a):                # name the position, not the value: a bad token may be a pasted secret
            refuse(f"argument {i + 1} has characters outside [A-Za-z0-9@._-] or is too long")
    if not argv:
        refuse("no verb")
    return argv[0], argv[1:]


def main(argv):
    verb, args = parse(argv)
    fn = VERBS.get(verb)
    if fn is None:
        refuse(f"unknown verb {verb!r}")
    sys.exit(fn(args))


if __name__ == "__main__":
    main(sys.argv[1:])
