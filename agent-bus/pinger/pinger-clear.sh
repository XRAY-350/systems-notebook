#!/bin/bash
# Give the pinger a fresh conversation WITHOUT restarting its process. A restart re-registers the session with the
# agent platform (a new duplicate "Pinger" appears in the human's session list every time); clearing the conversation
# keeps the same process and registration and makes it reread PINGER.md. Only types when the pane is idle, waiting
# up to 10 min if it is mid-relay. Used nightly (pinger-fresh.timer) and after instruction changes.
set -u
S="${PINGER_SESSION:-pinger}"
STATE_CMD="${PINGER_STATE_CMD:-$(dirname "$0")/../examples/tmux-state.sh $S}"
idle() { [ "$(eval "$STATE_CMD")" = idle ]; }
for _ in $(seq 120); do idle && break; sleep 5; done
idle || { echo "pinger busy for 10 min; skipping this time"; exit 0; }
tmux send-keys -t "$S" C-u; tmux send-keys -t "$S" -l "/clear"; sleep 0.6; tmux send-keys -t "$S" Enter
