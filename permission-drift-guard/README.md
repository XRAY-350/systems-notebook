# permission-drift-guard

Periodic access-control drift detection against a golden manifest — with two different trust
tiers, because not every kind of drift deserves the same response.

```js
const { computeDrift } = require('./drift_guard');
const { corrections, reports } = computeDrift(goldenManifest, liveState);
// corrections: safe to auto-apply
// reports: surface to a human, never auto-touch
```

This is a standalone, generalized rewrite of the core algorithm behind a permission auditor I run
in production, against plain objects instead of a live platform's real API objects — runnable and
testable entirely on its own.

## The real incident behind this

A moderator was found posting in a staff-only announcements channel they shouldn't have had access
to. The platform (Discord, in the original) has a real structural gotcha: a channel's own
permission overwrite for a role *replaces* its parent category's inherited overwrite entirely,
rather than merging with it. So when a role's channel-level overwrite only had an explicit ALLOW
and no explicit DENY, it silently dropped the category's "deny everything by default" inheritance
for everyone holding that role — a permissions leak that looked completely normal in the channel's
own settings UI, because nothing about it looked wrong in isolation.

That bug shape isn't specific to one channel. Any resource with a partial role-level override is a
candidate for the exact same silent leak, which is why the fix wasn't "correct this one channel" —
it was "build a system that catches this shape of drift automatically, on a schedule, everywhere,
without waiting for someone to notice and report it."

## Why two tiers, not one blanket policy

The tempting simple version is "diff against golden, revert anything that changed." That's wrong
for one whole class of legitimate changes: a targeted grant to one specific account (a bot
integration, a one-off staff exception) added *after* the golden snapshot was taken is far more
likely to be a deliberate, still-valid decision than accidental drift. Auto-reverting those would
actively undo real, intended configuration — arguably worse than the drift itself, since it fails
silently from the admin's point of view (they granted something, and it quietly disappeared later).

System-level grants (role-based, in the original) are the opposite: nobody expects those to change
outside of a deliberate, snapshot-worthy admin action, so drift there is overwhelmingly likely to
be either a mistake or an attack, and safe to auto-correct without waiting for a human.

## Verified

Tested with a synthetic reproduction of the real incident shape (a system-tier overwrite losing
its explicit deny) alongside a legitimate new user-tier grant in the same resource: correctly
classifies the system-tier drift as auto-correctable and the user-tier addition as report-only,
and produces zero findings when live state matches the manifest exactly (a negative-case check —
the "nothing to report" path was proven to actually report nothing, not just assumed).
