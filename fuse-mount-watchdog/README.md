# fuse-mount-watchdog

Detect a wedged SSHFS mount and heal it, without the watchdog itself ever getting stuck on the mount it's watching.

```
$ watchdog.sh --list
sshfs-bus:/mnt/bus:sshfs-bus.service
sshfs-media:/mnt/legacy/old-name/media:sshfs-media.service
$ watchdog.sh            # from a systemd timer every 3 minutes
2026-11-22T03:12:09Z WEDGED: sshfs-bus (/mnt/bus): the last canary did not return within one interval; recovering
2026-11-22T03:12:10Z RECOVERED: sshfs-bus (/mnt/bus) on box-a: remounted via sshfs-bus.service (wedged sshfs pid 81234)
```

This is a cleaned-up copy of the watchdog on the machines in a small server fleet that share files with each other
over SSHFS: an agent message bus, backups, a drop folder.

**Who wrote it:** AI coding agents (Claude Code) wrote it, the tests, and both later fixes, to my requirements.
The incidents that shaped it were found in production logs.

## The problem

`sshfs -o reconnect` sometimes doesn't reconnect after a real network drop. The mountpoint is then left answering
every `stat()` with an uninterruptible hang (D state), forever, silently. It happened on one machine, then again
independently on another. The second time it went unnoticed for five days, until it froze an agent's own session
that happened to touch the path.

The obvious check doesn't work. `timeout 5 stat /mnt/bus` spawns a `stat` that goes into D state itself, and
`SIGTERM` doesn't reliably free a process blocked in a FUSE call. A manual recovery once took over an hour.

## Design decisions

- **Never wait on the mount.** Each run starts a background "canary" `stat` per mount, records its PID, and leaves.
  The next run, one timer interval later, asks whether that canary is still alive. Alive after a whole interval
  means wedged. The stuck canary is never killed (it can't be); it returns by itself the moment the mount is lazily
  unmounted.
- **Recovery is the procedure that worked by hand:** lazy unmount (`fusermount -uz`), kill the wedged sshfs daemon,
  restart the mount's unit, then a verify that has a few seconds to answer and is itself just another background
  stat, so a still-wedged mount yields "unconfirmed", not a hung watchdog.
- **A reused PID is not a wedge.** A healthy canary exits at once, and its PID can later belong to something
  unrelated. The watchdog checks that the live process is still `stat <that mountpoint>` before acting.
- **Every output is detached.** The canaries and the verify run with stdin, stdout and stderr redirected. The test
  harness found the verify subshell still holding the script's stdout, which made anything waiting on the script's
  output hang for as long as the mount stayed wedged.

## Two fixes that came from production

**The watch list came from the unit name, and the mounts that mattered weren't on it.** The first version had a
hard-coded list of mounts, so one machine had no watchdog at all. The next version discovered mounts from unit
names (`sshfs-bus.service` was assumed to mount `/mnt/bus`). Then a migration kept some mounts at their old paths
under new unit names, and the name rule silently dropped every mount that carried that machine's message-bus writes.
The class fix: read the mountpoint from each unit's own `ExecStart`, the first path under the mount root after the
remote `host:path`. `--list` prints exactly what will be watched, so the list can be checked rather than assumed.

**A 3-minute detection took 8 minutes because of `KillMode`.** A oneshot service's default `KillMode=control-group`
makes systemd kill everything the run left behind when the script exits. The canary is left behind on purpose, and a
canary stuck on a wedged mount can't be killed, so the unit sat in its stop phase for about four and a half
minutes, and the next run (the one that heals) couldn't start. The unit now has `KillMode=process` (see
`fuse-mount-watchdog.service`).

## Files

- `watchdog.sh`: the watchdog (bash, coreutils, `ps`, `pgrep`, `fusermount`, systemd).
- `fuse-mount-watchdog.service`, `fuse-mount-watchdog.timer`: oneshot with `KillMode=process`, every 3 minutes.
- Settings by environment: `UNIT_GLOB` (default `sshfs-*.service`), `MOUNT_ROOT` (`/mnt/`), `STATE_FILE`, `VERIFY_S`
  (3), and `NOTIFY_CMD`, which gets one line on stdin per recovery (a chat webhook, a pager).

## Verified

`python3 test_watchdog.py`: 6 tests, about 7 seconds, no root and no FUSE. `systemctl`, `fusermount`, `pgrep` and
`stat` are stubs first on `PATH`. A wedged mount is a `stat` stub that blocks while a flag file exists.

- discovery lists only enabled sshfs units, with mountpoints read from `ExecStart` (including one whose path has
  nothing to do with its unit name), and `--list` changes nothing;
- healthy mounts are never recovered over several runs;
- a wedged mount is detected on the next run: it is lazily unmounted, its daemon killed and its unit restarted,
  and only that unit; the recovery is reported, and the run takes well under the timer interval even though the
  canary is still stuck;
- a mount still wedged after the restart is reported as unconfirmed, again without blocking;
- a live process that merely reuses an old canary PID is not treated as a wedge.

Four mutations, each caught: deriving the mountpoint from the unit name, dropping the reused-PID check, making the
canary synchronous, and skipping the restart.
