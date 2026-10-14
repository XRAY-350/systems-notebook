#!/bin/bash
# Example WATCHD_WAKE_CMD: type the nudge into the agent's tmux session and confirm it actually submitted.
#   WATCHD_WAKE_CMD='examples/tmux-wake.sh {agent} {text}'
# Lessons baked in: clear the input line first (never type on top of a half-submitted line); send the text and
# the Enter as SEPARATE keystrokes with a pause (a combined send can be swallowed by bracketed paste, leaving the
# nudge sitting dead in the input box); confirm by checking the text left the input area; retry the whole
# clear+type+Enter once before reporting failure. Exit 0 only when delivery is confirmed.
set -u
S="$1"; TXT="$2"
tmux has-session -t "$S" 2>/dev/null || exit 2
attempt() {
  sleep 0.4
  tmux send-keys -t "$S" C-u; sleep 0.2
  tmux send-keys -t "$S" -l "$TXT"; sleep 0.7
  tmux send-keys -t "$S" Enter
  for _ in 1 2 3; do
    sleep 0.5
    tmux capture-pane -p -t "$S" -S -6 | grep -qF -- "${TXT:0:40}" || return 0   # gone from the input box: submitted
    tmux send-keys -t "$S" Enter
  done
  return 1
}
attempt || attempt
