#!/usr/bin/env python3
"""Run: python3 test_failure_router.py"""
import json
import os
from failure_router import plan, recipients, is_noise

routes = json.load(open(os.path.join(os.path.dirname(__file__), "routes.example.json")))
LINK_ERR = 'Undefined symbols for architecture arm64:\n  "_ck_menu_add", referenced from:\n      _setup in CoreKit-a1b2.o\n'
n = 0
def ok(name, cond):
    global n
    assert cond, name
    n += 1
    print("ok ", name)

ok("download failure goes to the default owner", recipients(routes, "com.example.puzzle", "DOWNLOAD", "")[0] == ["pipeline-owner"])
ok("patch failure goes to that app's owner", recipients(routes, "com.example.puzzle", "PATCH", "")[0] == ["puzzle-mod-owner"])
ok("shared-library link error adds its owner", recipients(routes, "com.example.puzzle", "BUILD", LINK_ERR)[0] == ["puzzle-mod-owner", "corekit-owner"])
ok("an ordinary compile error does not wake the shared owner",
   recipients(routes, "com.example.puzzle", "BUILD", "main.m:12: error: use of undeclared identifier 'x'")[0] == ["puzzle-mod-owner"])
ok("shared owner only for apps that use the shared library", recipients(routes, "com.example.reader", "PATCH", LINK_ERR)[0] == ["reader-mod-owner"])
to, note = recipients(routes, "com.example.racer", "PATCH", "")
ok("unconfirmed route falls back to default and says so", to == ["pipeline-owner"] and "not confirmed" in note)
ok("unknown app goes to the default owner", recipients(routes, "com.example.none", "PATCH", "")[0] == ["pipeline-owner"])

base = {"app_id": "com.example.puzzle", "version": "2.1", "phase": "PATCH", "run_url": "https://ci.example/run/1"}
ok("a retryable failure is noise (the queue will retry)", is_noise(dict(base, retryable=True)) and plan(routes, dict(base, retryable=True)) == [])
ok("the dead-lettered final failure is reported", len(plan(routes, dict(base, retryable=True, dead_letter=True, attempts=4))) == 1)
ok("a forced test failure is always reported", len(plan(routes, dict(base, retryable=True, test=True))) == 1)
msgs = plan(routes, dict(base, phase="BUILD"), LINK_ERR)
ok("one message per recipient, one shared thread", [m["to"] for m in msgs] == ["puzzle-mod-owner", "corekit-owner"]
   and len({m["thread"] for m in msgs}) == 1 and msgs[0]["thread"] == "fail-com.example.puzzle-2.1")
ok("the body carries the failing log lines", "referenced from" in msgs[0]["body"])
print(f"\n{n} tests passed")
