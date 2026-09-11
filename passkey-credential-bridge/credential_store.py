#!/usr/bin/env python3
"""
credential_store.py — one shared WebAuthn (passkey) credential file, readable
and writable by two independently-built web apps that each expect a different
JSON shape for the same underlying data.

The problem this solves: App A and App B each grew their own WebAuthn
enrollment flow independently, on different timelines, with different field
names for the exact same concept:

    App A's internal names:  {id, public_key, sign_count, added}
    App B's on-disk format:  {id, publicKey, counter, transports,
                               deviceType, backedUp, label, created}

A passkey enrolled through App A's UI didn't work when App B tried to verify
it, and vice versa, because each app only recognized its own field names.

The fix deliberately does NOT touch either app's internal code, and does NOT
introduce a shared library both apps have to adopt. Instead there is exactly
ONE canonical file on disk (App B's own format, since it was there first),
and App A's side gets a translation layer that runs only at the two points
where App A touches the file: load and save. Every other line of App A's own
code keeps using its own field names, completely unaware anything is being
translated underneath it.
"""
from __future__ import annotations

import json
import os

CRED_PATH = os.environ.get("CREDENTIAL_STORE_PATH", os.path.expanduser("~/.credentials.json"))


def load_creds() -> list[dict]:
    """Read the shared credential store, translated into App A's own field names.

    Tolerates the file being a bare list (an older format) or missing/corrupt
    entirely (returns an empty list rather than raising -- a WebAuthn login
    flow should degrade to "no credentials enrolled yet", never crash).
    """
    try:
        with open(CRED_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []

    creds = raw.get("credentials", raw) if isinstance(raw, dict) else raw
    if not isinstance(creds, list):
        return []

    for c in creds:
        # setdefault, not assignment: if a credential already has App A's own
        # field name (e.g. it was enrolled through App A originally), leave
        # it alone -- only fill in the gap when reading App B's own format.
        c.setdefault("public_key", c.get("publicKey", ""))
        c.setdefault("sign_count", c.get("counter", 0))
        c.setdefault("added", c.get("created"))
    return creds


def save_creds(creds: list[dict]) -> None:
    """Write the shared credential store, keeping BOTH apps' field names in
    sync on every save -- so a credential enrolled or updated from EITHER
    app's UI stays fully readable by the other.

    The extra fields this sets (transports, deviceType, backedUp, label,
    created) only matter to App B's own verification path; App A's login
    flow doesn't require them, so sensible defaults are fine here even
    though App A's code never looks at them.
    """
    for c in creds:
        c["publicKey"] = c.get("public_key", c.get("publicKey", ""))
        c["counter"] = c.get("sign_count", c.get("counter", 0))
        c.setdefault("transports", [])
        c.setdefault("deviceType", "unknown")
        c.setdefault("backedUp", False)
        c.setdefault("label", "passkey")
        c.setdefault("created", None)

    cred_dir = os.path.dirname(CRED_PATH)
    if cred_dir:
        os.makedirs(cred_dir, exist_ok=True)

    # Atomic write: a crash mid-write must never leave a half-written,
    # unparseable credential file -- that would lock BOTH apps out at once.
    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"credentials": creds}, f, indent=2)
    os.replace(tmp, CRED_PATH)
