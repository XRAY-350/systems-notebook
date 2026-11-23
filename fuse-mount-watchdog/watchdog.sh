#!/bin/bash
# watchdog.sh: detect and self-heal a wedged SSHFS (FUSE) mount without ever blocking on it.
#
# A wedged FUSE mount answers stat() with an uninterruptible hang, forever. `timeout 5 stat /mnt/x` is not a check:
# the stat itself goes into D state and SIGTERM doesn't reliably free it. So this script never waits on a mount. Each
# run drops a background "canary" stat per mount and records its PID; the NEXT run (one timer interval later) checks
# whether that canary is still alive. Still alive after a whole interval = wedged. The stuck canary is never killed
# (it can't be); it returns by itself the moment the mount is lazily unmounted.
#
# Which mounts: every ENABLED unit matching $UNIT_GLOB whose ExecStart runs sshfs. The mountpoint is READ from that
# ExecStart line (the first path argument after the remote host:path), never derived from the unit's name.
#
# Recovery: lazy unmount, kill the wedged sshfs daemon, restart the unit, then a bounded verify that itself never
# blocks: a background stat gets $VERIFY_S seconds; if it hasn't returned, the result is "unconfirmed".
#
# Run as root from a oneshot timer unit with KillMode=process (see README: with the default control-group kill mode,
# systemd tries to kill the canary at the end of every run).
#
#   watchdog.sh           check and heal
#   watchdog.sh --list    print the mounts it would watch (name:mountpoint:unit) and change nothing
set -u
UNIT_GLOB="${UNIT_GLOB:-sshfs-*.service}"
MOUNT_ROOT="${MOUNT_ROOT:-/mnt/}"
STATE_FILE="${STATE_FILE:-/var/lib/fuse-mount-watchdog/state}"
VERIFY_S="${VERIFY_S:-3}"
NOTIFY_CMD="${NOTIFY_CMD:-}"            # optional: gets one line on stdin per recovery (chat webhook, pager, ...)
mkdir -p "$(dirname "$STATE_FILE")" && touch "$STATE_FILE"

log() { echo "$(date -u +%FT%TZ) $1"; [ -z "${WATCHDOG_NOJOURNAL:-}" ] && command -v systemd-cat >/dev/null && echo "$1" | systemd-cat -t fuse-mount-watchdog -p "${2:-info}" 2>/dev/null; return 0; }
notify() { log "$1" "${2:-notice}"; [ -n "$NOTIFY_CMD" ] && printf '%s\n' "$1" | sh -c "$NOTIFY_CMD" >/dev/null 2>&1; return 0; }

state_get() { awk -v k="$1" '$1==k {print $2}' "$STATE_FILE"; }
state_set() {   # state_set <name> [pid]   (no pid = drop the entry)
    { awk -v k="$1" '$1!=k' "$STATE_FILE"; [ -n "${2:-}" ] && echo "$1 $2"; } > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# mountpoint_of <unit>: the first argument under $MOUNT_ROOT that follows a remote "host:path" on the sshfs ExecStart.
mountpoint_of() {
    systemctl cat "$1" 2>/dev/null | awk -v root="$MOUNT_ROOT" '
        /^ExecStart=/ && /sshfs/ {
            sub(/^ExecStart=[-@+!]*/, ""); seen = 0
            for (i = 1; i <= NF; i++) {
                if (!seen && $i ~ /^[^-\/][^ ]*:/) { seen = 1; continue }      # the remote side: user@host:path
                if (seen && index($i, root) == 1) { print $i; exit }
            }
        }'
}

MOUNTS=()
while read -r unit state; do
    [ "$state" = enabled ] || continue
    mp=$(mountpoint_of "$unit")
    [ -n "$mp" ] || continue
    name=${unit%.service}
    MOUNTS+=("$name:$mp:$unit")
done < <(systemctl list-unit-files --no-legend --plain "$UNIT_GLOB" 2>/dev/null | awk '{print $1, $2}')

if [ "${1:-}" = --list ]; then
    [ ${#MOUNTS[@]} -gt 0 ] && printf '%s\n' "${MOUNTS[@]}"
    exit 0
fi

recover() {
    local name="$1" path="$2" unit="$3"
    log "WEDGED: $name ($path): the last canary did not return within one interval; recovering" err
    local pid; pid=$(pgrep -f "sshfs .*[[:space:]]${path}([[:space:]]|\$)" | head -1)
    fusermount -uz "$path" >/dev/null 2>&1
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
    systemctl restart "$unit"

    local mark; mark=$(mktemp)
    ( stat "$path" && echo ok > "$mark" ) </dev/null >/dev/null 2>&1 &   # detach fully: a stuck verify must not hold our stdout
    local i=0
    while [ $i -lt $((VERIFY_S * 10)) ] && [ ! -s "$mark" ]; do sleep 0.1; i=$((i + 1)); done
    if [ -s "$mark" ]; then
        notify "RECOVERED: $name ($path) on $(hostname): remounted via $unit (wedged sshfs pid ${pid:-unknown})"
    else
        notify "RECOVERY UNCONFIRMED: $name ($path) on $(hostname): $unit restarted but stat still not back in ${VERIFY_S}s" crit
    fi
    rm -f "$mark"
}

for entry in "${MOUNTS[@]+"${MOUNTS[@]}"}"; do
    IFS=: read -r name path unit <<< "$entry"
    cpid=$(state_get "$name")
    if [ -n "$cpid" ]; then
        # still alive AND still our canary (a reused PID belonging to something else is not a wedge)
        if kill -0 "$cpid" 2>/dev/null && ps -o args= -p "$cpid" 2>/dev/null | grep -qF "stat $path"; then
            recover "$name" "$path" "$unit"
        fi
        state_set "$name"                       # judged: healthy or recovered; either way, start fresh
    fi
    stat "$path" </dev/null >/dev/null 2>&1 &   # this run's canary: never waited on, judged next run
    state_set "$name" "$!"
done
exit 0                                          # a clean run is exit 0; real problems are logged and notified above
