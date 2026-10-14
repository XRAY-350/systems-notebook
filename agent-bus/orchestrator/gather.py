#!/usr/bin/env python3
"""gather.py: the instant pass of the human's digest. Collects everything WAITING ON THE HUMAN or STUCK,
forward-looking only (history belongs in the progress logs), from:
  * every project's SLATE.md "## Waiting on owner" section (ORCH_SLATE_GLOBS, colon-separated globs)
  * bus messages marked needs_reply with no reply, 12 h to 7 days old
  * approval requests with no SIGNED answer yet (last 14 days); also printable as JSON for a digest poster
  * unread mail in laptop-side (pull-only) lanes
  * optional: failed CI runs in the last 36 h (ORCH_GH_REPOS, "owner/repo" list, needs the gh CLI)
  * dated reminders (reminders.md next to this file), shown from 14 days before their date
Read-only everywhere. Usage: gather.py [--approvals-json]
The bus store is read directly (AGENTMSG_STORE: local, or a mount of the server's store).
"""
import datetime as D, glob, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER_HDR = re.compile(r"^##\s.*waiting on (the )?(owner|you)", re.I)


def owner_section(text):
    """Lines under any '## ... waiting on owner/you' heading, up to the next '## '. Comments and blanks dropped.
    Heading variants are matched on purpose: each project wrote its own, and a strict match silently skipped them."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = bool(OWNER_HDR.match(line)); continue
        if on and line.strip() and not line.strip().startswith("<!--"):
            out.append(line.rstrip())
    return out


def _messages(store):
    by_thread = {}
    for f in glob.glob(os.path.join(store, "threads", "*", "*.json")):
        try:
            m = json.load(open(f))
        except Exception:
            continue
        by_thread.setdefault(m.get("thread"), []).append(m)
    return by_thread


def _age_h(m, now):
    try:
        return (now - D.datetime.fromisoformat(m["ts"])).total_seconds() / 3600
    except Exception:
        return None


def unanswered_needs_reply(store, now, lo_h=12, hi_h=7 * 24):
    rows = []
    for ms in _messages(store).values():
        replied = {m.get("in_reply_to") for m in ms}
        for m in ms:
            age = _age_h(m, now)
            if m.get("needs_reply") and m["id"] not in replied and age is not None and lo_h <= age <= hi_h:
                rows.append((age, m))
    return [m for _a, m in sorted(rows, key=lambda r: -r[0])]


def pending_approvals(store, now, days=14, verify=None):
    """Approval requests with no answer from the human. `verify(msg) -> bool` decides whether an answer counts
    (pass agentmsg.owner_status(...) == 'signed' in production); an unsigned 'owner' reply never closes one."""
    rows = []
    for ms in _messages(store).values():
        answered = {m.get("in_reply_to") for m in ms if m.get("from") == "owner" and (verify is None or verify(m))}
        for m in ms:
            age = _age_h(m, now)
            if m.get("owner_approval") and m["id"] not in answered and age is not None and age <= days * 24:
                rows.append({"id": m["id"], "agent": m["from"], "subject": m.get("subject", "")[:150],
                             "thread": m["thread"], "age_days": round(age / 24, 1)})
    return sorted(rows, key=lambda r: -r["age_days"])


def unread_in_lanes(store, lanes, now, days=7):
    out = {}
    for lane in lanes:
        d = os.path.join(store, "inbox", lane)
        if os.path.isdir(d):
            n = sum(1 for f in os.listdir(d) if f.endswith(".json") and now.timestamp() - os.path.getmtime(os.path.join(d, f)) < days * 86400)
            if n:
                out[lane] = n
    return out


def due_reminders(path, today, ahead_days=14):
    out = []
    if os.path.exists(path):
        for line in open(path):
            m = re.match(r"-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.*)", line.strip())
            if m and D.date.fromisoformat(m.group(1)) <= today + D.timedelta(days=ahead_days):
                out.append((m.group(1), m.group(2)))
    return out


def failed_runs(repos, hours=36):
    since = (D.datetime.now(D.timezone.utc) - D.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for r in repos:
        try:
            runs = json.loads(subprocess.run(["gh", "run", "list", "-R", r, "-L", "60", "--json",
                                              "workflowName,conclusion,createdAt,url"], capture_output=True, text=True, timeout=60).stdout)
        except Exception:
            continue
        latest = {}
        for x in runs:
            if x["createdAt"] >= since and x["workflowName"] not in latest:
                latest[x["workflowName"]] = x                       # latest run per workflow only
        out += [(r, w, x) for w, x in latest.items() if x["conclusion"] == "failure"]
    return out


def main():
    store = os.path.expanduser(os.environ.get("AGENTMSG_STORE", "~/.agent-bus"))
    now = D.datetime.now().astimezone()
    lanes = [a for a in os.environ.get("AGENTMSG_AGENTS", "").split(",") if a.strip()]
    watched = set(os.environ.get("AGENTMSG_WATCHED", ",".join(lanes)).split(","))
    verify = None
    try:   # verify signatures with agentmsg itself when it is importable (it sits one directory up)
        import importlib.machinery, importlib.util
        ld = importlib.machinery.SourceFileLoader("agentmsg", os.path.join(HERE, "..", "agentmsg"))
        am = importlib.util.module_from_spec(importlib.util.spec_from_loader("agentmsg", ld)); ld.exec_module(am)
        verify = lambda m: am.owner_status(m) == "signed"
    except Exception:
        pass
    if "--approvals-json" in sys.argv:
        for row in pending_approvals(store, now, verify=verify):
            print(json.dumps(row))
        return
    print(f"# Digest input, {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print("\n### SLATE 'waiting on owner'")
    for g in [g for g in os.environ.get("ORCH_SLATE_GLOBS", "").split(":") if g]:
        for f in sorted(glob.glob(os.path.expanduser(g))):
            items = owner_section(open(f, errors="replace").read())
            print(f"- **{os.path.basename(os.path.dirname(f))}**: " + ("nothing listed" if not items else ""))
            for i in items:
                print(f"    {i}")
    print("\n### Bus: needs-reply with no reply (12 h to 7 d)")
    rows = unanswered_needs_reply(store, now)
    for m in rows:
        print(f"- {m['from']} -> {','.join(m['to'])} [{m['thread']}] {m['subject'][:90]} (id {m['id']})")
    if not rows:
        print("- none")
    print("\n### Approval requests with no signed answer (14 d)")
    ap = pending_approvals(store, now, verify=verify)
    for r in ap:
        print(f"- {r['agent']}: {r['subject']} ({r['age_days']} d, id {r['id']}, thread {r['thread']})")
    if not ap:
        print("- none")
    print("\n### Unread mail in laptop lanes (7 d)")
    un = unread_in_lanes(store, [l for l in lanes if l not in watched], now)
    print("\n".join(f"- {k}: {v} unread" for k, v in un.items()) or "- none")
    repos = [r for r in os.environ.get("ORCH_GH_REPOS", "").split(",") if r]
    if repos:
        print("\n### CI: failed runs, last 36 h (latest per workflow)")
        fr = failed_runs(repos)
        print("\n".join(f"- {r} / {w}: FAILED {x['createdAt'][:16]}Z {x['url']}" for r, w, x in fr) or "- none")
    print("\n### Dated reminders")
    rem = due_reminders(os.path.join(HERE, "reminders.md"), now.date())
    print("\n".join(f"- {d}: {t}" for d, t in rem) or "- none")


if __name__ == "__main__":
    main()
