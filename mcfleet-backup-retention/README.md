# mcfleet-backup-retention

A 26-line grandfather-father-son (GFS) backup retention policy, implemented as a pure filter over
filenames: read backup filenames on stdin, print the ones to delete on stdout. No database, no
external state — the schedule itself lives entirely in each backup's own timestamp.

```
ls /backups | python3 gfs_prune.py world-backup | xargs -I{} rm /backups/{}
```

## The policy

Keeps: the newest backup per day for the last 7 days, the newest backup per 7-day bucket for the
last ~3 buckets (~2 weeks), and the newest backup per calendar month for the last 2 months.
Everything else gets printed for deletion. At any given time this keeps roughly ten to twelve aged
restore points spanning weeks to months, without needing to track *when* a backup was ever
previously kept — every decision is re-derivable from the current list of filenames alone.

## Why "print what to delete" instead of "do the deleting"

Keeping the retention *decision* completely separate from the deletion *action* means the policy
itself can be tested safely against a real or synthetic file list with zero risk — run it, read
the output, and nothing has been touched. The actual deletion is one `xargs rm` away, deliberately
not baked into the script that makes the keep/delete call.

## Trusting it before trusting it with real backups

Before this logic was trusted against the live (small, ~5-backup) production set, it was run
against a synthetic 40-day, 480-file dataset first — enough volume and enough day/week/month
boundary crossings to actually exercise every branch of the retention logic, not just the common
case. The real production set was too small on its own to prove the month/week boundary handling
was correct; a bug in either could sit unnoticed for weeks in a 5-file set and still trip a rule
against the boundary math instantly at 480.

Re-verified the same way while extracting this: piped a synthetic 40-day / 2-hour-interval dataset
(480 filenames) through the actual script above — it kept a small, correctly bounded working set
and deleted the rest, and separately confirmed it degrades safely on malformed input (filenames
that don't match the expected pattern are silently ignored rather than crashing the whole run,
and empty input produces empty output rather than an error).
