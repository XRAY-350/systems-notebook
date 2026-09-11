# mcfleet-rcon-pool

A from-scratch, persistent Minecraft RCON client — one authenticated socket per server, reused
for every command, instead of a fresh connection per request.

```js
const rcon = require('./rcon');
const reply = await rcon.exec('/path/to/server/dir', 'list');
```

## The problem, found by reading the server's own log volume

A live-map feature I run polls each Minecraft server for every online player's position roughly
once a second per open viewer, three separate RCON queries each (position, dimension, rotation).
The original implementation shelled out to a small Python RCON script on every single poll —
which meant spawning a brand-new process AND opening a brand-new authenticated RCON connection for
every query, all day, for as long as anyone had the map open.

It went unnoticed as a *functional* bug — positions displayed fine, nothing looked broken. It was
only caught by actually reading the server's own log volume rather than trusting that "no errors"
meant "no problem": **86% of the server's total log output turned out to be RCON connect/disconnect
churn**, not gameplay. Roughly sixty processes a minute were being spawned on a box that was already
resource-constrained, for zero functional benefit — the connection was authenticated, used once,
and thrown away, every single time.

## The fix

`rcon.js` implements the Source RCON protocol (the same wire protocol Minecraft, Source-engine
games, and several other servers share) directly over a raw TCP socket, with one persistent,
authenticated connection per server, reused across every command through a single-in-flight queue
(a server only ever has one RCON request in flight at once, so pending commands queue up rather
than trying to multiplex a protocol that doesn't support it).

Verified directly against real traffic: sixteen RCON commands issued back to back produced exactly
one logged connection in the server's own log, down from sixteen — the fix eliminates the churn at
the source rather than filtering it out downstream.

## Design notes

- **Auto-reconnect, not a persistent-connection assumption.** A dropped socket just fails whatever
  command was in flight (the caller sees one failed poll, not a crash) and the *next* call
  transparently reconnects and re-authenticates. Nothing needs to know the connection could have
  died in between calls.
- **One pool per server directory**, keyed by the server's own directory path, so multiple servers
  each keep their own independent persistent connection without any explicit registration step —
  the first `exec()` call for a new server directory just works.
- **Reads the RCON port/password straight out of the server's own `server.properties`** rather than
  requiring separate configuration — there's already exactly one place that value lives correctly,
  so that's where this reads it from.

## Verification

Tested against a minimal mock implementation of the Source RCON wire protocol (auth handshake +
two sequential exec commands over the same TCP connection) before being extracted here — confirmed
the auth handshake completes, both commands get the correct matched responses, and both go out over
the literal same socket rather than opening a second connection for the second command.
