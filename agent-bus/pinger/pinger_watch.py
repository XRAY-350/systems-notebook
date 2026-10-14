#!/usr/bin/env python3
"""pinger_watch.py: wake laptop-side agent sessions when they get bus mail.

Server-side agents have agentmsg-watchd. Laptop-side sessions (pull-only agents, i.e. not in AGENTMSG_WATCHED)
cannot be typed into from the server, but a small always-on "pinger" agent session on the server CAN message them
through the agent platform's remote-messaging feature. So this watcher does the cheap part (notice mail, batch it,
decide when) and hands one RELAY line to the pinger session, which does the part that needs an agent (find the
right session and message it; see PINGER.md).

  * batching: wait SETTLE seconds after the newest file, so a burst becomes one RELAY line per lane
  * re-wake while UNREAD: 15, 30, 60, 120, then every 240 min, up to 72 h after the message ARRIVED. A laptop that
    was asleep or closed still gets woken once it is back; the bus keeps the message either way.
  * quiet hours (AGENTMSG_QUIET): non-urgent mail waits, urgent goes through (same rule as agentmsg-watchd)
  * only types into the pinger session when it is idle (PINGER_STATE_CMD), and never reads or moves mail itself
  * subjects are sanitised (printable ASCII, no double quotes, truncated): they are data written by other
    agents and end up inside another agent's prompt
Config: AGENTMSG_* as for agentmsg; PINGER_STATE_CMD / PINGER_WAKE_CMD (placeholder {text}), same contract as
WATCHD_STATE_CMD / WATCHD_WAKE_CMD; PINGER_STATE file (default ~/.local/state/agent-bus-pinger.json).
"""
import importlib.machinery, importlib.util, json, os, re, shlex, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
_path = os.environ.get("PINGER_AGENTMSG_PATH", os.path.join(HERE, "..", "agentmsg"))
_loader = importlib.machinery.SourceFileLoader("agentmsg", _path)
am = importlib.util.module_from_spec(importlib.util.spec_from_loader("agentmsg", _loader)); _loader.exec_module(am)

STATE = os.path.expanduser(os.environ.get("PINGER_STATE", "~/.local/state/agent-bus-pinger.json"))
POLL, SETTLE = 3, 8
RETRY_MIN = [15, 30, 60, 120, 240]     # minutes between re-wakes; the last value repeats
MAX_AGE_H = 72


def log(m):
    print(f"[pinger-watch] {m}", flush=True)


def lanes():
    return [a for a in am.AGENTS if a not in am.WATCHED]


def inbox_files():
    out = {}
    for lane in lanes():
        for p in am.inbox_files(lane):
            out[f"{lane}/{os.path.basename(p)}"] = p
    return out


def due(entry, now):
    """entry = [wakes_sent, last_wake_ts, arrived_ts]. True when the next re-wake is due."""
    wakes, last, arrived = entry
    if now - arrived > MAX_AGE_H * 3600:
        return False
    return now - last >= RETRY_MIN[min(wakes - 1, len(RETRY_MIN) - 1)] * 60


def clean(s, n=80):
    return re.sub(r"[^\x20-\x7e]", " ", str(s or "")).replace('"', "'")[:n].strip()


def relay_text(msgs, seen):
    """msgs: {lane/file: message}. One RELAY clause per lane, joined by ' || '."""
    by_lane = {}
    for k, m in sorted(msgs.items()):
        n = seen.get(k, [0])[0]
        by_lane.setdefault(k.split("/")[0], []).append(
            f'{clean(m.get("id"), 12)} from {clean(m.get("from"), 24)} "{clean(m.get("subject"))}"'
            + (f" (re-wake {n}, still unread)" if n else ""))
    return " || ".join(f"RELAY {lane}: {len(v)} new bus message(s): " + "; ".join(v) for lane, v in by_lane.items())


def _cmd(var, **kw):
    c = os.environ.get(var)
    return c.format(**{k: shlex.quote(str(v)) for k, v in kw.items()}) if c else None


def pinger_idle():
    c = _cmd("PINGER_STATE_CMD")
    if not c:
        return True
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=10).stdout.strip() == "idle"
    except Exception:
        return False


def send_relay(text):
    c = _cmd("PINGER_WAKE_CMD", text=text)
    if not c:
        print(text, flush=True); return True
    try:
        return subprocess.run(c, shell=True, timeout=90).returncode == 0
    except Exception:
        return False


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return None


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w")); os.replace(tmp, STATE)


def tick(seen, pending, last_new, now):
    """One poll. Returns (relay_text or None, last_new). Mutates seen/pending. Separated from main() for tests."""
    files = inbox_files()
    for k in [k for k in seen if k not in files]:
        seen.pop(k)                                         # read (moved out of the inbox): done
    for k, p in files.items():
        if k in pending:
            continue
        if k not in seen:
            pending[k] = p; last_new = now                  # new mail
        elif due(seen[k], now):
            pending[k] = p                                  # still unread: wake again
    for k in [k for k in pending if k not in files]:
        pending.pop(k)
    if not pending or now - last_new < SETTLE:
        return None, last_new
    msgs = {}
    for k, p in pending.items():
        try:
            msgs[k] = am.load(p)
        except Exception:
            msgs[k] = {}
    if am.in_quiet_hours(am.now()) and not any(m.get("urgent") for m in msgs.values()):
        return None, last_new
    return relay_text(msgs, seen), last_new


def mark_sent(seen, pending, now):
    for k, p in pending.items():
        prev = seen.get(k)
        if prev:
            seen[k] = [prev[0] + 1, now, prev[2]]
        else:
            try:
                arrived = os.path.getmtime(p)
            except OSError:
                arrived = now
            seen[k] = [1, now, arrived]
    pending.clear()


def main():
    seen = load_state()
    if seen is None:                                        # first start: mail already waiting is not news
        t = time.time(); seen = {k: [1, t, t] for k in inbox_files()}; save_state(seen)
        log(f"initialised; {len(seen)} existing message(s) marked seen")
    pending, last_new = {}, 0.0
    while True:
        now = time.time()
        text, last_new = tick(seen, pending, last_new, now)
        if text and pinger_idle():
            if send_relay(text):
                mark_sent(seen, pending, now); save_state(seen); log("relayed: " + text)
            else:
                log("relay not confirmed; retrying next poll")
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
