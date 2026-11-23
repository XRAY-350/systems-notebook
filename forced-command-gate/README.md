# forced-command-gate

Let one machine ask another to do a few named things as root, without giving it a root shell. And when root code
has to touch a path that a less-trusted account can write, do it in a way that account can't redirect.

```
restrict,from="192.0.2.10",command="/usr/local/sbin/gate" ssh-ed25519 AAAA... caller-key
```

```
$ ssh -i caller-key root@box "status app-web app-worker"
app-web=active
app-worker=active
$ ssh -i caller-key root@box "status app-web; id"
gate: refused: argument 2 has characters outside [A-Za-z0-9@._-] or is too long      (exit 9)
```

This is a standalone rewrite of the gates in a small server fleet I run. A bot host has to check service status,
start an agent sign-in, and restart a few services on two other machines. Before November its key on those machines
was an unrestricted root login. The production versions of this pattern sit in front of agent sign-in, the
status page, and Minecraft server restarts from a phone app.

**Who wrote it:** AI coding agents (Claude Code) wrote the code and the tests, to my requirements. The symlink bug
class below was found by one agent reviewing another agent's gate. My part was the requirement (no root shell
between boxes, every capability named) and the decisions on what each verb may do.

## The key line does half the work

- **`command=`** makes sshd ignore whatever the client asked to run. The gate always runs, and the client's string
  arrives in `SSH_ORIGINAL_COMMAND` as data.
- **`from=`** pins the key to the caller's address, so a copied key is useless from anywhere else. In production
  this was proven both ways before it was trusted: the caller's IPv4 address worked, and the same key from the same
  machine's IPv6 address was refused.
- **`restrict`** turns off port forwarding, agent forwarding, X11 and the PTY in one word, so the key can't be used
  as a tunnel even though it can run the gate.

## Design decisions

- **The allowlists are the policy, and they are literal.** Each verb lists exactly which arguments it takes:
  `restart` accepts two service names, `login-code` accepts two user names, `status` takes 1 to 40 unit names
  and is read-only. Anything not in a list is refused. There is no pattern like "any unit starting with app-".
- **No shell, ever.** The request is split with `shlex` and each token must match `[A-Za-z0-9@._-]{1,80}`, so
  `;`, `|`, `$( )`, backticks, redirects, quotes, slashes and globs never reach anything. The gate then runs one
  fixed helper path with a fixed argv. A request containing a control character is refused before it is split:
  `shlex` treats a newline as a separator, which writing this suite turned up.
- **Exit 9 means "refused".** A refusal is one line on stderr and exit 9; a helper's own exit code is passed
  through unchanged. So a caller can tell "the gate said no" from "the restart was refused because players are
  online" (exit 4 in production) from "the box didn't answer".
- **Secrets go on stdin.** A one-time login code is piped to the helper without being read, logged or echoed by
  the gate. A code placed in argv is refused, because argv shows up in process listings and logs. Refusal
  messages name the position of a bad token, never its value, in case the bad token is a pasted secret.
- **Dry-run twins.** `check-restart` runs the same checks with the same exit codes and never acts. The phone app
  calls it to decide whether to show a "players are online, restart anyway?" prompt.
- **It works before the key line is swapped.** Called with plain argv (no `SSH_ORIGINAL_COMMAND`) it behaves the
  same, so the callers moved to the new calling form while the old root line still existed, and the swap to the
  forced command broke nothing.
- **It logs the verb and the outcome only.** Never stdin, never a code.

## The bug class: root following a symlink

The gate that restarts Minecraft servers also writes two small files in the game account's home directory: a
"planned restart" marker (so the monitoring doesn't alert on a restart someone asked for) and a lock. A reviewing
agent noticed that the gate, running as root, opened those files with a plain `open()` and then `chown`ed them by
path. The game servers run as that account, so code running as that account (a server plugin, a mod) can replace
the marker with a symlink to any system file. The next restart from the phone would make root truncate that file
and give it to the game account: a privilege escalation that every normal run hides. The reviewer proved it in a
stub harness, without touching the live box.

The fix was for the class, not the one file. Every root script that wrote into that home directory was checked.
The class is "root code opens, truncates or chowns a path that a less-trusted account can create or replace", and
it covered the gate plus seven shell scripts (lock files opened with `exec 9>`, `cp` into a directory the account
owns, `chown` without `-h`). A separate review later found the same pattern in a Node alert-drain that read files from a
shared spool with `readFileSync`.

`safe_paths.py` is the reusable half of that fix:

- **`open_user_file()`** opens with `O_NOFOLLOW`, then checks the open fd: a regular file with exactly one link (a
  hard link to `/etc/shadow` is a regular file too, so the link count is what catches it). Ownership is fixed with
  `fchown` on the fd, never by path.
- **`safe_parent()`** walks every ancestor of a directory root is about to write into: each must be a real
  directory (checked with `lstat`), owned by a trusted uid, and not group or world writable unless it is sticky.
  After creating missing parts, the resolved path must equal the lexical one. The fleet's new deploy gate runs the
  same check before it installs any file.

## Verified

- `python3 test_gate.py`: 9 tests. Routing for each verb, the helper's exit code passed through, the secret
  delivered intact on stdin and absent from every argv and output, and **41 hostile requests**, each refused with
  exit 9 while no helper ran at all. The list covers shell metacharacters glued onto real verbs, path traversal,
  wrong arguments to real verbs, look-alike verbs, an embedded newline and an oversized token.
- `python3 test_safe_paths.py`: 13 tests. Each attack test plants the trap (symlink, dangling symlink, hard link,
  symlinked directory, world-writable directory, a file where a directory should be), runs the helper the way root
  would, and checks both that it refused and that the victim file is unchanged.
- **The tests can fail.** Removing each protection in turn (`O_NOFOLLOW`, the link count, the directory symlink
  check, the writable-directory check, the token class, the status bound, the login-code argument check) makes at
  least one test fail. One of those first survived: with the directory symlink check removed, the late realpath
  check still raised, but only after a directory had been created through the link. The test now also asserts
  that nothing was created on the far side.

In production, each gate was reviewed by a second agent before its key line went in: 37 of 37 hostile requests
refused on the sign-in gate, 32 of 32 on the restart verbs, with live checks on dummy targets that a planted
symlink, a hard link and a held lock each made the gate abort with the victim file unchanged.
