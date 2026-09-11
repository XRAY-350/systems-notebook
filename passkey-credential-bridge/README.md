# passkey-credential-bridge

One shared WebAuthn (passkey) credential file, readable and writable by two independently-built
web apps that grew their own enrollment flows separately and expect different field names for the
same underlying data.

## The actual problem

Two apps I run — one an admin control panel, one a separate operational dashboard — each have
their own passkey login (Face ID / Touch ID / a hardware key, no passwords). They were built at
different times, so each grew its own credential storage format independently:

```
App A's internal names:  {id, public_key, sign_count, added}
App B's on-disk format:  {id, publicKey, counter, transports, deviceType, backedUp, label, created}
```

Functionally identical data (a WebAuthn credential ID, its public key, and a signature counter
used to detect cloned authenticators), stored under different keys. A passkey enrolled through App
A's UI wasn't recognized by App B's login, and vice versa — annoying for a single operator who
wants one passkey to work everywhere, and a real gap if the operator ever needs to log in somewhere
in a hurry and the "wrong" credential is the one their device offers first.

## What I deliberately did NOT do

The obvious fixes are both worse than the one below:

- **Migrate one app's storage format to match the other's.** Touches code in an app that was
  working fine, for a problem that isn't really about that app's format being wrong — it's about
  two formats needing to agree.
- **Extract a shared credential library both apps import.** Now both apps have a new dependency,
  a version to keep in sync, and a migration to do on either side just to adopt it. For two field
  renames, that's a lot of new surface area for not much gain.

## What the fix actually is

One canonical file on disk, in App B's format (it existed first). App A gets a small translation
layer — `load_creds()` and `save_creds()` in `credential_store.py` — that runs *only* at the two
points App A actually touches the file. Every other line of App A's code keeps using App A's own
field names, completely unaware any translation is happening. `load_creds()` uses `setdefault`, not
assignment, so a credential that already has App A's field name (because it was enrolled through
App A originally) is left untouched — the translation only fills in what's missing.

`save_creds()` writes atomically (write to a temp file, then `os.replace`), because a crash mid-write
here doesn't just corrupt one app's data — it locks *both* apps out of authentication at once,
since they share the one file.

## Why this shape, generally

The instinct worth keeping from this isn't really about WebAuthn specifically: when two systems
need to agree on one piece of shared state but have diverged on its representation, translating at
the boundary (read/write) is often cheaper and less risky than migrating either side's internal
representation — especially when both sides are already working and the actual disagreement is
just naming, not semantics.
