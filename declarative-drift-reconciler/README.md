# declarative-drift-reconciler

Declarative infrastructure-state checking with self-heal: describe what SHOULD be true in one
small manifest, check it against reality, optionally fix the safe cases, and report only what's
left.

```python
from reconciler import reconcile, print_report

manifest = [
    {"name": "nginx-conf", "live_path": "/etc/nginx/sites-enabled/app",
     "source_path": "repo/nginx.conf"},
    # ... every file that's supposed to be deployed somewhere
]
report = reconcile(manifest, apply_fixes=True)
print_report(report)
```

This is a standalone, generalized version of the core of a server-reconciliation tool I run
against real production infrastructure. The full tool also checks OS-level state — installed
packages, file ownership, cron entries, running services, TLS certificate expiry — through a
pluggable SSH-or-local command runner; that plumbing is infrastructure-specific and left out here.
What's shown is the part that generalizes: hash-based drift detection with a bounded, explicit fix
path, reduced to its simplest real form (comparing local files), still doing the actual thing that
matters — proving a live system matches its declared source of truth, or saying exactly why not.

## Why this exists

The obvious way to know if a server's configuration has drifted from what it's supposed to be is
to remember to check. That doesn't scale, and it silently fails exactly when it matters most: a
manual live edit that never gets pushed back to the repo, or a deploy step that partially failed
without anyone noticing. This tool turns "does the live system match what we think it should be"
from a thing a human has to remember into a thing that gets asked, and answered, on a schedule.

**A real root cause this exact pattern caught**: a deploy workflow was landing on the wrong kind of
CI runner by chance roughly half the time, which happened to look like six consecutive unrelated
failures over 48 hours before anyone traced it to the actual cause. The fix wasn't "retry harder" —
it was checking, directly, which specific runner an actual failing run had executed on, rather than
assuming the earlier failures were already-understood bugs repeating.

## The three-tier report, on purpose

`GREEN` / `YELLOW` / `RED` isn't just cosmetic — it's the difference between "confirmed correct,"
"couldn't even check," and "confirmed wrong." Collapsing those into a single pass/fail bit would
hide exactly the failure mode worth catching: a check that silently can't run (a missing
comparison target, a permissions error) looks identical to "everything's fine" unless it's given
its own explicit, visible category.

## Self-heal, but re-verified, not assumed

`apply_fixes=True` copies the source over the drifted/missing live file — but the report it returns
reflects a REAL RE-CHECK of the fixed file's hash after the copy, not just "the fix command didn't
throw an exception." A fix that silently failed to actually take effect (a permissions issue, a
symlink pointing somewhere unexpected) still shows up as RED in the final report rather than being
assumed successful because the copy call itself didn't error.

## Verified

Tested against four real cases in one run: a genuinely drifted file (different content, same
name), a missing file, a file that already matches exactly, and an entry whose own source file
doesn't exist (the YELLOW "can't even check" case). `apply_fixes=True` correctly turned both the
drifted and missing cases into confirmed-matching GREEN results — verified by reading the actual
file contents afterward, not by trusting the report alone.
