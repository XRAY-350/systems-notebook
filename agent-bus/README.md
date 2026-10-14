# agent-bus

A message bus, wake system and human-approval layer for a small fleet of always-on AI coding agents
(Claude Code sessions) and the one human they work for. Plain files, Python standard library, OpenSSH
for signatures, systemd for scheduling. No broker, no database.

```
AGENTMSG_AGENTS=builder,ops,infra,laptop-builder,orchestrator  AGENTMSG_WATCHED=builder,ops,infra

agentmsg send --from builder --to ops --thread deploy --subject 'ready to ship?' --needs-reply 'build 41 is green'
agentmsg inbox --from ops                 # (ops) 1 unread: 3f9a1c ... builder -> [deploy] ready to ship?
agentmsg read 3f9a1c --from ops           # moves it to read/ and stamps read_at (the receipt)
agentmsg reply 3f9a1c --from ops 'yes'    # same thread, back to builder
agentmsg verify 7c21e0                    # exit 0 only for a message the human really signed
```

## The setup it was built for

Three agents run around the clock on a Linux server, each owning one area of work (a build pipeline,
a game-server fleet, a set of chat bots). More agent sessions run on the human's laptop, one per
project, and come and go. They hand work to each other constantly, the human reads along in a chat
channel, and most of the time nobody is watching. Every part of this module exists because of
something that went wrong in that setup.

## The parts

| Part | What it does |
|---|---|
| [`agentmsg`](agentmsg) | The bus CLI: store, send/inbox/read/reply/thread, crossing guard, activity board, scheduling, announcements, secret guard, attachments, signed human messages, held approval requests, a crossing metric. |
| [`agentmsg-watchd`](agentmsg-watchd) | One daemon per server-side agent. Notices new mail and wakes the agent from outside its process, only when it is idle. |
| [`agentmsg-sign-inject`](agentmsg-sign-inject) | The only way the human's own decisions enter the bus: signed with a key that exists on one host. |
| [`pinger/`](pinger/) | Wakes laptop-side sessions, which the server cannot type into: a watcher plus a tiny always-on "pinger" agent. |
| [`relay-hub/`](relay-hub/) | A hub session on the laptop that can wake sessions which are unloaded and unreachable from the server. |
| [`orchestrator/`](orchestrator/) | The human's one place for pending work: collects everything waiting on the human from every project into one digest. |
| [`systemd/`](systemd/), [`examples/`](examples/) | Units and timers; example tmux adapters for checking and waking an agent CLI. |
| [`test_agent_bus.py`](test_agent_bus.py) | 49 end-to-end checks in a throwaway store, plus a mutation mode that proves the signature tests can fail. |

## Storage and waking are separate jobs

The first design treated "send a message" and "make the other agent notice" as one thing. They fail
differently, so they were split:

- **The bus is the durable store.** Every message is a JSON file written atomically (temp file plus
  rename) into the recipient's `inbox/`, the sender's `sent/`, and a `threads/` archive. Reading moves
  the inbox copy to `read/`; that move is the receipt.
- **Waking is someone else's job.** A watcher that ran inside an agent's own session was killed at
  the end of each turn, so `agentmsg-watchd` runs as its own systemd unit per agent. It types a
  one-line nudge into the agent only when the agent is resting at its prompt: an Enter landing in a
  confirmation dialog would choose an option nobody chose. The "is it idle" and "how to wake it"
  steps are pluggable commands, so the daemon does not care which agent tool it is talking to.

The watcher's shape comes from three incidents. It once sat silent for 19 hours with mail waiting:
its idle check was stuck, and it only logged when it nudged, so the journal said nothing. Now it
logs a heartbeat at least every 5 minutes and never stops trying while mail is unread. Later, while
the account was at its usage limit, a fixed 5-minute re-nudge typed about 60 identical nudges over
roughly 14 hours, which all landed as one giant turn when the limit reset. Re-nudges now back off
exponentially, and new mail resets the backoff. Finally, its first "agent is stuck" alert measured
from the last time the agent was seen idle, which was only checked while mail was pending. So every
first message after a quiet stretch fired an instant alert: 39 urgent alerts in 6 days. Alerts now
run on an episode clock per cause (a dialog after 30 minutes, a usage limit after 60, a long turn
after 6 hours), and repeats double their gap.

**Laptop sessions need two more pieces**, because the server cannot type into them. The pinger
watcher batches their mail and hands one `RELAY` line to a small pinger agent on the server, which
messages the right session through the agent platform's remote-messaging feature. While a message
stays unread it re-wakes at 15, 30, 60 and 120 minutes, then every 240 minutes for up to 72 hours,
so a laptop that was asleep still gets woken once it is back. A session the laptop has unloaded is
unreachable from the server, but a hub session on the laptop can wake it by its local id
([`relay-hub/`](relay-hub/HUB.md)). If the hub is unreachable too, the pinger asks any reachable
laptop session to wake both. All four delivery paths were tested live: direct, hub, the
safety net, and "nothing reachable, the mirror channel is the signal".

Two things were tried and dropped along the way. Keep-alive pings to stop the laptop unloading idle
sessions turned out unnecessary: a probe of 100 checks showed idle sessions stayed loaded for about
8.5 hours, and the unloads were app restarts. Restarting the pinger to refresh its instructions
was also dropped, because every restart registered a new duplicate session in the human's list. It
now clears its conversation in place instead ([`pinger-clear.sh`](pinger/pinger-clear.sh)).

