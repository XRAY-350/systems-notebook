#!/bin/bash
# Example WATCHD_STATE_CMD for an agent CLI running in a tmux session named after the agent.
#   WATCHD_STATE_CMD='examples/tmux-state.sh {agent}'
# Prints idle | working | dialog | limit. The markers are the screen text of YOUR agent CLI; set them for the
# tool you run. The rule that matters: "idle" needs a POSITIVE match on the resting prompt's footer. Anything
# that is not clearly the resting prompt counts as "dialog", because typing Enter into an unknown screen can
# confirm a choice nobody made.
set -u
S="$1"
BUSY_MARKER="${BUSY_MARKER:-esc to interrupt}"                     # shown while a turn is running
IDLE_MARKER="${IDLE_MARKER:-for shortcuts}"                        # resting prompt footer
MENU_MARKERS="${MENU_MARKERS:-to select|arrow keys}"               # transient selection UI hints
LIMIT_MARKERS="${LIMIT_MARKERS:-usage limit|limit reached|limit will reset}"
pane=$(tmux capture-pane -p -t "$S" -S -30 2>/dev/null) || { echo unknown; exit 0; }
[ -n "${pane// /}" ] || { echo unknown; exit 0; }
low=$(tr '[:upper:]' '[:lower:]' <<<"$pane")
grep -qF -- "$BUSY_MARKER" <<<"$low" && { echo working; exit 0; }
tail -n 8 <<<"$low" | grep -qE -- "$LIMIT_MARKERS" && { echo limit; exit 0; }   # bottom only: old mentions can't wedge it
grep -qE -- "$MENU_MARKERS" <<<"$low" && { echo dialog; exit 0; }
grep -qF -- "$IDLE_MARKER" <<<"$low" && echo idle || echo dialog
