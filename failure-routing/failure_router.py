#!/usr/bin/env python3
"""failure_router.py: decide WHO gets told (and woken) when a step of a multi-stage pipeline fails.

Routing is data (routes.json), not code. Pure functions; sending is injected, so the whole decision is testable
without a message bus.

    from failure_router import plan
    for msg in plan(routes, summary, log_tail):
        send(msg["to"], msg["thread"], msg["subject"], msg["body"])

summary: {"app_id", "version", "phase", "run_url", "attempts", "retryable", "dead_letter", "retry_reason", "test"}
phase:   the pipeline's own marker for where it was when it failed (e.g. DOWNLOAD, PATCH, BUILD, PUBLISH, UNKNOWN).
"""
import json
import re
import sys


def recipients(routes, app_id, phase, log_tail):
    """(recipients, note). note names a proposed route that was NOT used because its owner has not confirmed it."""
    default = list(routes.get("default") or [])
    app = (routes.get("apps") or {}).get(app_id) or {}
    proposed = list((app.get("phases") or {}).get(phase) or default)
    # A failure inside a SHARED component (a library several apps compile against) also goes to that
    # component's owner, but only when the log actually shows the shared component failing.
    pat = routes.get("shared_error_pattern")
    if app.get("shared") and pat and re.search(pat, log_tail or ""):
        proposed += [r for r in app["shared"] if r not in proposed]
    # An unconfirmed entry is a PROPOSAL: never wake an owner on routing they did not agree to.
    if app and not app.get("confirmed") and proposed != default:
        return default, f"proposed route {', '.join(proposed)} is not confirmed by its owner yet"
    return proposed, ""


def is_noise(summary):
    """A failure the retry queue will handle is not news yet. Its dead-lettered final failure is.
    Forced test failures are never noise (they exist to prove the route)."""
    if summary.get("test"):
        return False
    return bool(summary.get("retryable")) and not summary.get("dead_letter")


def thread_for(app_id, version):
    return re.sub(r"[^A-Za-z0-9._-]", "-", f"fail-{app_id}-{version}")[:80]


def plan(routes, summary, log_tail=""):
    """The messages to send: one per recipient, one thread per app+version. [] when it is noise."""
    if is_noise(summary):
        return []
    app_id = summary.get("app_id") or "unknown"
    version = summary.get("version") or "latest"
    phase = (summary.get("phase") or "UNKNOWN").upper()
    test = bool(summary.get("test"))
    to, note = recipients(routes, app_id, phase, log_tail)
    attempts = int(summary.get("attempts") or 1)
    if attempts > 1:
        retried = f"yes, attempt {attempts}" + (" and the retry queue gave up" if summary.get("dead_letter") else "")
    else:
        retried = "no (first attempt" + (", not retryable)" if not summary.get("retryable") else ")")
    subject = f"{'TEST ' if test else ''}Pipeline failed: {app_id} {version} at {phase}"
    body = "\n".join([
        f"{'TEST (forced failure; nothing to fix). ' if test else ''}The pipeline failed.",
        f"App: {app_id}   Version: {version}",
        f"Failed step: {phase}" + (f"  ({summary['retry_reason']})" if summary.get("retry_reason") else ""),
        f"Run: {summary.get('run_url', '?')}",
        f"Retry already happened: {retried}",
        *([f"Routing: {note}; sent to the default owner instead."] if note else []),
        "",
        "Last lines of the failed step:",
        (log_tail or "(no log captured)").rstrip()[-3500:],
    ])
    thread = thread_for(app_id, version)
    return [{"to": r, "thread": thread, "subject": subject, "body": body} for r in to]


if __name__ == "__main__":
    # usage: failure_router.py routes.json summary.json [log_tail.txt]   (prints the plan, sends nothing)
    routes = json.load(open(sys.argv[1]))
    summary = json.load(open(sys.argv[2]))
    tail = open(sys.argv[3], errors="replace").read() if len(sys.argv) > 3 else ""
    for m in plan(routes, summary, tail):
        print(f"--- to {m['to']} (thread {m['thread']}): {m['subject']}\n{m['body']}\n")
