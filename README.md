# systems-notebook

Standalone, working code pulled out of production systems I built and operate, each with a short
write-up of the actual engineering problem it solves — not toy examples, real pieces extracted from
real infrastructure and cleaned up to stand on their own.

The full story behind each of these — the systems they came from, the incidents that shaped them,
and a lot more that isn't code at all — is written up at
**[sourcekit.org/portfolio](https://sourcekit.org/portfolio/)**.

## What's here

One module per system in the portfolio, picked for what actually generalizes cleanly to standalone
code — plus MCFleet gets a second, since it's original infrastructure/tooling work with no
copyright complications to work around in how it's described.

**SourceKit** (iOS patch & distribution pipeline)
| Module | What it is |
|---|---|
| [`macho-signature-strip/`](macho-signature-strip/) | A pure-Python reimplementation of `codesign --remove-signature`, built so a build pipeline that develops on macOS but runs its CI on Linux behaves identically on both, with zero platform-specific tooling. Verified byte-for-byte against real signed binaries, not just "it doesn't crash." |
| [`adaptive-rate-governor/`](adaptive-rate-governor/) | A self-tuning, preventive rate limiter for a high-fanout service hitting a third-party API with undocumented rate limits. AIMD tuning, GCRA pacing (with the measured concurrency bug the naive token-bucket design had), a shadow mode for testing a new limit against real traffic without ever blocking anything. |

**MCFleet** (Minecraft server fleet, run like infrastructure)
| Module | What it is |
|---|---|
| [`mcfleet-rcon-pool/`](mcfleet-rcon-pool/) | A from-scratch, persistent Minecraft RCON client — one authenticated connection per server, reused for every command, instead of one per request. Cut a real production server's connection-churn log noise by 86% (the fix behind it is what caught the bug in the first place: reading the server's own log volume, not assuming "no errors" meant "no problem"). |
| [`mcfleet-backup-retention/`](mcfleet-backup-retention/) | A 26-line grandfather-father-son backup retention policy, implemented as a pure stdin-filenames-in / stdout-delete-list-out filter. Validated against a synthetic 480-file, 40-day dataset before ever being trusted on real backups. |

**Community Platform** (moderation + engagement bot, two live communities)
| Module | What it is |
|---|---|
| [`permission-drift-guard/`](permission-drift-guard/) | Periodic access-control drift detection with two trust tiers — auto-correct system-level drift, only ever report user-level changes. Built after a platform-specific inheritance gotcha caused a real permissions leak; generalized here to work over plain objects instead of the original platform's live API. |

**Infrastructure & Recovery**
| Module | What it is |
|---|---|
| [`declarative-drift-reconciler/`](declarative-drift-reconciler/) | Declarative "does live state match its source of truth" checking with self-heal and a three-tier (confirmed-correct / couldn't-check / confirmed-wrong) report, instead of collapsing everything into a single pass/fail. The pattern that caught a real CI runner misconfiguration that looked, for 48 hours, like six unrelated failures. |

**Admin Control Plane** (passkey-gated ops surface + remote terminal)
| Module | What it is |
|---|---|
| [`passkey-credential-bridge/`](passkey-credential-bridge/) | ~30 lines that let one WebAuthn passkey work across two independently-built apps with different credential storage formats, by translating at the read/write boundary instead of migrating either app's internal format. |

**Media Relay** isn't represented here on purpose: the real code is tightly coupled to how it
connects to its streaming platform, which isn't something I'm describing in detail publicly (see
the portfolio's Media Relay page for the generalized write-up of the actual engineering — a hidden
framework timeout, a cross-instance cache bug, a mis-diagnosed network path).

Each directory is self-contained: the code, and a README explaining the actual problem, what was
tried and rejected, and why the final shape looks the way it does. No secrets, no internal
infrastructure details, no proprietary business logic — the code here works as written, with the
context genuinely generalized rather than search-and-replaced.

## Why extracted, not linked

The production repos these came from are private — they contain live infrastructure config,
credentials references, and enough operational detail about systems currently in use that making
them public outright isn't something I'm going to do casually. What's here is the reusable
engineering underneath that, pulled out and verified standalone, so it can actually be read and run
without needing access to anything private.