## Messages that cross

Before building anything, the existing traffic was measured: 828 server-agent messages over six
weeks. In 229 of them (28%) the same sender sent again before the other side replied. Fifty times,
two agents messaged each other on different threads within five minutes. Nobody used `reply`. None
of this ever errors: both messages deliver fine, and each side answers something the other has
already moved past.

An activity board ("what I am doing right now") was the first idea, and it is in here, but it cannot
stop a message already in flight and it depends on agents remembering to update it. The fix had to
live at send time, the one place that knows what the sender has not read:

- **Crossing guard:** `send`, `reply` and `schedule` refuse (exit 1, listing the mail) when the
  sender has unread conversational mail from a recipient. `--force` overrides and is logged. A softer
  note fires when the recipient has not read the sender's previous message yet. Broadcast mail never
  counts, or one fleet-wide notice would block every conversation. Scheduling obeys the same rule,
  so a hold cannot be dodged with `schedule --in 1m`.
- **Read receipts with a time:** a rename keeps the file's mtime, so "was it read before the reply
  went out?" could not be answered. Reads now stamp `read_at`, and `agentmsg crossings` measures the
  real rate going forward instead of assuming the guard works.

## "The human said yes" is not proof

Agents relay the human's decisions to each other all the time ("approved by the owner, go ahead").
Anything that can run the CLI could write `--from owner`, so a careful agent re-asks, and the human
ends up approving the same thing twice. One night an orchestrator relayed about a dozen approvals,
and every one needed the human again.

So the human's own decisions are **signed**. The human answers through a chat client (Approve, Deny,
Reply buttons or a typed line). The bot receiving those calls
[`agentmsg-sign-inject`](agentmsg-sign-inject) through a narrow sudo rule. That host is the only one
holding the private key, an OpenSSH key used with `ssh-keygen -Y sign`. Every other host has only
the public key in an `allowed_signers` file. `agentmsg verify <id>` exits 0 only if every stored copy
of the message verifies, so altering just the inbox copy an agent reads is caught. The inbox shows
`[HUMAN, signed]`, `[claims HUMAN, NOT signed]` or `[claims HUMAN, BAD signature]`. The limit, stated
plainly: root on the signing host can sign. This stops relayed and injected claims, not a compromised
signing host.

On top of that, **held approval requests**: `send --owner-approval` addressed to other agents stores
the request without delivering it, so they are not woken about something that may be denied. Only a
signed reply starting with `APPROVED` releases it, stamped with the approval id each recipient can
verify. An unsigned "APPROVED" does nothing.

## A mirror that cannot leak

Every message is mirrored to a chat channel the human reads (`AGENTMSG_MIRROR_CMD`, any command that
accepts JSON on stdin). That makes the bus a publishing channel, so:

- a send whose subject, body or attachments look like a credential is **blocked**, naming the kind of
  credential and the line, never the value. `--allow-secret "<reason>"` overrides and is logged, and the
  mirror masks it anyway;
- attachments travel as `name, size, sha256` and are never mirrored; `fetch` re-checks the hash;
- whether to mirror is decided from the message itself, not from the caller. The human's own chat
  lines are not echoed back, and the human's forwards from elsewhere are. An earlier version relied on
  the injecting caller to set a no-mirror flag, and the human's forwards silently vanished.

Small guards that each came from a lost message:
- A literal `-` body used to be stored as the message while the heredoc was dropped. One agent lost
  six messages that way, each reported as sent.
- Empty bodies are refused.
- A subject with a double, leading or trailing space is refused. That is the fingerprint of a
  backticked word eaten by shell substitution, and a warning there was missed, because the shell's
  own error had already scrolled past.
- Identities and thread names are validated, because they become directory names.

## The orchestrator

With a dozen sessions, "what is waiting on me?" had no single answer. Every project keeps a
`SLATE.md` with a `## Waiting on owner` section. The orchestrator's
[`gather.py`](orchestrator/gather.py) collects in one read-only pass:

- every project's owner section;
- needs-reply mail nobody answered;
- approval requests with no signed answer;
- unread laptop mail;
- failed CI runs;
- dated reminders.

A second pass asks each active agent for anything that lives only in its context, and has it write
the item to its SLATE first, so the next pass finds it without asking. Approvals go out as one
grouped control. The answers come back signed, so the orchestrator never becomes an approver
itself.

## One more lesson: the store's location is part of the protocol

When the agents moved to a new server, one of them kept the old store through a network mount while
the others used the new local default, so messages silently stopped crossing between them. The human
noticed first, from a message addressed to a machine that no longer ran any agents. The two stores were diffed,
merged with nothing lost, and every agent pointed at one path. Since then, moving an agent includes
pointing it at the store explicitly, and the inject helper fails fast when the store is unreachable
instead of hanging on a dead mount.

## Running it

```
cp agentmsg agentmsg-watchd agentmsg-sign-inject /usr/local/bin/
cp systemd/agentmsg.env /etc/agentmsg.env            # agents, watched set, store, timezone, quiet hours, adapters
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl enable --now agentmsg-watch@builder agentmsg-scheduler.timer agentmsg-announce-sweep.timer
python3 test_agent_bus.py && python3 test_agent_bus.py --mutation
```

`--mutation` reruns the suite against a copy of `agentmsg` whose signature check always says "signed".
The suite must fail on the forgery checks, and it does, so passing tests mean something.
