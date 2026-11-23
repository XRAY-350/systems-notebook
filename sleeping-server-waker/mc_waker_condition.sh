#!/bin/sh
# mc_waker_condition.sh <id> -- ExecCondition of mc-waker@<id>.service. Exit 0 only if the server's unit is
# inactive or failed (so the waker can take its ports); exit 1 otherwise (systemd then skips the start without
# marking the unit failed). Never touches the server.
#
# Which unit: $WAKER_UNIT from the environment (EnvironmentFile= is applied to ExecCondition= processes), else the
# WAKER_UNIT= line of /etc/mc-waker/<id>.env read directly (belt and braces), else <id>.service.
id="${1:-}"
case "$id" in ''|*[!A-Za-z0-9_.@-]*) echo "mc_waker_condition: bad instance id" >&2; exit 1;; esac
unit="${WAKER_UNIT:-}"
if [ -z "$unit" ] && [ -r "/etc/mc-waker/$id.env" ]; then
  unit="$(sed -n 's/^[[:space:]]*WAKER_UNIT[[:space:]]*=[[:space:]]*//p' "/etc/mc-waker/$id.env" | tail -n 1 | tr -d "\"'")"
fi
[ -n "$unit" ] || unit="$id.service"
case "$unit" in *[!A-Za-z0-9_.@:-]*) echo "mc_waker_condition: bad unit name" >&2; exit 1;; esac
case "$unit" in *.*) ;; *) unit="$unit.service";; esac
state="$(/bin/systemctl show -p ActiveState --value "$unit" 2>/dev/null)"
case "$state" in
  inactive|failed) exit 0 ;;
  *) echo "mc_waker_condition: $unit is ${state:-unknown}, not starting the waker"; exit 1 ;;
esac
