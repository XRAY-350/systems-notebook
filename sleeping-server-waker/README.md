# sleeping-server-waker

Stop a game server when nobody is on it, and start it again the moment a real player tries to join, without the
player needing anything but the normal game client. A small unprivileged listener holds the server's ports while it
sleeps, answers the server list with "asleep, join to wake", and on a genuine join attempt drops a flag file and exits.

```
$ WAKER_ID=survival WAKER_JAVA_PORT=25565 WAKER_BEDROCK_PORT=19132 WAKER_MODE=open python3 mc_waker.py
mc-waker[survival]: sleeping for survival.service on tcp/25565 + tcp/19132 (NetherNet signaling) + udp/19132; mode=open; name='survival'; cooldown=600s; ip_limit=5/3600s
WAKE mode=open ip=203.0.113.7 path=java-login name='alice'
```

This is a standalone copy of the waker used on a small Minecraft fleet I run. The servers share one machine, and
the rule was simple: every server sleeps when it is empty, and anyone who tries to join a public one wakes it. A
version for one private server existed already; this one serves any number of servers from one systemd template.

**Who wrote it:** AI coding agents (Claude Code) wrote the code and the tests, to my requirements. The scanner
guard and the known-players mode came from watching what actually woke the servers.

## What it answers

Minecraft has two client families, and each has more than one way to find and join a server:

| Protocol | Request | Response |
|---|---|---|
| Java TCP | status ping | asleep MOTD with the server name; echoes the client's protocol so it doesn't show "outdated" |
| Java TCP | login | wakes (see the modes) and disconnects with "waking up, reconnect in about a minute" |
| Bedrock UDP (RakNet) | unconnected ping | pong with an asleep MOTD |
| Bedrock UDP (RakNet) | open connection request 1 | wakes |
| Bedrock TCP (NetherNet signaling) | `GET /v1/join` | status JSON, the same fields an awake server reports |
| Bedrock TCP (NetherNet signaling) | `POST /v1/join` with an SDP offer | wakes. The offer carries the player's identity token: only its first bytes are read, and it is never logged. |
| Bedrock TCP | anything else | 404 / 400 / 405, no wake |

Newer Bedrock clients speak only NetherNet, which is plain HTTP signaling on TCP plus WebRTC on UDP, both on the
Bedrock port. A client that opens with the HTTP/2 preface gets a minimal HTTP/2 answer instead.

## Design decisions

- **The waker never starts anything.** It runs as a systemd `DynamicUser` with no privileges and parses packets
  from the internet, so all it may do is write one flag file in its runtime directory and exit. A separate root
  `.path` unit watches the flag and starts the server. The server's `ExecStartPre` stops the waker so the server can
  bind its ports, and its `ExecStopPost` arms the waker again.
- **It can't touch a running server.** The unit is deliberately not `Conflicts=` with the server: on a production
  server that once stopped a running game the moment the waker started. Instead an `ExecCondition` script checks
  that the server unit is inactive or failed, and skips quietly otherwise.
- **Everything server-specific comes from the environment.** One env file per server (`example.env`), so the same
  file serves every server. The server's whitelist and `server.properties` arrive read-only through systemd
  credentials, never by reading a home directory.
