# gated-auto-update

A human-approve-once, apply-when-safe update state machine: check for updates, require a single
human approval, then apply automatically the next time the system is in a safe state — with a
hard guard against auto-applying a real version migration, and treating a failed apply's automatic
rollback as a normal, terminal (not error) outcome.

```js
const { createGatedUpdater } = require('./gated_updater');

const updater = createGatedUpdater({
  checkForUpdates: async () => ({ signature: '...', changes: [...] }),
  isSafeWindow:    async () => /* e.g. zero active users right now */ true,
  checkMigrationGuard: async () => ({ migrationRequired: false }),
  apply:           async () => 'success' | 'rolled_back' | 'deferred',
});

await updater.checkAndNotify();   // -> { notified: true, plan: [...] } if there's something new
updater.approve();                // one human click arms it
await updater.tryApply();         // called on a schedule; only acts once armed AND safe
```

This is a standalone, generalized version of the core state machine behind a mod auto-updater I
run for a game server fleet: daily check → post a pending-changes summary → a human clicks
approve once → the system applies it automatically the next time the server has zero players
online, with its own backup-and-verify step that auto-rolls-back on a failed boot. Every piece of
real I/O (checking for updates, checking whether it's currently safe to touch anything, the actual
apply-with-rollback step) is injected as a plain async function, so the state machine itself is
fully testable with zero real subprocesses, files, or a live server anywhere near it.

## Why "approve once," not "approve this specific apply"

The naive design — notify, human approves, apply immediately — doesn't fit a live system that
can't tolerate downtime whenever it happens to be convenient for a human to click a button. This
decouples the human decision (should these changes be applied AT ALL) from the system's own
judgment about WHEN it's actually safe to touch anything. One approval "arms" the plan; a separate,
continuously-running check applies it the moment conditions are right, which could be seconds or
hours after the approval.

## The migration guard, and why it can't be bypassed by staying armed longer

Not every "update" is the same kind of decision. A routine version bump within the same major
version is safe to fully automate. A cutover to a different target version entirely is a
deliberate, by-hand decision — the two can look identical in a raw diff (both are "a version
number changed") but carry very different risk. `checkMigrationGuard` runs on every single apply
attempt, not just the first one after approval, specifically so that staying armed for a long time
(waiting for a safe window) can never quietly turn a migration into something that slips through
on a technicality — if the guard would refuse it now, it refuses it every time, and disarms rather
than leaving a refused plan sitting there to be retried forever.

## Deferred vs. terminal, and why that distinction matters

`deferred` (the safe window closed in the narrow gap between checking it and actually starting the
apply — a real race, not a hypothetical one) is the ONLY outcome that leaves the plan armed to
retry. `success` and `rolled_back` are both terminal: either the change landed, or it failed and
was safely undone, and in both cases there's nothing left to apply — leaving the plan armed after
either would mean silently retrying something that already reached a real conclusion.

## Verified

Six real scenarios run end to end (not just the type-level shape): a full notify → approve →
deferred → armed-and-retryable path with a real state check after the deferral; a dismiss that
correctly prevents `tryApply` from ever calling the injected apply function; the migration guard
refusing an apply and disarming even though the plan was armed; a clean success terminating the
plan; a `rolled_back` outcome ALSO terminating the plan (confirming a failed-but-safely-undone
apply isn't left sitting armed to retry indefinitely); and confirming a duplicate `checkForUpdates`
call with an unchanged plan signature correctly does not re-notify.
