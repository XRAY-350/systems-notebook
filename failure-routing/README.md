# failure-routing

When one step of a multi-stage build pipeline fails, tell the one owner who can fix that step, and
nobody else. Routing is a data file, not code.

```python
from failure_router import plan
for msg in plan(routes, summary, log_tail):
    send(msg["to"], msg["thread"], msg["subject"], msg["body"])
```

This is a standalone rewrite of the routing logic inside a patch-and-publish pipeline I run. That
pipeline downloads an app, builds and injects a mod for it, and publishes the result. Several apps
go through it, and each app's mod has its own owner. Here the sending part is injected, so the whole
decision can be tested without a message bus.

## The problem

Before this, every failure went to one place: the pipeline's own owner. That owner is the right
person for a download failure, but the wrong one for a mod that no longer compiles against a new app
version. So most failures took an extra hop, and the person who could actually fix them heard about
it last.

The requirement, as it was given to me: different steps go to different owners ("download goes to
the pipeline's owner"), and each mod has different needs, "so this is something to determine, not a
formula to apply as a blanket." So the rules live in a file, one entry per app, one recipient list
per step.

## Design decisions

- **The pipeline marks its own phase.** Each stage writes a marker (DOWNLOAD, PATCH, BUILD,
  PUBLISH) before it starts, so a failure is attributed to the stage that was running, not guessed
  from the error text. Failing before any marker is reported as UNKNOWN, which goes to the default owner.
- **A route is a proposal until its owner confirms it.** I drafted each app's routing from its
  pipeline code, but an owner who never agreed to a route should not be woken by it. An unconfirmed
  entry falls back to the default owner, and the message says which proposal was skipped. Each route
  went live only after its owner confirmed it.
- **A shared library adds its owner only when the log blames it.** Several mods compile against
  one shared library. Its owner is added only when the failing log matches a pattern built from
  real compiler and linker output (an `Undefined symbols ... referenced from ... in CoreKit-*.o`
  link error, say), and only for apps that use that library. A plain compile error in the app's own
  code doesn't wake the library's owner.
- **Retryable failures are noise until the retry queue gives up.** A failure the queue will retry
  sends nothing. Either the retry recovers, or the queue eventually dead-letters it, and that final
  failure is reported, with the attempt count. Forced test failures always send, because they exist
  to prove a route works.
- **One thread per app and version.** Every message about one release lands in the same
  conversation, so an owner sees the whole history in one place.

## The step that fails while the run passes

The most useful route turned out to be for a failure that leaves the run green. When a mod's
source no longer compiled, the pipeline fell back to a previously built binary and shipped that, so
the run passed with a possibly stale mod inside. That fallback is now reported as its own BUILD
event, and it is the only way a mod compile error reaches the person who can fix it.

## Verified

`python3 test_failure_router.py` runs 12 checks:

- each step goes to its owner;
- the shared-library owner is added only for a matching link error, and only for apps that use the library;
- an unconfirmed route falls back to the default owner and says so;
- retryable failures stay quiet, while the dead-lettered one and forced tests are reported;
- the messages share one thread.

To show the tests can actually fail, I removed the "unconfirmed" gate from the router: the matching
test failed. In production, each route type was proven with one forced failure on a dry run before it was relied on.
