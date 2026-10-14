# systems-notebook

Standalone, working code pulled out of production systems I designed, direct and operate, each with a short
write-up of the actual engineering problem it solves (not toy examples, real pieces extracted from
real infrastructure and cleaned up to stand on their own).

The full story behind each of these (the systems they came from, the incidents that shaped them,
and a lot more that isn't code at all) is written up at
**[sourcekit.org/portfolio](https://sourcekit.org/portfolio/)**.

**Who wrote the code:** most of it was written by AI coding agents (Claude Code) working to my
requirements, under operating rules I set. My part is the design calls, spotting problems, and the
decisions. Where a module's story turns on who found or built something, its README says so.

## What's here

One module per system in the portfolio, picked for what actually generalizes cleanly to standalone
code, plus the agent coordination layer that runs all of them; MCFleet gets a second, since it's original infrastructure/tooling work with no
copyright complications to work around in how it's described.

**SourceKit** (iOS patch & distribution pipeline)
| Module | What it is |
|---|---|
| [`macho-signature-strip/`](macho-signature-strip/) | A pure-Python reimplementation of `codesign --remove-signature`, built so a build pipeline that develops on macOS but runs its CI on Linux behaves identically on both, with zero platform-specific tooling. Verified byte-for-byte against real signed binaries, not just "it doesn't crash." |
| [`adaptive-rate-governor/`](adaptive-rate-governor/) | A self-tuning, preventive rate limiter for a high-fanout service hitting a third-party API with undocumented rate limits. AIMD tuning, GCRA pacing (with the measured concurrency bug the naive token-bucket design had), a shadow mode for testing a new limit against real traffic without ever blocking anything. |
| [`failure-routing/`](failure-routing/) | When one step of a multi-stage build pipeline fails, tell only the owner who can fix that step. Routing is a data file (per app, per step), a route is only a proposal until its owner confirms it, a shared library's owner is added only when the log blames that library, and retryable failures stay quiet until the retry queue gives up. |

**MCFleet** (Minecraft server fleet, run like infrastructure)
| Module | What it is |
|---|---|
| [`mcfleet-rcon-pool/`](mcfleet-rcon-pool/) | A from-scratch, persistent Minecraft RCON client: one authenticated connection per server, reused for every command, instead of one per request. On a real production server it cut RCON connects from about 65 a minute per map viewer to about 2 a minute. I spotted the flood in the server's own log and pushed for the persistent connection; an AI agent traced it and built this. |
| [`mcfleet-backup-retention/`](mcfleet-backup-retention/) | A 26-line grandfather-father-son backup retention policy, implemented as a pure stdin-filenames-in / stdout-delete-list-out filter. Validated against a synthetic 480-file, 40-day dataset before ever being trusted on real backups. |
| [`gated-auto-update/`](gated-auto-update/) | A human-approve-once, apply-when-safe update state machine, generalized from a mod auto-updater: check for updates, one human click arms the plan, apply automatically the next safe window, with a hard guard against ever auto-applying a real version migration, and a failed apply's auto-rollback treated as a normal terminal outcome, not an error to keep retrying. |
| [`format-era-migrator/`](format-era-migrator/) | Parse a command written in one of several historical, syntactically-different format eras into an era-agnostic spec, then recompile it in any target era; generalized from a command converter covering 22 Minecraft versions across 3 real historical data-format eras, with explicit tracking of anything a lossy conversion can't carry over. |

**Community Platform** (moderation + engagement bot, two live communities)
| Module | What it is |
|---|---|
| [`permission-drift-guard/`](permission-drift-guard/) | Periodic access-control drift detection with two trust tiers: auto-correct system-level drift, only ever report user-level changes. Built after a platform-specific inheritance gotcha caused a real permissions leak; generalized here to work over plain objects instead of the original platform's live API. |

**Agent Operations** (several always-on AI coding agents and the one human they work for)
| Module | What it is |
|---|---|
| [`agent-bus/`](agent-bus/) | The whole coordination layer: a file-based message bus with a send-time crossing guard (measured first: 28% of 828 messages were a sender talking past an unanswered reply), wake daemons kept separate from storage, the human's own decisions cryptographically signed so a relayed "the human said yes" is never mistaken for one, a mirror that blocks credentials before they are published, a pinger and a laptop-side relay hub that wake sessions the server cannot reach, and an orchestrator that collects everything waiting on the human into one digest. 49 end-to-end checks, plus a mutation mode that proves the signature tests can fail. |

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
the portfolio's Media Relay page for the generalized write-up of the actual engineering: a hidden
framework timeout, a cross-instance cache bug, a mis-diagnosed network path).

Each directory is self-contained: the code, and a README explaining the actual problem, what was
tried and rejected, and why the final shape looks the way it does. No secrets, no internal
infrastructure details, no proprietary business logic; the code here works as written, with the
context genuinely generalized rather than search-and-replaced.

## Why extracted, not linked

The production repos these came from are private: they contain live infrastructure config,
credentials references, and enough operational detail about systems currently in use that making
them public outright isn't something I'm going to do casually. What's here is the reusable
engineering underneath that, pulled out and verified standalone, so it can actually be read and run
without needing access to anything private.