- **Two modes.** `whitelist`: a Java login wakes the server only for a name on the list; anyone else gets a refusal
  text. `open`: any genuine client may wake it. In both, a Bedrock join attempt wakes it, because Bedrock sends no
  name that early (the test suite pins this, so it can't change by accident).
- **Every read is size-capped and time-limited.** One stalled client can't hold up another client's ping.

## The scanner guard

Once a few servers slept in public, the logs showed that most wakes weren't players. Internet scanners send Java
login packets around the clock. Three layers, in order:

1. **Strict parse.** A Java login counts only when the handshake is well formed and consumes the packet exactly, and
   is followed by a login-start whose name is 1 to 16 of `[A-Za-z0-9_]` (optionally after a Floodgate prefix).
   Half handshakes, garbage and trailing bytes are logged and dropped.
2. **Per-IP limit.** Every wake-qualifying attempt, on all three protocol paths, is counted against its source IP;
   bursts within a few seconds count as one. Past the limit in the window, that IP is ignored and logged once.
3. **Cooldown.** After a wake, no new flag for a set time; a player still gets the waking text, since the server is
   presumably starting. The trade-off is written down: if a start fails, nobody can wake it again until the
   cooldown ends.

Even then, all eight wakes of one public server over a few days turned out to be bots that do send well-formed
logins with plausible names. Checking for a real game account was not an option, because that server allows
offline-mode players. The answer was **known players only**: the public server runs in `whitelist` mode against a
list of everyone the server has ever seen, rebuilt from its own user cache before each sleep, and a new player wakes
it with a chat command in the community's Discord instead. `WAKER_ASLEEP_TEXT` and `WAKER_DENY_TEXT` exist so the
server list and the refusal can say exactly that.

## Settings (one env file per server)

| Variable | Default | Meaning |
|---|---|---|
| `WAKER_ID` | required (the unit sets it from `%i`) | Server id; names the runtime dir, goes in logs and the flag. |
| `WAKER_UNIT` | `<id>.service` | The server's unit, checked by the ExecCondition. |
| `WAKER_JAVA_PORT`, `WAKER_BEDROCK_PORT` | required | Ports to hold while asleep (Bedrock: UDP and TCP). |
| `WAKER_MODE` | `whitelist` | `whitelist` or `open`. |
| `WAKER_NAME` | `motd` from server.properties, else the id | Name in pings and in the waking text. |
| `WAKER_ASLEEP_TEXT` | `asleep, join to wake it (about a minute)` | Java server-list second line. One line, at most 200 characters. |
| `WAKER_DENY_TEXT` | `You are not whitelisted on this server.` | Whitelist-mode refusal. One line, at most 200 characters. |
| `WAKER_COOLDOWN_S` | 600 open, 0 whitelist | Minimum time between two wake flags. |
| `WAKER_IP_LIMIT`, `WAKER_IP_WINDOW_S` | 5 open (0 = off in whitelist), 3600 | Per-IP attempt budget. |
| `WAKER_COALESCE_S` | 10 | Attempts from one IP closer than this count as one. |
| `WAKER_BIND` | all interfaces | Listen address (tests use 127.0.0.1). |

A malformed setting (a port out of range, a missing id, an over-long text) makes the waker refuse to start with a
clear message, so check `systemctl status` after editing an env file.

## Files

- `mc_waker.py`: the waker (stdlib only).
- `mc-waker@.service`: the systemd template, with the sandboxing (`ProtectSystem=strict`, `ProtectHome`,
  `MemoryDenyWriteExecute`, a 128 MB cap, IPv4/IPv6 sockets only).
- `mc_waker_condition.sh`: the ExecCondition. It finds the server unit from the environment, else from the env file
  itself, else from the id, so it never depends on how systemd expands variables on a command line.
- `example.env`: one server's settings.

## Verified

`python3 test_mc_waker.py` starts the waker on free local ports with a temporary runtime and credentials directory
and plays the client side of every protocol path: 49 checks, stdlib only, no root.

- status and login in both modes, a known name waking and an unknown one refused with no flag;
- open-mode Java validation (garbage, half handshakes, bad names, trailing bytes all refused) and the Floodgate prefix;
- RakNet ping and open request, NetherNet over HTTP/1.1 and HTTP/2 prior knowledge, and anything else getting no wake;
- the cooldown blocking a second flag and expiring, the per-IP limit, the custom texts, an over-long text refused;
- a stalled client not blocking another client's ping.

Removing the whitelist check, the strict Java parse or the cooldown each makes the suite fail (3 to 5 checks each).
In production the waker was also tested through systemd on spare ports before each change went live.
